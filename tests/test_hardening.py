"""Stability: pipes never block children, config recovery, CSRF guard, single instance, meter fallback."""

import http.client
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
import urllib.error
from pathlib import Path

from tfcz_audio import meters
from tfcz_audio.config import Config, load_or_recover, parse, save
from tfcz_audio.pw import DrainedProcess, FakeBackend
from tfcz_audio.router import Router

from .helpers import MINIMAL, fake_backend, minimal_config
from .test_api import ApiTestCase


class DrainedProcessTests(unittest.TestCase):
    def test_chatty_child_never_blocks(self):
        # writes 2 MB to stderr (far beyond the 64 KiB pipe buffer) then exits 0
        code = "import sys\nfor i in range(20000):\n    sys.stderr.write('line %d ' % i + 'x' * 90 + '\\n')\nsys.stderr.flush()\n"
        proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        wrapped = DrainedProcess(proc, keep_lines=5)
        deadline = time.monotonic() + 10
        while wrapped.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(wrapped.poll(), 0, "child blocked on a full stderr pipe")
        time.sleep(0.1)
        tail = wrapped.stderr_tail.splitlines()
        self.assertLessEqual(len(tail), 25, "bounded buffer")
        self.assertIn("line 0", tail[0], "the first lines are kept: a usage error names the cause there")
        self.assertIn("line 19999", tail[-1], "and the newest lines too")


class ConfigRecoveryTests(unittest.TestCase):
    def test_backup_is_written_and_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            save(minimal_config(), path)
            save(minimal_config(), path)  # second save creates .bak of the first
            self.assertTrue(path.with_suffix(".toml.bak").is_file())
            path.write_text("this is = not [valid toml")
            cfg, err = load_or_recover(path)
            self.assertIn("previous config", err)
            self.assertEqual(set(cfg.routes), {"a_to_b", "b_to_a", "hdmi_to_a", "a_to_obs"})
            self.assertEqual(cfg.path, path)

    def test_no_backup_gives_empty_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("[routes.x]\nfrom = 1\n")
            cfg, err = load_or_recover(path)
            self.assertIn("NO connections", err)
            self.assertEqual(cfg.routes, {})
            self.assertEqual(cfg.path, path)
            # the daemon still starts and reports the problem
            router = Router(cfg, fake_backend(), node_wait=0.1, sleep=lambda s: None)
            router.config_error = err
            router.start()
            codes = [p["code"] for p in router.problems()]
            self.assertEqual(codes[0], "config_invalid")
            self.assertTrue(router.status()["virtual_mic"]["present"], "OBS mic exists even without routes")

    def test_readonly_config_gives_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "ro"
            d.mkdir()
            path = d / "config.toml"
            save(minimal_config(), path)
            d.chmod(0o500)
            try:
                from tfcz_audio.config import ConfigError

                with self.assertRaises(ConfigError) as ctx:
                    save(minimal_config(), path)
                self.assertIn("permission", str(ctx.exception).lower())
            finally:
                d.chmod(0o700)


class GraphCacheTests(unittest.TestCase):
    def test_status_polls_share_one_pw_dump(self):
        backend = fake_backend()
        calls = {"n": 0}
        real = backend.graph

        def counted():
            calls["n"] += 1
            return real()

        backend.graph = counted
        clock = [100.0]
        router = Router(minimal_config(), backend, node_wait=0.1, sleep=lambda s: None, clock=lambda: clock[0])
        router.graph_cache_ttl = 0.4
        router.start()
        clock[0] += 1.0  # expire whatever start() cached
        calls["n"] = 0
        router.status(); router.status(); router.devices()
        self.assertEqual(calls["n"], 1)
        clock[0] += 1.0
        router.status()
        self.assertEqual(calls["n"], 2)
        # the supervisor never trusts the cache
        router.reconcile()
        self.assertEqual(calls["n"], 3)


class MeterFallbackTests(unittest.TestCase):
    def test_falls_back_when_pw_record_refuses_raw(self):
        """The command shape is found out by trying: an option pw-record does
        not know must not leave the user with empty bars and no explanation."""
        from tfcz_audio.pw import FakeProcess

        spawned = []

        class Refusing(FakeProcess):
            def __init__(self, cmd):
                super().__init__()
                self.stdout = None
                self.stderr_tail = "pw-record: unrecognized option '--raw'" if "--raw" in cmd else ""
                self.returncode = 1 if "--raw" in cmd else None

        def spawn(cmd):
            spawned.append(cmd)
            return Refusing(cmd)

        mm = meters.MeterManager(lambda: minimal_config(), spawn=spawn, resolved_getter=lambda: {"a_mic": "alsa_input.a"})
        mm.reconcile()   # starts with --raw
        mm.reconcile()   # notices the refusal and moves to the next shape
        mm.reconcile()   # starts again without it
        self.assertTrue(mm.enabled, "meters stay on")
        self.assertEqual(mm.shape_label(), "pw-record ohne --raw")
        self.assertTrue(any("--raw" in c for c in spawned))
        self.assertTrue(any("pw-record" in c[0] and "--raw" not in c for c in spawned), "retried without the refused option")

    def test_gives_up_with_the_real_error(self):
        from tfcz_audio.pw import FakeProcess

        class Broken(FakeProcess):
            def __init__(self, cmd):
                super().__init__()
                self.stdout = None
                self.stderr_tail = "pw-record: unknown option '--nonsense'"
                self.returncode = 1

        mm = meters.MeterManager(lambda: minimal_config(), spawn=Broken, resolved_getter=lambda: {"a_mic": "alsa_input.a"})
        for _ in range(10):  # every shape in turn, then give up
            mm.reconcile()
        self.assertFalse(mm.enabled)
        self.assertIn("--nonsense", mm.disabled_reason)
        entry = mm.problem()
        self.assertEqual(entry["code"], "meters_off")

    def test_wav_header_is_skipped(self):
        import struct

        fmt = b"\x01\x00\x02\x00" + b"\x00" * 12  # 16 bytes, as the chunk size says
        header = b"RIFF" + struct.pack("<I", 36) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", 8)
        self.assertEqual(len(header), 44)
        self.assertEqual(meters.wav_data_offset(header), 44)
        self.assertIsNone(meters.wav_data_offset(b"RIFF" + struct.pack("<I", 36) + b"WAVEfmt "))
        self.assertEqual(meters.wav_data_offset(b"\x01\x02\x03\x04"), 0, "raw stream starts at once")

    def test_parec_is_the_last_resort_shape(self):
        spec = meters.MeterSpec("a_mic", "alsa_input.a")
        cmd = spec.command(tool="parec")
        self.assertEqual(cmd[0], "parec")
        self.assertIn("--device=alsa_input.a", cmd)
        self.assertIn("--format=s16le", cmd)
        sink = meters.MeterSpec("a_out", "alsa_output.a", capture_sink=True)
        self.assertIn("--device=alsa_output.a.monitor", sink.command(tool="parec"))

    def test_meter_spec_has_no_passive_links(self):
        cmd = meters.MeterSpec("x", "node").command()
        joined = " ".join(cmd)
        self.assertNotIn('node.passive = "true"', joined)
        self.assertIn("node.dont-fallback", joined)


class CsrfTests(ApiTestCase):
    def test_cross_site_post_rejected(self):
        code, body = self.call("POST", "/routes/a_to_b/mute", headers={"Origin": "http://evil.example"})
        self.assertEqual(code, 403)
        code, body = self.call("POST", "/routes/a_to_b/mute", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(code, 403)

    def test_same_origin_and_no_origin_ok(self):
        host = self.base.split("//", 1)[1]
        code, body = self.call("POST", "/routes/a_to_b/mute", headers={"Origin": "http://" + host})
        self.assertEqual(code, 200)
        code, body = self.call("POST", "/routes/a_to_b/unmute")  # curl / Advanced Scene Switcher style
        self.assertEqual(code, 200)
        code, body = self.call("GET", "/status", headers={"Origin": "http://evil.example"})
        self.assertEqual(code, 200, "reads are harmless")

    def test_body_too_large(self):
        # the server answers 400 without reading the body; depending on timing the
        # client sees the 400 or a reset while still sending -- both are rejections
        try:
            code, body = self.call("PUT", "/routes/a_to_b", raw=b"x" * 1_000_001, headers={"Content-Type": "application/json"})
        except (urllib.error.URLError, ConnectionError, OSError, http.client.HTTPException):
            return  # the server closed the connection before we finished sending: also a rejection
        self.assertEqual(code, 400)

    def test_levels_reports_unavailable_meters(self):
        code, body = self.call("GET", "/levels")
        self.assertEqual(code, 200)
        self.assertFalse(body["available"])
        self.assertTrue(body["reason"])


class SingleInstanceTests(unittest.TestCase):
    def test_second_instance_refused(self):
        import os

        from tfcz_audio import cli

        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("XDG_RUNTIME_DIR")
            os.environ["XDG_RUNTIME_DIR"] = tmp
            try:
                first = cli._single_instance()
                with self.assertRaises(SystemExit) as ctx:
                    cli._single_instance()
                self.assertEqual(ctx.exception.code, 3)
                first.close()
                again = cli._single_instance()  # lock released with the handle
                again.close()
            finally:
                if old is None:
                    del os.environ["XDG_RUNTIME_DIR"]
                else:
                    os.environ["XDG_RUNTIME_DIR"] = old


class SelftestTests(unittest.TestCase):
    def test_selftest_runs_end_to_end(self):
        """The selftest is what the user runs when something sounds wrong; it
        must never crash, whatever the capture probe returns."""
        import argparse
        import io
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path as P

        from tfcz_audio import cli
        from tfcz_audio.config import save

        with tempfile.TemporaryDirectory() as tmp:
            path = P(tmp) / "config.toml"
            save(minimal_config(), path)
            args = argparse.Namespace(config=str(path), seconds=0.1, fake=True, verbose=False)
            probes = []

            def fake_probe(cmd, seconds=2.0, skip_wav=False):
                probes.append(cmd)
                # first device delivers audio; the next refuses --raw once and then
                # works, the rest fail with something that cannot be dropped
                if len(probes) == 1:
                    return cli.Probe(9600, 0.5, "", 0, {})
                if "--raw" in cmd and len(probes) == 2:
                    return cli.Probe(0, 0.0, "pw-record: unrecognized option '--raw'\nusage...", 1, {})
                if len(probes) == 3:
                    return cli.Probe(4800, 0.2, "", 0, {})
                return cli.Probe(0, 0.0, "pw-record: unrecognized option '--nope'", 1, {})

            cli._probe = fake_probe
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cli.cmd_selftest(args)
            text = out.getvalue()
        self.assertEqual(rc, 1, "reports a problem when devices deliver nothing")
        self.assertIn("=== devices ===", text)
        self.assertIn("NO DATA", text)
        self.assertIn("unrecognized option", text, "shows the real error from the helper")
        self.assertIn("retrying without it", text, "drops a refused option instead of giving up")
        self.assertTrue(any("--raw" not in c for c in probes), "retried in a reduced shape")
        self.assertIn("command:", text, "shows the exact command so it can be run by hand")
        self.assertIn("=== router streams ===", text)
        self.assertTrue(probes and probes[0][0] == "pw-record")


class HelperErrorCaptureTests(unittest.TestCase):
    def test_a_usage_dump_does_not_hide_its_first_line(self):
        """pw-record answers a bad option with the reason on line one and then
        the whole help text. Keeping the tail reports the help, not the cause."""
        from tfcz_audio.pw import DrainedProcess

        code = (
            "import sys\n"
            "sys.stderr.write(\"pw-record: unrecognized option '--raw'\\n\")\n"
            "for i in range(60):\n"
            "    sys.stderr.write('  --option-%d   some help text\\n' % i)\n"
        )
        proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        wrapped = DrainedProcess(proc)
        deadline = time.monotonic() + 10
        while wrapped.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)
        self.assertIn("unrecognized option", wrapped.stderr_head)
        self.assertTrue(wrapped.stderr_tail.startswith("pw-record: unrecognized option"))

    def test_every_shape_is_tried_before_giving_up(self):
        from tfcz_audio.pw import FakeProcess

        tried = []

        class Refusing(FakeProcess):
            def __init__(self, cmd):
                super().__init__()
                self.stdout = None
                tried.append(cmd)
                # refuse --raw, then -P, then succeed with the plainest shape
                if "--raw" in cmd:
                    self.stderr_tail = "pw-record: unrecognized option '--raw'\nusage: ..."
                    self.returncode = 1
                elif "-P" in cmd:
                    self.stderr_tail = "pw-record: unrecognized option '-P'\nusage: ..."
                    self.returncode = 1
                else:
                    self.stderr_tail = ""
                    self.returncode = None

        mm = meters.MeterManager(lambda: minimal_config(), spawn=Refusing, resolved_getter=lambda: {"a_mic": "alsa_input.a"})
        for _ in range(8):
            mm.reconcile()
        self.assertTrue(mm.enabled, "a working shape was found instead of switching the bars off")
        self.assertEqual(mm.shape_label(), "pw-record ohne --raw und ohne -P")
        self.assertTrue(any("--raw" not in c and "-P" not in c for c in tried))


class ForgottenStateTests(unittest.TestCase):
    """A daemon runs for weeks while an automation creates and deletes routes.
    Nothing may accumulate, and a name that comes back must start clean."""

    def _router(self, tmp):
        from pathlib import Path as P

        from tfcz_audio.config import save
        from tfcz_audio.router import Router

        path = P(tmp) / "config.toml"
        cfg = minimal_config()
        cfg.path = path
        save(cfg, path)
        router = Router(cfg, fake_backend(), node_wait=0.01, sleep=lambda s: None)
        router.start()
        return router

    def test_nothing_is_remembered_about_a_deleted_route(self):
        import tempfile

        from tfcz_audio import edit

        with tempfile.TemporaryDirectory() as tmp:
            router = self._router(tmp)
            try:
                for index in range(20):
                    name = f"tmp{index}"
                    edit.upsert_route(router, name, {"from": "a_mic", "to": "b_out", "volume": 0.4})
                    router.reconcile()
                    edit.delete_route(router, name)
                for attribute in router.PER_NAME_STATE:
                    left = [k for k in getattr(router, attribute) if k.startswith("tmp")]
                    self.assertEqual(left, [], f"{attribute} kept entries for deleted routes")
            finally:
                router.stop()

    def test_a_reused_name_does_not_inherit_the_old_notes(self):
        """'its volume is already applied' from a previous route of the same
        name would leave the new stream wherever it started."""
        import tempfile

        from tfcz_audio import edit

        with tempfile.TemporaryDirectory() as tmp:
            router = self._router(tmp)
            try:
                edit.upsert_route(router, "again", {"from": "a_mic", "to": "b_out", "volume": 0.4})
                router.reconcile()
                self.assertIn("again", router._applied)
                edit.delete_route(router, "again")
                self.assertNotIn("again", router._applied, "the note outlived the route")
                # the name comes back, and the new stream really carries the new volume
                edit.upsert_route(router, "again", {"from": "a_mic", "to": "b_out", "volume": 0.9})
                router.reconcile()
                self.assertAlmostEqual(router.route_status("again")["volume"], 0.9, places=3)
            finally:
                router.stop()


class ConnectionLimitTests(ApiTestCase):
    """The API may be put on the LAN. A client gone wrong must not be able to
    spawn threads until the machine gives up while the stream is running."""

    def test_connections_beyond_the_limit_are_refused_not_queued(self):
        import socket

        self.server.max_connections = 4
        held = []
        try:
            for _ in range(4):
                sock = socket.create_connection(self.server.server_address[:2], timeout=5)
                sock.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n")  # deliberately unfinished
                held.append(sock)
            extra = socket.create_connection(self.server.server_address[:2], timeout=5)
            held.append(extra)
            extra.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
            answer = extra.recv(200)
            self.assertIn(b"503", answer)
        finally:
            for sock in held:
                sock.close()

    def test_the_count_comes_back_down_so_refusals_are_not_permanent(self):
        self.server.max_connections = 4
        for _ in range(10):
            status, _ = self.call("GET", "/health")
            self.assertEqual(status, 200)
        self.assertLessEqual(self.server._connections, 1)


class StateFileTests(unittest.TestCase):
    """The saved volumes are the least important file in the system. Nothing in
    it may keep the daemon from starting -- without sound nobody can even reach
    the page that would explain the problem."""

    CASES = {
        "a list": "[1, 2, 3]",
        "a string": '"hello"',
        "a number": "42",
        "routes as a list": '{"routes": [1, 2]}',
        "routes as a string": '{"routes": "nope"}',
        "not json at all": "{broken",
        "empty": "",
        "NaN volume": '{"routes": {"a_to_b": {"volume": NaN}}}',
        "infinite volume": '{"routes": {"a_to_b": {"volume": 1e999}}}',
        "wrong types": '{"routes": {"a_to_b": {"volume": "loud", "mute": "maybe"}}}',
    }

    def test_no_shape_of_state_file_stops_the_daemon(self):
        import tempfile
        from pathlib import Path as P

        from tfcz_audio.router import Router

        for label, content in self.CASES.items():
            with tempfile.TemporaryDirectory() as tmp, self.subTest(label):
                state = P(tmp) / "state.json"
                state.write_text(content)
                router = Router(minimal_config(state_file=state), fake_backend(),
                                node_wait=0.01, sleep=lambda s: None)
                router.start()
                try:
                    router.reconcile()
                    volume = router.status()["routes"]["a_to_b"]["volume"]
                    self.assertEqual(volume, 1.0, f"{label}: fell back to the config value")
                finally:
                    router.stop()

    def test_a_usable_state_file_is_still_restored(self):
        import json
        import tempfile
        from pathlib import Path as P

        from tfcz_audio.router import Router

        with tempfile.TemporaryDirectory() as tmp:
            state = P(tmp) / "state.json"
            state.write_text(json.dumps({"routes": {"a_to_b": {"volume": 0.25, "mute": True}}}))
            router = Router(minimal_config(state_file=state), fake_backend(), node_wait=0.01, sleep=lambda s: None)
            router.start()
            try:
                status = router.status()["routes"]["a_to_b"]
                self.assertAlmostEqual(status["volume"], 0.25, places=3)
                self.assertTrue(status["mute"])
            finally:
                router.stop()


class PipeWireRestartTests(unittest.TestCase):
    """The audio system is restarted under the running daemon -- an update, a
    crash, `systemctl --user restart pipewire`. Every node comes back with a
    new id and every helper of ours is gone. Nobody is going to fix that by
    hand during a tournament."""

    def test_everything_comes_back_by_itself(self):
        from tfcz_audio.pw import PwError
        from tfcz_audio.router import Router

        clock = [1000.0]
        backend = fake_backend()
        router = Router(minimal_config(), backend, node_wait=0.01,
                        sleep=lambda s: None, clock=lambda: clock[0])
        router.start()
        for _ in range(3):
            router.reconcile()
        self.assertTrue(all(r["connected"] for r in router.status()["routes"].values()))

        real_graph = backend.graph
        down = [True]

        def flaky(*args, **kwargs):
            if down[0]:
                raise PwError("connection refused")
            return real_graph(*args, **kwargs)

        backend.graph = flaky
        try:
            for _ in range(3):
                clock[0] += 1.0
                router.reconcile()
            self.assertIn("cannot talk to PipeWire", router.last_error)
            self.assertTrue(any("Tonsystem" in p["title"] for p in router.problems()))

            # back up: same names, different ids, our own nodes gone
            graph = real_graph()
            survivors = [n for n in graph.nodes.values() if not n.name.startswith("tfcz.")]
            graph.nodes.clear()
            graph.links.clear()
            for node in survivors:
                node.id += 1000
                graph.nodes[node.id] = node
            for proc in router.procs.values():
                if hasattr(proc, "returncode"):
                    proc.returncode = 1
            down[0] = False

            for _ in range(15):
                clock[0] += 1.0
                router.reconcile()
                if all(r["connected"] for r in router.status()["routes"].values()):
                    break
            else:
                self.fail(f"did not recover: {router.status()['routes']}")
            self.assertEqual(router.last_error, "")
            self.assertEqual(router.problems(), [])
        finally:
            backend.graph = real_graph
            router.stop()

    def test_a_flapping_session_does_not_spin_the_helpers(self):
        """Restarting helpers as fast as PipeWire flaps would make it worse."""
        from tfcz_audio.pw import PwError
        from tfcz_audio.router import Router

        clock = [1000.0]
        backend = fake_backend()
        spawns = []
        real_spawn = backend.spawn_loopback
        backend.spawn_loopback = lambda spec: (spawns.append(spec.name), real_spawn(spec))[1]
        router = Router(minimal_config(), backend, node_wait=0.01,
                        sleep=lambda s: None, clock=lambda: clock[0])
        router.start()
        real_graph = backend.graph
        up = [True]
        backend.graph = lambda *a, **k: real_graph(*a, **k) if up[0] else (_ for _ in ()).throw(PwError("gone"))
        try:
            spawns.clear()
            for i in range(60):
                up[0] = i % 2 == 0
                clock[0] += 0.5
                router.reconcile()
            self.assertLess(len(spawns), 40, f"respawned {len(spawns)} times while the session flapped")
        finally:
            backend.graph = real_graph
            router.stop()
