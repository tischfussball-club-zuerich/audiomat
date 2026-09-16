"""Stability: pipes never block children, config recovery, CSRF guard, single instance, meter fallback."""

import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
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
        self.assertLessEqual(len(tail), 5)
        self.assertIn("line 19999", tail[-1])


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
    def test_disabled_without_raw_support(self):
        old = meters._RAW_SUPPORT
        meters._RAW_SUPPORT = False
        try:
            mm = meters.MeterManager(lambda: minimal_config())
            self.assertFalse(mm.enabled)
            self.assertIn("--raw", mm.disabled_reason)
            mm.reconcile()  # no-op, must not spawn anything
            self.assertEqual(mm.meters, {})
        finally:
            meters._RAW_SUPPORT = old

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
        except (urllib.error.URLError, ConnectionError):
            return
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

            def fake_probe(cmd, seconds=2.0):
                probes.append(cmd)
                # first device delivers audio, the rest fail like a broken pw-record
                if len(probes) == 1:
                    return 9600, 0.5, "", 0
                return 0, 0.0, "pw-record: unrecognized option '--nope'", 1

            cli._probe = fake_probe
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cli.cmd_selftest(args)
            text = out.getvalue()
        self.assertEqual(rc, 1, "reports a problem when devices deliver nothing")
        self.assertIn("=== devices ===", text)
        self.assertIn("NO DATA", text)
        self.assertIn("unrecognized option", text, "shows the real error from the helper")
        self.assertIn("command:", text, "shows the exact command so it can be run by hand")
        self.assertIn("=== router streams ===", text)
        self.assertTrue(probes and probes[0][0] == "pw-record")
