"""Device identity: identical headsets, serials, USB ports, labels, setup."""

import tempfile
import tomllib
import unittest
from pathlib import Path

from tfcz_audio import edit
from tfcz_audio.config import ConfigError, DeviceSpec, human, load, parse, save
from tfcz_audio.pw import FakeBackend, identity_for, physical_devices, port_label, resolve_match
from tfcz_audio.router import Router, resolve_devices

from .helpers import MINIMAL


def twin_backend():
    """Two identical Logitech headsets (no serial) in ports 1 and 2, one Jabra with a serial, one HDMI card."""
    b = FakeBackend()
    for i in (1, 2):
        b.add_physical(
            "Logitech USB Headset", "usb", f"alsa_input.usb-Logitech-0{i}.mono-fallback", f"alsa_output.usb-Logitech-0{i}.analog-stereo",
            form_factor="headset",
            extra={"device.serial": "Logitech_Logitech_USB_Headset", "device.bus-path": f"pci-0000:00:14.0-usb-0:{i}:1.0",
                   "device.vendor.id": "046d", "device.product.id": "0a44"},
        )
    b.add_physical(
        "Jabra Speak 510", "usb", "alsa_input.usb-Jabra_A1B2-00.mono-fallback", "alsa_output.usb-Jabra_A1B2-00.analog-stereo",
        form_factor="headset",
        extra={"device.serial": "Jabra_Speak_510_A1B2", "device.bus-path": "pci-0000:00:14.0-usb-0:4:1.0",
               "device.vendor.id": "0b0e", "device.product.id": "0412"},
    )
    b.add_physical("HWS", "pci", "alsa_input.pci-0000_03_00.0.hws-1", None, extra={"alsa.card_name": "HWS", "api.alsa.card": "1"})
    b.add_physical("HDMI Monitor", "pci", None, "alsa_output.pci-0000_01_00.1.hdmi-stereo")
    return b


class IdentityTests(unittest.TestCase):
    def test_port_label(self):
        self.assertEqual(port_label("pci-0000:00:14.0-usb-0:3:1.0"), "USB port 3")
        self.assertEqual(port_label("pci-0000:00:14.0-usb-0:3.2:1.0"), "USB port 3.2")
        self.assertEqual(port_label(""), "")

    def test_identical_headsets_are_port_bound(self):
        g = twin_backend().graph()
        ident = identity_for(g.by_name("alsa_input.usb-Logitech-01.mono-fallback"), g)
        self.assertEqual(ident["strategy"], "port")
        self.assertEqual(ident["match"], {"device.bus-path": "pci-0000:00:14.0-usb-0:1:1.0", "kind": "input"})
        self.assertIn("USB port 1", ident["text"])
        self.assertIn("identical", ident["text"])

    def test_unique_serial_is_port_independent(self):
        g = twin_backend().graph()
        ident = identity_for(g.by_name("alsa_output.usb-Jabra_A1B2-00.analog-stereo"), g)
        self.assertEqual(ident["strategy"], "serial")
        self.assertEqual(ident["match"], {"device.serial": "Jabra_Speak_510_A1B2", "kind": "output"})
        self.assertIn("Any USB port", ident["text"])

    def test_pci_hardware_uses_name(self):
        g = twin_backend().graph()
        self.assertEqual(identity_for(g.by_name("alsa_input.pci-0000_03_00.0.hws-1"), g)["strategy"], "name")

    def test_resolve_match(self):
        g = twin_backend().graph()
        nodes = resolve_match({"device.bus-path": "pci-0000:00:14.0-usb-0:2:1.0", "kind": "output"}, g)
        self.assertEqual([n.name for n in nodes], ["alsa_output.usb-Logitech-02.analog-stereo"])
        # ambiguous: serial shared by both twins
        nodes = resolve_match({"device.serial": "Logitech_Logitech_USB_Headset", "kind": "input"}, g)
        self.assertEqual(len(nodes), 2)

    def test_physical_grouping_and_friendly_names(self):
        g = twin_backend().graph()
        groups = physical_devices(g)
        headsets = [x for x in groups if x["headset"]]
        self.assertEqual(len(headsets), 3)
        jabra = next(x for x in headsets if "Jabra" in x["name"])
        self.assertEqual(jabra["inputs"][0]["friendly"], "Jabra Speak 510 · microphone")
        self.assertEqual(jabra["outputs"][0]["friendly"], "Jabra Speak 510 · headphones")
        self.assertEqual(jabra["identity"]["strategy"], "serial")
        hdmi = next(x for x in groups if x["hdmi_capture"])
        self.assertTrue(hdmi["inputs"][0]["friendly"].startswith("HDMI capture input 1"))
        monitor = next(x for x in groups if "Monitor" in x["name"])
        self.assertTrue(monitor["outputs"][0]["speakers"])


class ConfigIdentityTests(unittest.TestCase):
    def test_match_device_roundtrip(self):
        text = MINIMAL + '\n[devices.extra]\nmatch = { "device.serial" = "X1", kind = "input" }\n[labels]\nheadset_a = "Anna"\n'
        cfg = parse(tomllib.loads(text))
        self.assertEqual(cfg.devices["extra"].match, {"device.serial": "X1", "kind": "input"})
        self.assertFalse(cfg.devices["extra"].is_static)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            save(cfg, path)
            again = load(path)
        self.assertEqual(again.devices["extra"].match, cfg.devices["extra"].match)
        self.assertEqual(again.labels, {"headset_a": "Anna"})

    def test_bad_match(self):
        with self.assertRaises(ConfigError):
            parse(tomllib.loads(MINIMAL + '\n[devices.x]\nmatch = { kind = "sideways" }\n'))
        data = tomllib.loads(MINIMAL)
        data["devices"]["y"] = 5
        with self.assertRaises(ConfigError):
            parse(data)

    def test_human_names(self):
        cfg = parse(tomllib.loads(MINIMAL + '\n[labels]\nheadset_a = "Anna"\nhdmi = "PlayStation"\n'))
        self.assertEqual(human(cfg, "headset_a_mic"), "Anna microphone")
        self.assertEqual(human(cfg, "headset_a_out"), "Anna headphones")
        self.assertEqual(human(cfg, "hdmi"), "PlayStation")
        self.assertEqual(human(cfg, "headset_b_out"), "Headset B headphones")
        self.assertEqual(human(cfg, "obs_mic"), "OBS stream")
        self.assertEqual(human(None, "hdmi_1"), "HDMI 1")


class RouterIdentityTests(unittest.TestCase):
    def config(self):
        text = """
[devices.a_mic]
match = { "device.bus-path" = "pci-0000:00:14.0-usb-0:1:1.0", kind = "input" }
[devices.a_out]
match = { "device.bus-path" = "pci-0000:00:14.0-usb-0:1:1.0", kind = "output" }
[devices.b_mic]
match = { "device.bus-path" = "pci-0000:00:14.0-usb-0:2:1.0", kind = "input" }
[devices.b_out]
match = { "device.bus-path" = "pci-0000:00:14.0-usb-0:2:1.0", kind = "output" }
[devices.j_out]
match = { "device.serial" = "Jabra_Speak_510_A1B2", kind = "output" }
[labels]
a = "Anna"
b = "Ben"
[routes.a_to_b]
from = "a_mic"
to = "b_out"
[routes.b_to_a]
from = "b_mic"
to = "a_out"
[routes.a_to_j]
from = "a_mic"
to = "j_out"
[routes.a_to_obs]
from = "a_mic"
to = "obs_mic"
"""
        cfg = parse(tomllib.loads(text))
        cfg.state_file = None
        return cfg

    def make(self, backend=None):
        backend = backend or twin_backend()
        router = Router(self.config(), backend, node_wait=0.1, sleep=lambda s: None)
        router.start()
        return router, backend

    def test_resolution_by_port_and_serial(self):
        router, _ = self.make()
        self.assertEqual(router.resolved["a_mic"].node, "alsa_input.usb-Logitech-01.mono-fallback")
        self.assertEqual(router.resolved["b_out"].node, "alsa_output.usb-Logitech-02.analog-stereo")
        self.assertEqual(router.resolved["j_out"].node, "alsa_output.usb-Jabra_A1B2-00.analog-stereo")
        st = router.status()
        self.assertTrue(st["routes"]["a_to_b"]["connected"])
        self.assertEqual(st["devices"]["a_mic"]["identity"]["strategy"], "port")
        self.assertEqual(st["devices"]["j_out"]["identity"]["strategy"], "serial")
        self.assertEqual(st["routes"]["a_to_b"]["label"], "Anna microphone → Ben headphones")

    def test_unplug_and_replug_in_same_port(self):
        router, backend = self.make()
        proc_before = router.procs["b_to_a"]
        backend.remove_node("alsa_input.usb-Logitech-02.mono-fallback")
        router.reconcile()
        st = router.route_status("b_to_a")
        self.assertFalse(st["source_present"])
        codes = [p["code"] for p in router.problems()]
        self.assertIn("device_missing", codes)
        # loopback restarted pointing at a placeholder, waiting
        self.assertIsNot(router.procs["b_to_a"], proc_before)
        # same headset comes back in the same port under a new node id/name
        backend.add_physical(
            "Logitech USB Headset", "usb", "alsa_input.usb-Logitech-02b.mono-fallback", None, form_factor="headset",
            extra={"device.serial": "Logitech_Logitech_USB_Headset", "device.bus-path": "pci-0000:00:14.0-usb-0:2:1.0",
                   "device.vendor.id": "046d", "device.product.id": "0a44"},
        )
        router.reconcile()
        self.assertEqual(router.resolved["b_mic"].node, "alsa_input.usb-Logitech-02b.mono-fallback")
        self.assertTrue(router.route_status("b_to_a")["connected"])

    def test_moved_to_other_port_is_explained(self):
        router, backend = self.make()
        backend.remove_node("alsa_input.usb-Logitech-02.mono-fallback")
        backend.remove_node("alsa_output.usb-Logitech-02.analog-stereo")
        backend.add_physical(
            "Logitech USB Headset", "usb", "alsa_input.usb-Logitech-03.mono-fallback", "alsa_output.usb-Logitech-03.analog-stereo",
            form_factor="headset",
            extra={"device.serial": "Logitech_Logitech_USB_Headset", "device.bus-path": "pci-0000:00:14.0-usb-0:7:1.0",
                   "device.vendor.id": "046d", "device.product.id": "0a44"},
        )
        router.reconcile()
        missing = [p for p in router.problems() if p["code"] == "device_missing"]
        self.assertTrue(missing)
        self.assertIn("USB port 7", missing[0]["why"])
        self.assertIn("USB port 2", missing[0]["fix"])

    def test_serial_device_moves_ports_freely(self):
        router, backend = self.make()
        backend.remove_node("alsa_output.usb-Jabra_A1B2-00.analog-stereo")
        backend.remove_node("alsa_input.usb-Jabra_A1B2-00.mono-fallback")
        router.reconcile()
        self.assertFalse(router.route_status("a_to_j")["sink_present"])
        backend.add_physical(
            "Jabra Speak 510", "usb", None, "alsa_output.usb-Jabra_A1B2-00.analog-stereo.9", form_factor="headset",
            extra={"device.serial": "Jabra_Speak_510_A1B2", "device.bus-path": "pci-0000:00:14.0-usb-0:9:1.0",
                   "device.vendor.id": "0b0e", "device.product.id": "0412"},
        )
        router.reconcile()
        self.assertTrue(router.route_status("a_to_j")["sink_present"])
        self.assertTrue(router.route_status("a_to_j")["connected"])

    def test_problem_messages_use_labels_and_fixes(self):
        router, backend = self.make()
        node = backend.graph().by_name("alsa_input.usb-Logitech-01.mono-fallback")
        node.mute = True
        probs = {p["code"]: p for p in router.problems()}
        self.assertIn("device_muted", probs)
        self.assertTrue(probs["device_muted"]["title"].startswith("Anna microphone"))
        self.assertTrue(probs["device_muted"]["fixable"])
        router.fix_device("a_mic")
        self.assertFalse(backend.graph().by_name("alsa_input.usb-Logitech-01.mono-fallback").mute)
        self.assertIn("port_bound", {p["code"] for p in router.problems()})

    def test_feedback_warning_for_speakers(self):
        cfg = self.config()
        cfg.devices["tv"] = DeviceSpec("tv", node="alsa_output.pci-0000_01_00.1.hdmi-stereo")
        text = "[routes.a_to_tv]\nfrom = 'a_mic'\nto = 'tv'\n"
        from tfcz_audio.config import RouteConfig
        cfg.routes["a_to_tv"] = RouteConfig(name="a_to_tv", source="", sink="alsa_output.pci-0000_01_00.1.hdmi-stereo", source_ref="a_mic", sink_ref="tv")
        router = Router(cfg, twin_backend(), node_wait=0.1, sleep=lambda s: None)
        router.start()
        codes = {p["code"] for p in router.problems()}
        self.assertIn("feedback_risk", codes)


class SetupTests(unittest.TestCase):
    def test_setup_builds_full_config_with_identity_and_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            cfg = parse(tomllib.loads(MINIMAL))
            cfg.state_file = None
            save(cfg, path)
            cfg = load(path)
            cfg.state_file = None
            backend = twin_backend()
            router = Router(cfg, backend, node_wait=0.1, sleep=lambda s: None)
            router.start()
            status = edit.setup(router, {
                "headset_a": {"mic": "alsa_input.usb-Logitech-01.mono-fallback", "out": "alsa_output.usb-Logitech-01.analog-stereo", "label": "Anna"},
                "headset_b": {"mic": "alsa_input.usb-Jabra_A1B2-00.mono-fallback", "out": "alsa_output.usb-Jabra_A1B2-00.analog-stereo", "label": "Ben"},
                "game": "alsa_input.pci-0000_03_00.0.hws-1",
                "game_label": "PlayStation",
            })
            saved = load(path)
            self.assertEqual(saved.labels, {"headset_a": "Anna", "headset_b": "Ben", "game_sound": "PlayStation"})
            self.assertEqual(saved.devices["headset_a_mic"].match["device.bus-path"], "pci-0000:00:14.0-usb-0:1:1.0")
            self.assertEqual(saved.devices["headset_b_out"].match["device.serial"], "Jabra_Speak_510_A1B2")
            self.assertTrue(saved.devices["game_sound"].is_static)
            self.assertEqual(set(saved.routes), {"a_to_b", "b_to_a", "a_to_obs", "b_to_obs", "game_to_a", "game_to_b"})
            self.assertIn("game_quiet", saved.presets)
            self.assertTrue(all(r["connected"] for r in status["routes"].values()), status["routes"])
            self.assertEqual(status["routes"]["a_to_b"]["label"], "Anna microphone → Ben headphones")
            self.assertEqual(status["devices"]["headset_a_mic"]["identity"]["strategy"], "port")
            self.assertEqual(status["devices"]["headset_b_mic"]["identity"]["strategy"], "serial")

    def test_setup_validation(self):
        cfg = parse(tomllib.loads(MINIMAL))
        cfg.state_file = None
        router = Router(cfg, twin_backend(), node_wait=0.1, sleep=lambda s: None)
        router.start()
        same = {"mic": "alsa_input.usb-Logitech-01.mono-fallback", "out": "alsa_output.usb-Logitech-01.analog-stereo"}
        with self.assertRaises(ConfigError):
            edit.setup(router, {"headset_a": same, "headset_b": same})
        with self.assertRaises(ConfigError):
            edit.setup(router, {"headset_a": same})
        with self.assertRaises(ConfigError):
            edit.setup(router, {"headset_a": dict(same, label="X"), "headset_b": {"mic": "alsa_input.usb-Logitech-02.mono-fallback", "out": "alsa_output.usb-Logitech-02.analog-stereo", "label": "x"}})
