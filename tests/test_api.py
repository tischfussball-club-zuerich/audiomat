import json
import threading
import unittest
import urllib.error
import urllib.request

from tfcz_audio.api import BadRequest, extract_route_params, parse_bool, serve
from tfcz_audio.router import Router

from .helpers import fake_backend, minimal_config


class ApiTestCase(unittest.TestCase):
    token = ""

    def setUp(self):
        cfg = minimal_config()
        self.backend = fake_backend()
        self.router = Router(cfg, self.backend, node_wait=0.1, sleep=lambda s: None)
        self.router.start()
        self.server = serve(self.router, "127.0.0.1", 0, self.token)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.router.stop()

    def call(self, method, path, body=None, headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())


class ApiTests(ApiTestCase):
    def test_health_and_status(self):
        code, body = self.call("GET", "/health")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        code, body = self.call("GET", "/status")
        self.assertEqual(code, 200)
        self.assertIn("a_to_b", body["routes"])
        self.assertTrue(body["virtual_mic"]["present"])

    def test_routes_listing_and_single(self):
        code, body = self.call("GET", "/routes")
        self.assertEqual(code, 200)
        self.assertEqual(body["routes"]["hdmi_to_a"]["volume"], 0.6)
        code, body = self.call("GET", "/routes/hdmi_to_a")
        self.assertEqual(body["route"]["name"], "hdmi_to_a")
        code, body = self.call("GET", "/routes/nope")
        self.assertEqual(code, 404)

    def test_put_json_body(self):
        code, body = self.call("PUT", "/routes/hdmi_to_a", {"volume": 0.25, "mute": True})
        self.assertEqual(code, 200)
        self.assertEqual(body["route"]["volume"], 0.25)
        self.assertTrue(body["route"]["mute"])
        self.assertEqual(self.backend.graph().by_name("tfcz.hdmi_to_a.out").volume, 0.25)

    def test_post_query_params_no_body(self):
        code, body = self.call("POST", "/routes/hdmi_to_a?volume=0.5&mute=false")
        self.assertEqual(code, 200)
        self.assertEqual(body["route"]["volume"], 0.5)
        self.assertFalse(body["route"]["mute"])

    def test_path_variants(self):
        code, body = self.call("POST", "/routes/hdmi_to_a/volume/0.1")
        self.assertEqual(code, 200)
        self.assertEqual(body["route"]["volume"], 0.1)
        code, body = self.call("POST", "/routes/hdmi_to_a/volume_db/-6")
        self.assertEqual(code, 200)
        self.assertAlmostEqual(body["route"]["volume_db"], -6.0, places=1)
        code, body = self.call("POST", "/routes/hdmi_to_a/mute")
        self.assertTrue(body["route"]["mute"])
        code, body = self.call("POST", "/routes/hdmi_to_a/toggle")
        self.assertFalse(body["route"]["mute"])
        code, body = self.call("POST", "/routes/hdmi_to_a/unmute")
        self.assertFalse(body["route"]["mute"])

    def test_form_encoded_body(self):
        code, body = self.call(
            "POST", "/routes/a_to_b", raw=b"volume=0.7", headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        self.assertEqual(code, 200)
        self.assertEqual(body["route"]["volume"], 0.7)

    def test_bad_requests(self):
        code, body = self.call("PUT", "/routes/a_to_b", {"volume": 5})
        self.assertEqual(code, 400)
        code, body = self.call("PUT", "/routes/a_to_b", {})
        self.assertEqual(code, 400)
        code, body = self.call("PUT", "/routes/a_to_b", raw=b"not json", headers={"Content-Type": "application/json"})
        self.assertEqual(code, 400)
        code, body = self.call("POST", "/routes/a_to_b?mute=maybe")
        self.assertEqual(code, 400)
        code, body = self.call("GET", "/nothing/here")
        self.assertEqual(code, 404)

    def test_presets(self):
        code, body = self.call("GET", "/presets")
        self.assertEqual(code, 200)
        self.assertIn("quiet", body["presets"])
        code, body = self.call("POST", "/presets/quiet")
        self.assertEqual(code, 200)
        self.assertEqual(body["preset"], "quiet")
        self.assertEqual(body["routes"]["hdmi_to_a"]["volume"], 0.2)
        code, body = self.call("POST", "/presets/nope")
        self.assertEqual(code, 404)

    def test_reset_and_devices(self):
        self.call("POST", "/routes/hdmi_to_a/volume/0.1")
        code, body = self.call("POST", "/reset")
        self.assertEqual(body["routes"]["hdmi_to_a"]["volume"], 0.6)
        code, body = self.call("GET", "/devices")
        self.assertEqual(code, 200)
        self.assertTrue(any(d["name"] == "alsa_input.hdmi" for d in body["devices"]))


class ApiTokenTests(ApiTestCase):
    token = "s3cret"

    def test_token_required(self):
        code, _ = self.call("GET", "/status")
        self.assertEqual(code, 401)
        code, _ = self.call("GET", "/status", headers={"Authorization": "Bearer s3cret"})
        self.assertEqual(code, 200)
        code, _ = self.call("POST", "/routes/a_to_b/mute?token=s3cret")
        self.assertEqual(code, 200)
        code, _ = self.call("POST", "/routes/a_to_b/mute?token=wrong")
        self.assertEqual(code, 401)


class ParamTests(unittest.TestCase):
    def test_parse_bool(self):
        self.assertTrue(parse_bool("on"))
        self.assertFalse(parse_bool("0"))
        self.assertTrue(parse_bool(True))
        with self.assertRaises(BadRequest):
            parse_bool("nah")

    def test_extract(self):
        self.assertEqual(extract_route_params({"volume": "0.5"}), (0.5, None))
        self.assertEqual(extract_route_params({"mute": "true"}), (None, True))
        vol, _ = extract_route_params({"volume_db": 0})
        self.assertEqual(vol, 1.0)
        with self.assertRaises(BadRequest):
            extract_route_params({"volume": "abc"})


class ConfigApiTests(ApiTestCase):
    def test_ui_served(self):
        for path in ("/", "/ui"):
            req = urllib.request.Request(self.base + path)
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("text/html", resp.headers["Content-Type"])
                body = resp.read().decode()
        self.assertIn("<title>TFCZ Audio</title>", body)
        self.assertIn("/config/devices", body)

    def test_get_config(self):
        code, body = self.call("GET", "/config")
        self.assertEqual(code, 200)
        self.assertEqual(body["routes"]["a_to_obs"]["to"], "obs_mic")
        self.assertIn("devices", body)
        self.assertNotIn("token", body["api"])

    def test_route_crud_via_api(self):
        code, body = self.call("PUT", "/config/routes/hdmi_to_b", {"from": "hdmi", "to": "b_out", "volume": 0.3})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["routes"]["hdmi_to_b"]["volume"], 0.3)
        self.assertIsNotNone(self.backend.graph().by_name("tfcz.hdmi_to_b.out"))
        code, body = self.call("PUT", "/config/routes/hdmi_to_b", {"from": "hdmi"})
        self.assertEqual(code, 400)
        code, body = self.call("DELETE", "/config/routes/hdmi_to_b")
        self.assertEqual(code, 200)
        self.assertNotIn("hdmi_to_b", body["routes"])
        code, body = self.call("DELETE", "/config/routes/hdmi_to_b")
        self.assertEqual(code, 404)

    def test_devices_and_defaults_via_api(self):
        self.backend.add_device("alsa_input.hdmi2", "Audio/Source")
        code, body = self.call("GET", "/config")
        devices = dict(body["devices"], hdmi="alsa_input.hdmi2")
        code, body = self.call("PUT", "/config/devices", devices)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["routes"]["hdmi_to_a"]["from_node"], "alsa_input.hdmi2")
        self.call("PUT", "/routes/hdmi_to_a", {"volume": 0.11})
        code, body = self.call("POST", "/config/save-defaults")
        self.assertEqual(code, 200)
        code, body = self.call("GET", "/config")
        self.assertEqual(body["routes"]["hdmi_to_a"]["volume"], 0.11)

    def test_preset_crud_via_api(self):
        code, body = self.call("PUT", "/config/presets/night", {"hdmi_to_a": {"mute": True}, "a_to_b": 0.5})
        self.assertEqual(code, 200, body)
        code, body = self.call("GET", "/presets")
        self.assertEqual(body["presets"]["night"]["a_to_b"], {"volume": 0.5})
        code, body = self.call("DELETE", "/config/presets/night")
        self.assertEqual(code, 200)
        code, body = self.call("DELETE", "/config/presets/night")
        self.assertEqual(code, 404)


class AudioBufferTests(ApiTestCase):
    def test_read_and_change_the_system_buffer(self):
        code, body = self.call("GET", "/audio")
        self.assertEqual(code, 200)
        self.assertEqual(body["quantum"], 1024, "falls back to the PipeWire default when nothing is set")
        self.assertFalse(body["forced"])
        self.assertEqual(body["router_request"], "auto", "the router itself asks for nothing")
        self.assertIn({"frames": 0, "ms": None}, body["choices"])

        code, body = self.call("PUT", "/audio", {"quantum": 512})
        self.assertEqual(code, 200)
        self.assertTrue(body["forced"])
        self.assertEqual(body["quantum"], 512)
        self.assertAlmostEqual(body["ms"], 10.7, places=1)
        self.assertIn(("force_quantum", 512), self.backend.calls)

        code, body = self.call("PUT", "/audio", {"quantum": 0})
        self.assertEqual(code, 200)
        self.assertIn(("force_quantum", 0), self.backend.calls)

    def test_invalid_buffer_sizes_are_refused(self):
        for value in (777, "big", None):
            code, _ = self.call("PUT", "/audio", {"quantum": value})
            self.assertEqual(code, 400, f"{value} must be refused")

    def test_dropout_check(self):
        code, body = self.call("POST", "/audio/dropouts", {"seconds": 1})
        self.assertEqual(code, 200)
        self.assertTrue(body["available"])
        self.assertEqual(body["errors"], 0)


class DiagnosticsAndLogTests(ApiTestCase):
    def test_diagnostics_run_in_the_background(self):
        import time as _t

        code, body = self.call("GET", "/diagnostics")
        self.assertEqual(code, 200)
        self.assertFalse(body["running"])
        self.assertEqual(body["kind"], "")

        code, body = self.call("POST", "/diagnostics", {"kind": "selftest"})
        self.assertEqual(code, 200)
        self.assertEqual(body["kind"], "selftest")

        deadline = _t.monotonic() + 30
        while _t.monotonic() < deadline:
            code, body = self.call("GET", "/diagnostics")
            if not body["running"]:
                break
            _t.sleep(0.2)
        self.assertFalse(body["running"], "the check finished")
        self.assertIn("=== devices ===", body["output"])
        self.assertIsNotNone(body["rc"])

    def test_unknown_check_is_refused(self):
        code, _ = self.call("POST", "/diagnostics", {"kind": "rm -rf"})
        self.assertEqual(code, 400)

    def test_logs_come_from_the_ring_buffer(self):
        import logging

        from tfcz_audio import logbuf

        logbuf.install()
        logging.disable(logging.NOTSET)  # the suite silences logging globally
        try:
            logging.getLogger("tfcz.test").warning("a warning for the UI")
            logging.getLogger("tfcz.test").info("an info line")
        finally:
            logging.disable(logging.CRITICAL)
        code, body = self.call("GET", "/logs?level=WARNING&limit=50")
        self.assertEqual(code, 200)
        self.assertEqual(body["source"], "memory")
        messages = [e["message"] for e in body["entries"]]
        self.assertIn("a warning for the UI", messages)
        self.assertNotIn("an info line", messages)
        code, body = self.call("GET", "/logs?level=INFO&limit=50")
        self.assertIn("an info line", [e["message"] for e in body["entries"]])
        for entry in body["entries"]:
            self.assertIn(entry["level"], logbuf.LEVELS)


class AnalysisTests(ApiTestCase):
    def test_analysis_reads_the_whole_graph(self):
        sample = """S   ID  QUANT   RATE    WAIT    BUSY   W/Q   B/Q  ERR FORMAT           NAME
R  114    128  48000    +++    1.0us  +++   0.00  570    S16LE 2 48000 alsa_input.hdmi
R   42      0      0  46.6us  23.0us  0.02  0.01  232363         F32P 1 0  + capture.headset-clean
R  221    256  48000  10.1us   0.5us  0.00  0.00  30         F32P 2 0  + tfcz.obsmix
"""
        self.backend.dropouts = lambda seconds=2.0: __import__("tfcz_audio.pw", fromlist=["x"]).parse_pw_top(sample)
        # a filter-chain node that belongs to nobody, plus a capture card
        self.backend.add_device("capture.headset-clean", "Audio/Source/Virtual")
        self.backend.add_physical("HWS", "pci", "alsa_input.hdmi", None, extra={"alsa.card_name": "HWS", "api.alsa.card": "1"})
        code, body = self.call("POST", "/analysis", {"seconds": 1})
        self.assertEqual(code, 200)
        self.assertTrue(body["available"])
        by_name = {r["name"]: r for r in body["rows"]}
        self.assertEqual(by_name["capture.headset-clean"]["category"], "filter")
        self.assertEqual(by_name["tfcz.obsmix"]["category"], "router")
        self.assertEqual(by_name["alsa_input.hdmi"]["category"], "device")
        self.assertEqual(body["rows"][0]["name"], "capture.headset-clean", "worst offender first")
        titles = " | ".join(f["title"] for f in body["findings"])
        self.assertIn("verlorene Tonpakete", titles)
        self.assertIn("Filterkette", titles, "names the foreign filter chain as the main source")
        self.assertIn("Aufnahmekarte gibt den Takt vor", titles)
        self.assertEqual(body["totals"]["filter"], 232363)
        self.assertLess(body["totals"]["router"], 100)

    def test_analysis_without_pw_top(self):
        from tfcz_audio.pw import PwError

        def boom(seconds=2.0):
            raise PwError("pw-top not found")

        self.backend.dropouts = boom
        code, body = self.call("POST", "/analysis", {"seconds": 1})
        self.assertEqual(code, 200)
        self.assertFalse(body["available"])
        self.assertIn("pw-top", body["findings"][0]["why"])


class ClassificationTests(unittest.TestCase):
    def test_nodes_missing_from_the_dump_are_still_classified(self):
        """pw-top and pw-dump are two separate measurements; a node can appear in
        one and not the other, and the table must still say who owns it."""
        from tfcz_audio.pw import Graph, classify_node

        g = Graph()
        cases = {
            "capture.headset-left-clean": "filter",
            "playback.headset-right-sidetone": "filter",
            "effect_input.rnnoise": "filter",
            "alsa_output.headset-right": "device",
            "v4l2_input.pci-0000_02_00.0": "device",
            "tfcz.a_to_b.out": "router",
            "OBS": "app",
            "Dummy-Driver": "system",
        }
        for name, want in cases.items():
            self.assertEqual(classify_node(name, None, g), want, name)


class BuildIdentityTests(ApiTestCase):
    def test_health_names_the_page_build(self):
        """'I don't see that section' must be answerable without guessing."""
        from tfcz_audio.api import ui_build

        code, body = self.call("GET", "/health")
        self.assertEqual(code, 200)
        self.assertEqual(body["build"], ui_build())
        self.assertRegex(body["build"], r"^[0-9a-f]{8}$")
        # the served page carries the field that displays it (HTML, not JSON)
        req = urllib.request.Request(self.base + "/")
        with urllib.request.urlopen(req, timeout=5) as resp:
            page = resp.read().decode()
        self.assertIn('id="build"', page)
        self.assertIn("Was läuft im Tonsystem", page, "the analysis section ships with the page")


class ModulePathTests(ApiTestCase):
    def test_health_names_the_loaded_copy(self):
        """A restart re-runs the installed copy; the daemon must say which one
        it loaded so a stale install is obvious."""
        from pathlib import Path

        import tfcz_audio

        code, body = self.call("GET", "/health")
        self.assertEqual(code, 200)
        self.assertEqual(Path(body["module"]), Path(tfcz_audio.__file__).resolve().parent)


class AudioConfigTests(ApiTestCase):
    def test_release_the_forced_buffer_size(self):
        """A request from this router drags the whole graph down, so it must be
        removable from the page."""
        self.router.cfg.audio.latency = "256/48000"
        code, body = self.call("GET", "/audio")
        self.assertEqual(body["router_request"], "256/48000")
        code, body = self.call("PUT", "/config/audio", {"latency": "auto"})
        self.assertEqual(code, 200, body)
        self.assertEqual(self.router.cfg.audio.latency, "auto")
        code, body = self.call("GET", "/audio")
        self.assertEqual(body["router_request"], "auto")

    def test_meters_can_be_switched_off_from_the_page(self):
        code, body = self.call("PUT", "/config/audio", {"meters": False})
        self.assertEqual(code, 200, body)
        self.assertFalse(self.router.cfg.audio.meters)

    def test_invalid_latency_is_refused(self):
        code, _ = self.call("PUT", "/config/audio", {"latency": "sehr klein"})
        self.assertEqual(code, 400)


class DeltaMeasurementTests(unittest.TestCase):
    def test_old_nodes_do_not_outrank_nodes_failing_now(self):
        """ERR is cumulative since a node started, so a node up since boot looks
        worse than one restarted a minute ago. Only the change matters."""
        from tfcz_audio.pw import parse_pw_top

        head = "S   ID  QUANT   RATE    WAIT    BUSY   W/Q   B/Q  ERR FORMAT           NAME\n"
        old_busy = "R   42      0      0  13.9us   9.1us  0.01  0.00  {}         F32P 1 0  + capture.headset-left-clean\n"
        fresh = "R  191    256  48000  25.8us  13.1us  0.01  0.00  {}         F32P 2 0  + tfcz.obsmix\n"
        text = head + old_busy.format(528000) + fresh.format(200) + head + old_busy.format(528002) + fresh.format(260)
        r = parse_pw_top(text)
        self.assertEqual(r["samples"], 2)
        self.assertEqual(r["errors"], 528262, "totals are still reported")
        self.assertEqual(r["delta"], 62, "but the comparison uses what changed")
        self.assertEqual(r["nodes"][0]["name"], "tfcz.obsmix")

    def test_single_sample_falls_back_to_totals(self):
        from tfcz_audio.pw import parse_pw_top

        text = ("S   ID  QUANT   RATE    WAIT    BUSY   W/Q   B/Q  ERR FORMAT           NAME\n"
                "R   42      0      0  13.9us   9.1us  0.01  0.00  99         F32P 1 0  + capture.x\n")
        r = parse_pw_top(text)
        self.assertEqual(r["samples"], 1)
        self.assertEqual(r["delta"], 99)


class AdvancedTabsTests(ApiTestCase):
    def test_every_control_still_ships_with_the_page(self):
        """The Advanced section is split into tabs; no control may go missing in
        the move, and the script must not reference an element that is gone."""
        import re
        import urllib.request

        with urllib.request.urlopen(self.base + "/", timeout=5) as resp:
            page = resp.read().decode()
        for tab in ("setup", "sound", "diag", "system"):
            self.assertIn(f'data-tab="{tab}"', page)
        for control in ("labels", "devices", "r-from-mount", "save-defaults", "quantum-mount",
                        "dropout-check", "run-doctor", "run-selftest", "run-analysis",
                        "log-level-mount", "token", "cfgpath", "build"):
            self.assertIn(f'id="{control}"', page, control)
        script = re.search(r"<script>(.*)</script>", page, re.S).group(1)
        referenced = set(re.findall(r"\$\('#([a-zA-Z0-9_-]+)'\)", script))
        missing = sorted(i for i in referenced if f'id="{i}"' not in page)
        self.assertEqual(missing, [], "script refers to elements the markup does not have")
