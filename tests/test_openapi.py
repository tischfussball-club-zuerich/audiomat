import json
import unittest
import urllib.request

from tfcz_audio import openapi

from .test_api import ApiTestCase


class SpecTests(unittest.TestCase):
    def test_the_document_is_valid_enough_to_load(self):
        doc = openapi.document("http://127.0.0.1:8787")
        self.assertEqual(doc["openapi"], "3.0.3")
        self.assertEqual(doc["servers"][0]["url"], "http://127.0.0.1:8787")
        self.assertTrue(doc["paths"])
        json.dumps(doc)  # must survive the trip through the wire

    def test_every_operation_carries_a_summary_and_a_known_tag(self):
        doc = openapi.document()
        known = {t["name"] for t in doc["tags"]}
        for path, ops in doc["paths"].items():
            for method, op in ops.items():
                self.assertTrue(op.get("summary"), f"{method} {path}")
                self.assertIn(op["tags"][0], known, f"{method} {path}")

    def test_the_headline_endpoints_are_all_described(self):
        paths = openapi.document()["paths"]
        for path in ("/status", "/routes/{route}/volume/{value}", "/presets/{preset}",
                     "/reset", "/versions", "/config", "/diagnostics"):
            self.assertIn(path, paths)


class ServedSpecTests(ApiTestCase):
    def test_the_spec_and_the_docs_page_need_no_token(self):
        """Documentation behind a token is no documentation. Neither holds
        anything the page does not show anyway."""
        with urllib.request.urlopen(self.base + "/openapi.json", timeout=5) as resp:
            doc = json.loads(resp.read().decode())
        self.assertEqual(doc["info"]["title"], "TFCZ Audio")
        self.assertEqual(doc["servers"][0]["url"], self.base)
        with urllib.request.urlopen(self.base + "/api-docs", timeout=5) as resp:
            page = resp.read().decode()
        self.assertIn("swagger", page)
        self.assertIn("openapi.json", page)

    def test_a_forged_host_header_cannot_end_up_in_the_document(self):
        req = urllib.request.Request(self.base + "/openapi.json")
        req.add_header("Host", 'evil.example"/><script>')
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                doc = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:  # a rejected host is fine too
            self.assertIn(exc.code, (400, 403))
            return
        self.assertNotIn("script", doc["servers"][0]["url"])

    def test_every_documented_get_without_parameters_really_exists(self):
        """The spec is written by hand, so it has to be checked against the
        server or it drifts."""
        for path, ops in openapi.document()["paths"].items():
            if "get" not in ops or "{" in path:
                continue
            status, _ = self.call("GET", path)
            self.assertNotEqual(status, 404, f"documented but missing: GET {path}")

    def test_no_endpoint_of_the_server_is_missing_from_the_spec(self):
        """Every literal path the handler matches has to be documented."""
        import re
        from pathlib import Path as P

        source = P("tfcz_audio/api.py").read_text()
        literal = set()
        for match in re.finditer(r'seg == \[([^\]]*)\]', source):
            parts = [p.strip().strip('"') for p in match.group(1).split(",") if p.strip()]
            if parts:
                literal.add("/" + "/".join(parts))
        for match in re.finditer(r'seg\[1:\] == \[([^\]]*)\]', source):
            parts = [p.strip().strip('"') for p in match.group(1).split(",") if p.strip()]
            if parts:
                literal.add("/config/" + "/".join(parts))
        documented = set(openapi.document()["paths"])
        undocumented = sorted(p for p in literal if p not in documented)
        self.assertEqual(undocumented, [], f"handled but not in the spec: {undocumented}")
