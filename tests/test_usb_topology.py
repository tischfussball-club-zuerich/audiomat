"""Two USB audio devices behind one hub.

Measured on the studio machine and it cost weeks: both headsets are
full-speed devices, so behind one hub they share its transaction translator.
The game sound and the other person's voice came out with noise that grew
with the signal. Moving one headset to another controller removed it.

No software can fix that -- somebody has to move a plug -- so the tool has to
name it instead of leaving people to hunt.
"""

import unittest

from tfcz_audio.pw import FakeBackend, physical_devices, shares_usb_path, usb_topology
from tfcz_audio.router import Router

STUDIO_LEFT = "pci-0000:09:00.0-usb-0:1.2:1.0"   # on the VL805 hub
STUDIO_RIGHT = "pci-0000:0f:00.3-usb-0:2:1.0"    # on the motherboard
SAME_HUB = "pci-0000:09:00.0-usb-0:1.3:1.0"      # the arrangement that was noisy
SAME_CONTROLLER = "pci-0000:09:00.0-usb-0:4:1.0"


class TopologyTests(unittest.TestCase):
    def test_a_path_through_a_hub_is_recognised(self):
        parsed = usb_topology(STUDIO_LEFT)
        self.assertTrue(parsed["usb"])
        self.assertEqual(parsed["controller"], "pci-0000:09:00.0")
        self.assertEqual(parsed["ports"], ["1", "2"])
        self.assertTrue(parsed["through_hub"])
        self.assertEqual(parsed["hub"], "pci-0000:09:00.0-usb-0:1")

    def test_a_direct_port_has_no_hub(self):
        parsed = usb_topology(STUDIO_RIGHT)
        self.assertFalse(parsed["through_hub"])
        self.assertEqual(parsed["hub"], "")

    def test_anything_that_is_not_usb_is_left_alone(self):
        for path in ("", "pci-0000:03:00.0", "platform-something", "nonsense"):
            self.assertFalse(usb_topology(path)["usb"], path)

    def test_the_studios_working_layout_is_not_flagged(self):
        self.assertEqual(shares_usb_path(STUDIO_LEFT, STUDIO_RIGHT), "")

    def test_the_arrangement_that_was_noisy_is_flagged(self):
        self.assertEqual(shares_usb_path(STUDIO_LEFT, SAME_HUB), "hub")

    def test_the_same_controller_without_a_hub_is_told_apart(self):
        self.assertEqual(shares_usb_path(STUDIO_LEFT, SAME_CONTROLLER), "controller")

    def test_one_device_is_never_in_conflict_with_itself(self):
        self.assertEqual(shares_usb_path(STUDIO_LEFT, STUDIO_LEFT), "")


def router_with(paths):
    import tomllib

    from tfcz_audio.config import parse

    devices = "\n".join(
        f'headset_{name}_mic = {{ match = {{ "device.bus-path" = "{path}", kind = "input" }} }}'
        for name, path in paths.items())
    cfg = parse(tomllib.loads(f'[devices]\n{devices}\n[labels]\nheadset_a = "Links"\nheadset_b = "Rechts"\n'))
    backend = FakeBackend()
    for name, path in paths.items():
        backend.add_physical("MMX 150", "usb", f"alsa_input.{name}", f"alsa_output.{name}",
                             form_factor="headset", extra={"device.bus-path": path})
    router = Router(cfg, backend, node_wait=0.05, sleep=lambda s: None)
    router.start()
    return router


class FindingTests(unittest.TestCase):
    def test_two_headsets_on_one_hub_are_reported_with_both_names(self):
        router = router_with({"a": STUDIO_LEFT, "b": SAME_HUB})
        try:
            findings = router.usb_findings()
            self.assertEqual(len(findings), 1)
            self.assertIn("Links", findings[0]["title"])
            self.assertIn("Rechts", findings[0]["title"])
            self.assertEqual(findings[0]["level"], "warning")
            self.assertIn("Rauschen", findings[0]["effect"])
            self.assertIn("anderen", findings[0]["fix"])
        finally:
            router.stop()

    def test_the_working_layout_says_nothing(self):
        router = router_with({"a": STUDIO_LEFT, "b": STUDIO_RIGHT})
        try:
            self.assertEqual(router.usb_findings(), [])
        finally:
            router.stop()

    def test_the_same_controller_alone_is_not_worth_a_warning(self):
        """Only the shared hub was measured. Warning about anything less would
        make the check noise of its own."""
        router = router_with({"a": STUDIO_LEFT.replace("1.2", "3"), "b": SAME_CONTROLLER})
        try:
            self.assertEqual(router.usb_findings(), [])
        finally:
            router.stop()

    def test_it_shows_up_in_the_problem_list(self):
        router = router_with({"a": STUDIO_LEFT, "b": SAME_HUB})
        try:
            codes = [p["code"] for p in router.problems()]
            self.assertIn("usb_shared_hub", codes)
        finally:
            router.stop()

    def test_the_hardware_list_carries_the_hub_so_the_wizard_can_warn(self):
        backend = FakeBackend()
        backend.add_physical("MMX 150", "usb", "alsa_input.l", "alsa_output.l",
                             form_factor="headset", extra={"device.bus-path": STUDIO_LEFT})
        groups = {g["name"]: g for g in physical_devices(backend.graph())}
        self.assertEqual(next(iter(groups.values()))["hub"], "pci-0000:09:00.0-usb-0:1")
