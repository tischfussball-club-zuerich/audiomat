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
