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
