"""Regression tests for the findings of the independent stability review."""

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tfcz_audio import meters
from tfcz_audio.config import load_or_recover, save
from tfcz_audio.pw import FakeProcess
from tfcz_audio.router import Router, route_spec, stop_all, virtual_spec

from .helpers import fake_backend, minimal_config
from .test_api import ApiTestCase
from .test_router import make_router


class RestoreKeyTests(unittest.TestCase):
    def test_every_stream_has_its_own_restore_key(self):
        cfg = minimal_config()
        from tfcz_audio.pw import Graph
        from tfcz_audio.router import resolve_devices

        keys = set()
        for r in cfg.routes.values():
            spec = route_spec(cfg, r, resolve_devices(cfg, Graph()))
            for props in (spec.capture_props, spec.playback_props):
                key = (props["media.role"], props["application.id"], props["application.name"])
                self.assertNotIn(key, keys)
                keys.add(key)
        v = virtual_spec(cfg)
        self.assertNotEqual(v.capture_props["media.role"], v.playback_props["media.role"])


class SettleAndBudgetTests(unittest.TestCase):
    def test_new_stream_gets_its_volume_in_the_same_pass(self):
        router, backend = make_router()
        router.start()
        router.set_route("hdmi_to_a", volume=0.3, mute=True)
        router.procs["hdmi_to_a"].crash()
        clock = [100.0]
        router._clock = lambda: clock[0]
        router.reconcile()  # notices the exit
        clock[0] += 60
        backend.calls.clear()
        router.reconcile()  # respawns AND applies in the same pass
        node = backend.graph().by_name("tfcz.hdmi_to_a.out")
        self.assertEqual((node.volume, node.mute), (0.3, True))
        self.assertIn(("spawn", "tfcz.hdmi_to_a"), backend.calls)
        self.assertIn(("set_mute", node.id, True), backend.calls)

    def test_late_appearing_stream_is_settled_and_applied(self):
        router, backend = make_router()
        router.start()
        router.set_route("hdmi_to_a", volume=0.3, mute=True)
        # make spawns create their nodes only later, like real pw-loopback does
        real_spawn = backend.spawn_loopback
        deferred = []

        def slow_spawn(spec):
            proc = FakeProcess(on_exit=backend._on_exit)
            deferred.append((proc, spec))
            backend.processes[proc.pid] = (proc, spec)
            return proc

        def sleep_creates_nodes(_seconds):
            while deferred:
                proc, spec = deferred.pop()
                backend.processes.pop(proc.pid, None)
                real = real_spawn(spec)  # creates the nodes and links
                backend.processes[proc.pid] = (proc, spec)
                backend.processes.pop(real.pid, None)

        backend.spawn_loopback = slow_spawn
        router._sleep = sleep_creates_nodes
        router.procs["hdmi_to_a"].crash()
        clock = [100.0]
        router._clock = lambda: clock[0]
        router.reconcile()
        clock[0] += 60
        router.reconcile()  # respawn -> node absent -> settle poll -> node appears -> applied
        node = backend.graph().by_name("tfcz.hdmi_to_a.out")
        self.assertIsNotNone(node)
        self.assertEqual((node.volume, node.mute), (0.3, True))

    def test_capture_stream_forced_neutral(self):
        router, backend = make_router()
        router.start()
        cap = backend.graph().by_name("tfcz.a_to_b.in")
        cap.volume, cap.mute = 0.2, True  # e.g. restored by WirePlumber from an old session
        router._applied.pop("a_to_b", None)
        router.reconcile()
        cap = backend.graph().by_name("tfcz.a_to_b.in")
        self.assertEqual((cap.volume, cap.mute), (1.0, False))

    def test_apply_budget_bounds_a_pass(self):
        router, backend = make_router()
        router.apply_budget = 2
        router.start()
        for n in router.cfg.routes:
            router.desired[n].volume = 0.11
        router._applied.clear()
        backend.calls.clear()
        router.reconcile()
        self.assertEqual(sum(1 for c in backend.calls if c[0] == "set_volume"), 2)
        router.reconcile()
        self.assertEqual(sum(1 for c in backend.calls if c[0] == "set_volume"), 4)


class BackoffResetTests(unittest.TestCase):
    def test_pipewire_recovery_clears_backoff(self):
        from tfcz_audio.pw import PwError

        router, backend = make_router()
        router.start()
        clock = [100.0]
        router._clock = lambda: clock[0]
        real = backend.graph
        backend.graph = lambda: (_ for _ in ()).throw(PwError("down"))
        for p, _ in list(backend.processes.values()):
            p.crash()
        router.reconcile()  # PipeWire down: exits are not counted as helper failures
        self.assertTrue(all(router._retry_at[n] <= clock[0] + 1.0 for n in router.cfg.routes))
        self.assertEqual(router._failures, {})
        backend.graph = real
        clock[0] += 1.5
        before = router.pw_recovered_count
        router.reconcile()
        self.assertEqual(router.pw_recovered_count, before + 1)
        self.assertTrue(all(n in router.procs for n in router.cfg.routes))


class StopAllTests(unittest.TestCase):
    def test_shared_deadline(self):
        class Stubborn(FakeProcess):
            def wait(self, timeout=None):
                time.sleep(min(timeout or 0, 0.3))
                if self.returncode is None:
                    raise subprocess.TimeoutExpired("x", timeout)
                return self.returncode

            def terminate(self):
                pass  # ignores SIGTERM

            def kill(self):
                self.returncode = -9

        procs = [Stubborn() for _ in range(10)]
        t0 = time.monotonic()
        stop_all(procs, deadline=0.5)
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertTrue(all(p.returncode == -9 for p in procs))


class SaveOrderTests(unittest.TestCase):
    def test_live_file_never_disappears(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            save(minimal_config(), path)
            first = path.read_text()
            # make the tmp target unwritable: the live file must stay intact
            (path.with_suffix(".toml.tmp")).mkdir()
            from tfcz_audio.config import ConfigError

            with self.assertRaises(ConfigError):
                save(minimal_config(), path)
            self.assertEqual(path.read_text(), first)

    def test_missing_file_recovers_from_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            save(minimal_config(), path)
            save(minimal_config(), path)
            path.unlink()
            cfg, err = load_or_recover(path)
            self.assertIn("previous config", err)
            self.assertTrue(cfg.routes)


class MeterWatchTests(unittest.TestCase):
    def test_watch_validates_and_caps(self):
        spawned = []

        def spawn(cmd):
            spawned.append(cmd)
            return FakeProcess()

        known = {f"alsa_input.dev{i}" for i in range(50)}
        mm = meters.MeterManager(lambda: minimal_config(), spawn=spawn, resolved_getter=lambda: {}, known_nodes=lambda: known)
        added = mm.watch([{"name": "alsa_input.dev1"}, {"name": "not.a.device"}, {"name": 'alsa_input."quote"'}])
        self.assertEqual(added, ["alsa_input.dev1"])
        for i in range(2, 50):
            mm.watch([{"name": f"alsa_input.dev{i}"}])
        self.assertLessEqual(len(mm._watch), mm.MAX_WATCH)
        mm.watch([], seconds=0)
        self.assertEqual(mm._watch, {})
        self.assertNotIn("obs", {})  # obs meter always present regardless
        mm.stop()

    def test_spa_string_never_breaks_on_odd_names(self):
        spec = meters.MeterSpec('we"ird\\name', 'we"ird\\name')
        cmd = spec.command()
        spa = cmd[cmd.index("-P") + 1]
        # every quoted value inside the SPA dict is sanitised: no stray quote or backslash
        import re
        for value in re.findall(r'= "([^"]*)"', spa):
            self.assertNotIn('"', value)
            self.assertNotIn("\\", value)
        self.assertEqual(spa.count('"') % 2, 0)


class HostPinningTests(ApiTestCase):
    def test_foreign_host_rejected_on_loopback_listener(self):
        code, _ = self.call("POST", "/routes/a_to_b/mute", headers={"Host": "evil.example:8787"})
        self.assertEqual(code, 403)
        code, _ = self.call("POST", "/routes/a_to_b/mute", headers={"Host": "localhost:1"})
        self.assertEqual(code, 200)
        code, _ = self.call("POST", "/routes/a_to_b/unmute", headers={"Origin": "null"})
        self.assertEqual(code, 403)

    def test_health_is_cheap_and_honest(self):
        code, body = self.call("GET", "/health")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.router.last_error = "cannot talk to PipeWire: down"
        code, body = self.call("GET", "/health")
        self.assertFalse(body["ok"])
        self.router.last_error = ""

    def test_negative_length(self):
        code, _ = self.call("POST", "/routes/a_to_b/mute", raw=b"", headers={"Content-Length": "-5"})
        self.assertIn(code, (400,))


class ShutdownTests(unittest.TestCase):
    def test_fake_meter_manager_stop_signature(self):
        mm = meters.FakeMeterManager(lambda: minimal_config(), lambda: {})
        mm.stop(deadline=3.0)
        mm.stop()

    def test_fake_daemon_starts_and_exits_cleanly(self):
        """End-to-end: `run --fake` on a starter config, SIGTERM, exit code 0, no traceback."""
        import os
        import signal
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "starter.toml"
            cfg.write_text(f"[api]\nport = {port}\n")
            env = dict(os.environ, XDG_RUNTIME_DIR=tmp, XDG_STATE_HOME=tmp)
            proc = subprocess.Popen(
                [sys.executable, "-m", "tfcz_audio", "-c", str(cfg), "run", "--fake", "--node-wait", "0.2"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=str(Path(__file__).resolve().parents[1]),
            )
            try:
                deadline = time.monotonic() + 15
                ready = False
                while time.monotonic() < deadline:
                    with socket.socket() as probe:
                        probe.settimeout(0.2)
                        if probe.connect_ex(("127.0.0.1", port)) == 0:
                            ready = True
                            break
                    time.sleep(0.1)
                self.assertTrue(ready, "daemon did not open its port")
                proc.send_signal(signal.SIGTERM)
                out = proc.communicate(timeout=15)[0].decode()
            finally:
                if proc.poll() is None:
                    proc.kill()
            self.assertEqual(proc.returncode, 0, out[-2000:])
            self.assertNotIn("Traceback", out)
            self.assertIn("stopped", out)


class SecondReviewTests(unittest.TestCase):
    def test_connected_requires_links_to_the_right_devices(self):
        router, backend = make_router()
        router.start()
        st = router.route_status("a_to_b")
        self.assertTrue(st["connected"])
        self.assertEqual(st["misrouted_to"], [])
        # simulate WirePlumber moving the playback stream to another sink (remembered manual move)
        g = backend.graph()
        play = g.by_name("tfcz.a_to_b.out")
        g.links = [l for l in g.links if l.output_node != play.id]
        backend.link("tfcz.a_to_b.out", "alsa_output.a")
        st = router.route_status("a_to_b")
        self.assertFalse(st["connected"])
        self.assertEqual(st["misrouted_to"], ["alsa_output.a"])
        codes = {p["code"]: p for p in router.problems()}
        self.assertIn("misrouted", codes)
        self.assertEqual(codes["misrouted"]["level"], "error")

    def test_volume_drift_is_corrected_but_not_fought_every_tick(self):
        router, backend = make_router()
        router.start()
        clock = [100.0]
        router._clock = lambda: clock[0]
        node = backend.graph().by_name("tfcz.hdmi_to_a.out")
        node.volume = 1.0  # something else changed it after we applied 0.6
        backend.calls.clear()
        router.reconcile()
        self.assertEqual(backend.graph().by_name("tfcz.hdmi_to_a.out").volume, 0.6)
        self.assertTrue(any(c[0] == "set_volume" for c in backend.calls))
        # drift again immediately: we wait 5 s before re-applying instead of fighting
        backend.graph().by_name("tfcz.hdmi_to_a.out").volume = 1.0
        backend.calls.clear()
        router.reconcile()
        self.assertFalse(any(c[0] == "set_volume" for c in backend.calls))
        clock[0] += 6
        router.reconcile()
        self.assertTrue(any(c[0] == "set_volume" for c in backend.calls))

    def test_system_default_into_obs_mic_is_reported(self):
        router, backend = make_router()
        router.start()
        backend.graph().defaults["default.audio.sink"] = "tfcz.obsmix"
        codes = {p["code"]: p for p in router.problems()}
        self.assertIn("default_into_obs", codes)
        self.assertEqual(codes["default_into_obs"]["level"], "error")
        backend.graph().defaults["default.audio.sink"] = "alsa_output.a"
        self.assertNotIn("default_into_obs", {p["code"] for p in router.problems()})

    def test_parse_dump_reads_default_metadata(self):
        import json

        from tfcz_audio.pw import parse_dump

        dump = json.dumps([
            {"id": 30, "type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "default"},
             "metadata": [{"subject": 0, "key": "default.audio.sink", "type": "Spa:String:JSON", "value": {"name": "tfcz.obsmix"}},
                          {"subject": 0, "key": "default.audio.source", "type": "Spa:String:JSON", "value": {"name": "alsa_input.x"}}]},
        ])
        g = parse_dump(dump)
        self.assertEqual(g.defaults["default.audio.sink"], "tfcz.obsmix")
        self.assertEqual(g.defaults["default.audio.source"], "alsa_input.x")

    def test_setup_rejects_headset_output_as_game_and_names_descriptions(self):
        from tfcz_audio import edit
        from tfcz_audio.config import ConfigError
        from .test_identity import twin_backend
        from tfcz_audio.config import parse
        import tomllib
        from .helpers import MINIMAL

        cfg = parse(tomllib.loads(MINIMAL)); cfg.state_file = None
        router = Router(cfg, twin_backend(), node_wait=0.1, sleep=lambda s: None)
        router.start()
        a = {"mic": "alsa_input.usb-Logitech-01.mono-fallback", "out": "alsa_output.usb-Logitech-01.analog-stereo", "label": "Anna"}
        b = {"mic": "alsa_input.usb-Jabra_A1B2-00.mono-fallback", "out": "alsa_output.usb-Jabra_A1B2-00.analog-stereo", "label": "Ben"}
        with self.assertRaises(ConfigError):
            edit.setup(router, {"headset_a": a, "headset_b": b, "game": b["out"]})
        st = edit.setup(router, {"headset_a": a, "headset_b": b, "game": "alsa_input.pci-0000_03_00.0.hws-1", "game_label": "Switch"})
        self.assertEqual(router.cfg.routes["a_to_b"].description, "Anna talks to Ben")
        self.assertEqual(router.cfg.routes["game_to_b"].description, "Switch for Ben")
        self.assertTrue(st["routes"]["game_to_b"]["connected"])


class SystemEdgeTests(unittest.TestCase):
    def test_tool_output_decodes_regardless_of_locale(self):
        import os

        from tfcz_audio.pw import PipeWireBackend

        env = dict(os.environ, LC_ALL="C", LANG="C", PYTHONIOENCODING="utf-8")
        b = PipeWireBackend()
        # run through the same _run as pw-dump; a non-ASCII device description must not raise
        out = b._run([sys.executable, "-c", "import sys; sys.stdout.buffer.write('Kopfh\\u00f6rer \\u00e9\\n'.encode('utf-8'))"])
        self.assertIn("Kopfhörer", out)
        out = b._run([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff\\xfe bad bytes\\n')"])
        self.assertIn("bad bytes", out)

    def test_fallback_to_wrong_source_is_muted_for_safety(self):
        router, backend = make_router()
        router.start()
        g = backend.graph()
        # headset A unplugged, WirePlumber (ignoring dont-fallback) links the capture stream to the OBS mic: a loop
        backend.remove_node("alsa_input.a")
        backend.link("tfcz.obsmic", "tfcz.a_to_obs.in")
        router.reconcile()
        st = router.route_status("a_to_obs")
        self.assertIn(st["misrouted_to"][0], ("tfcz.obsmic", "TFCZ OBS Mic"))
        self.assertTrue(st["safety_muted"])
        self.assertFalse(st["connected"])
        self.assertTrue(backend.graph().by_name("tfcz.a_to_obs.out").mute, "stream muted at the sink side")
        self.assertFalse(router.desired["a_to_obs"].mute, "the user's own setting is untouched")
        codes = {p["code"]: p for p in router.problems()}
        self.assertIn("misrouted", codes)
        self.assertIn("feed the OBS microphone", codes["misrouted"]["effect"])
        # device comes back and the wrong link goes away: safety mute lifted
        g = backend.graph()
        cap = g.by_name("tfcz.a_to_obs.in")
        g.links = [l for l in g.links if l.input_node != cap.id]
        backend.add_device("alsa_input.a", "Audio/Source")
        router.reconcile()  # resolves again and respawns / relinks
        router.reconcile()
        self.assertFalse(router.route_status("a_to_obs")["safety_muted"])
        self.assertFalse(backend.graph().by_name("tfcz.a_to_obs.out").mute)

    def test_virtual_nodes_are_kept_at_unity(self):
        router, backend = make_router()
        router.start()
        clock = [100.0]
        router._clock = lambda: clock[0]
        mic = backend.graph().by_name("tfcz.obsmic")
        mic.mute, mic.volume = True, 0.2
        router.reconcile()
        mic = backend.graph().by_name("tfcz.obsmic")
        self.assertEqual((mic.mute, mic.volume), (False, 1.0))
        # not fought every tick
        mic.mute = True
        backend.calls.clear()
        router.reconcile()
        self.assertFalse(any(c[0] == "set_mute" and c[1] == mic.id for c in backend.calls))
        clock[0] += 6
        router.reconcile()
        self.assertFalse(backend.graph().by_name("tfcz.obsmic").mute)

    def test_config_rejects_routing_our_own_nodes(self):
        import tomllib

        from tfcz_audio.config import ConfigError, parse

        from .helpers import MINIMAL

        with self.assertRaises(ConfigError):
            parse(tomllib.loads(MINIMAL + '[routes.loop]\nfrom = "tfcz.obsmic"\nto = "a_out"\n'))
        with self.assertRaises(ConfigError):
            parse(tomllib.loads(MINIMAL + '[routes.loop2]\nfrom = "a_mic"\nto = "tfcz.obsmix"\n'))

    def test_graph_parse_failure_behaves_like_unreachable(self):
        from tfcz_audio.pw import PwError

        router, backend = make_router()
        router.start()
        backend.graph = lambda: (_ for _ in ()).throw(ValueError("garbage"))
        router.reconcile()  # must not raise
        self.assertIn("cannot read", router.last_error)
        code, _ = (200, None)
        st = router.status()  # empty graph -> problems, no exception
        self.assertEqual(st["problems"][0]["code"], "no_audio_system")


class SelfHealingTests(unittest.TestCase):
    def strip_links(self, backend, route="a_to_b"):
        g = backend.graph()
        cap = g.by_name(f"tfcz.{route}.in")
        play = g.by_name(f"tfcz.{route}.out")
        g.links = [l for l in g.links
                   if (cap is None or l.input_node != cap.id) and (play is None or l.output_node != play.id)]

    def test_relink_watchdog_recycles_a_stuck_route(self):
        router, backend = make_router()
        router.start()
        clock = [100.0]
        router._clock = lambda: clock[0]
        self.strip_links(backend)
        before = router.procs["a_to_b"]
        router.reconcile()  # notices it is unlinked and starts the timer
        self.assertIs(router.procs["a_to_b"], before, "no restart before the grace period")
        self.assertFalse(router.route_status("a_to_b")["connected"])
        clock[0] += 10  # longer than relink_after
        router.reconcile()
        self.assertIsNot(router.procs["a_to_b"], before, "loopback recycled")
        self.assertTrue(router.route_status("a_to_b")["connected"])
        clock[0] += 1
        router.reconcile()  # the next pass sees it linked again and forgets the attempt
        self.assertNotIn("a_to_b", router._relink_attempts)
        self.assertNotIn("a_to_b", router._unlinked_since)

    def test_relink_gives_up_and_reports_instead_of_looping(self):
        router, backend = make_router()
        router.start()
        clock = [100.0]
        router._clock = lambda: clock[0]
        self.strip_links(backend)
        router.reconcile()
        for _ in range(3):
            clock[0] += 40
            self.strip_links(backend)
            router.reconcile()
        self.assertEqual(router._relink_attempts["a_to_b"], 3)
        clock[0] += 40
        self.strip_links(backend)
        proc = router.procs["a_to_b"]
        router.reconcile()
        self.assertIs(router.procs["a_to_b"], proc, "stopped recycling after 3 attempts")
        problems = {p["code"]: p for p in router.problems()}
        self.assertIn("not_linking", problems)
        self.assertIn("restart the audio system", problems["not_linking"]["fix"].lower())

    def test_pass_deadline_stops_volume_work(self):
        router, backend = make_router()
        router.start()
        for n in router.cfg.routes:
            router.desired[n].volume = 0.11
        router._applied.clear()
        router.max_pass_seconds = 0.0
        backend.calls.clear()
        router.reconcile()
        self.assertFalse(any(c[0] == "set_volume" for c in backend.calls), "no volume work once the pass budget is spent")
        router.max_pass_seconds = 6.0
        router.reconcile()
        self.assertTrue(any(c[0] == "set_volume" for c in backend.calls))

    def test_recycles_are_budgeted_but_respawns_are_not(self):
        router, backend = make_router()
        router.start()
        # every loopback dies at once (PipeWire restart): all come back in one pass
        for proc, _ in list(backend.processes.values()):
            proc.crash()
        clock = [100.0]
        router._clock = lambda: clock[0]
        router.reconcile()
        clock[0] += 60
        router.reconcile()
        self.assertTrue(all(n in router.procs for n in router.cfg.routes))
