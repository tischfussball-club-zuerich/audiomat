"""Exclusive device use (another program opened the ALSA device directly)."""

import tempfile
import unittest
from pathlib import Path

from tfcz_audio.pw import Node, alsa_usage
from tfcz_audio.router import Router

from .test_api import ApiTestCase
from .test_identity import twin_backend


def fake_proc(tmp: Path, status: str, owner_comm: str = "obs", pid: int = 4242) -> Path:
    d = tmp / "asound" / "card2" / "pcm0c" / "sub0"
    d.mkdir(parents=True)
    (d / "status").write_text(status)
    (tmp / str(pid)).mkdir()
    (tmp / str(pid) / "comm").write_text(owner_comm + "\n")
    return tmp


def hdmi_node() -> Node:
    return Node(id=1, name="alsa_input.pci-0000_03_00.0.hws-1", media_class="Audio/Source",
                props={"alsa.card": "2", "alsa.device": "0", "alsa.subdevice": "0", "api.alsa.pcm.stream": "capture", "device.api": "alsa"})


class AlsaUsageTests(unittest.TestCase):
    def test_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_proc(Path(tmp), "closed\n")
            self.assertEqual(alsa_usage(hdmi_node(), str(root))["exclusive"], False)

    def test_open_by_pipewire_is_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_proc(Path(tmp), "state: RUNNING\nowner_pid   : 4242\ntrigger_time: 1.2\n", owner_comm="pipewire")
            u = alsa_usage(hdmi_node(), str(root))
            self.assertTrue(u["open"])
            self.assertEqual(u["owner"], "pipewire")
            self.assertFalse(u["exclusive"])

    def test_open_by_obs_is_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_proc(Path(tmp), "state: RUNNING\nowner_pid   : 4242\n", owner_comm="obs")
            u = alsa_usage(hdmi_node(), str(root))
            self.assertTrue(u["exclusive"])
            self.assertEqual(u["owner"], "obs")
            self.assertEqual(u["owner_pid"], 4242)

    def test_missing_proc_entry(self):
        self.assertFalse(alsa_usage(hdmi_node(), "/nonexistent")["exclusive"])

    def test_non_alsa_node(self):
        n = Node(id=2, name="bluez_input.x", media_class="Audio/Source", props={"device.api": "bluez5"})
        self.assertFalse(alsa_usage(n)["open"])


class TakenProblemTests(unittest.TestCase):
    def test_problem_and_status(self):
        from .test_identity import RouterIdentityTests
        cfg = RouterIdentityTests().config()
        backend = twin_backend()
        router = Router(cfg, backend, node_wait=0.1, sleep=lambda s: None)
        router.start()
        backend.graph().by_name("alsa_input.usb-Logitech-01.mono-fallback").props["tfcz.fake.owner"] = "obs"
        probs = {p["code"]: p for p in router.problems()}
        self.assertIn("device_taken", probs)
        self.assertTrue(probs["device_taken"]["title"].startswith("Anna Mikrofon ist von OBS übernommen"))
        self.assertIn("Audio Input Capture (PipeWire)", probs["device_taken"]["fix"])
        self.assertEqual(router.status()["devices"]["a_mic"]["taken_by"], "obs")
        self.assertFalse(router.status()["ok"])

    def test_node_error_state(self):
        from .test_identity import RouterIdentityTests
        cfg = RouterIdentityTests().config()
        backend = twin_backend()
        router = Router(cfg, backend, node_wait=0.1, sleep=lambda s: None)
        router.start()
        node = backend.graph().by_name("alsa_output.usb-Logitech-02.analog-stereo")
        node.state, node.error = "error", "Device or resource busy"
        probs = {p["code"]: p for p in router.problems()}
        self.assertIn("device_error", probs)
        self.assertIn("busy", probs["device_error"]["why"])


class DemoEndpointTests(ApiTestCase):
    def test_demo_take_and_release(self):
        code, body = self.call("POST", "/demo/take/hdmi", {"by": "obs"})
        self.assertEqual(code, 200)
        self.assertEqual(body["devices"]["hdmi"]["taken_by"], "obs")
        self.assertIn("device_taken", [p["code"] for p in body["problems"]])
        code, body = self.call("POST", "/demo/take/hdmi", {"release": True})
        self.assertIsNone(body["devices"]["hdmi"]["taken_by"])
        code, _ = self.call("POST", "/demo/take/nope")
        self.assertEqual(code, 404)
