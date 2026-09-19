import tomllib
import unittest

from tfcz_audio.config import OBS_MIC, ConfigError, parse

from .helpers import MINIMAL, example_config, minimal_config


class ConfigTests(unittest.TestCase):
    def test_example_config_parses(self):
        cfg = example_config()
        self.assertEqual(cfg.api.port, 8787)
        self.assertIn("a_to_b", cfg.routes)
        self.assertEqual(cfg.routes["a_to_obs"].sink, OBS_MIC)
        self.assertIn("hdmi_off", cfg.presets)
        self.assertTrue(cfg.presets["hdmi_off"]["hdmi_to_a"].mute)
        self.assertIsNone(cfg.presets["hdmi_off"]["hdmi_to_a"].volume)

    def test_aliases_resolve_to_node_names(self):
        cfg = minimal_config()
        self.assertEqual(cfg.routes["a_to_b"].source, "alsa_input.a")
        self.assertEqual(cfg.routes["a_to_b"].sink, "alsa_output.b")
        self.assertEqual(cfg.routes["a_to_b"].in_node, "tfcz.a_to_b.in")
        self.assertEqual(cfg.routes["a_to_b"].out_node, "tfcz.a_to_b.out")

    def test_raw_node_name_allowed(self):
        cfg = minimal_config('[routes.raw]\nfrom = "alsa_input.zzz"\nto = "alsa_output.a"\n')
        self.assertEqual(cfg.routes["raw"].source, "alsa_input.zzz")

    def test_default_volume_is_unity(self):
        self.assertEqual(minimal_config().routes["b_to_a"].volume, 1.0)

    def test_rejects_bad_volume(self):
        with self.assertRaises(ConfigError):
            minimal_config('[routes.x]\nfrom = "a_mic"\nto = "a_out"\nvolume = 3.0\n')

    def test_rejects_unknown_preset_route(self):
        with self.assertRaises(ConfigError):
            minimal_config("[presets.bad]\nnope = 0.5\n")

    def test_rejects_obs_mic_as_source(self):
        with self.assertRaises(ConfigError):
            minimal_config('[routes.x]\nfrom = "obs_mic"\nto = "a_out"\n')

    def test_rejects_bad_route_name(self):
        with self.assertRaises(ConfigError):
            minimal_config('[routes."Bad Name"]\nfrom = "a_mic"\nto = "a_out"\n')

    def test_routes_are_optional(self):
        cfg = parse(tomllib.loads('[devices]\na = "x"\n'))
        self.assertEqual(cfg.routes, {})
        self.assertEqual(cfg.devices["a"].node, "x")

    def test_state_file_can_be_disabled(self):
        cfg = parse(tomllib.loads("state_file = false\n" + MINIMAL))
        self.assertIsNone(cfg.state_file)
        cfg = parse(tomllib.loads(MINIMAL))
        self.assertIsNotNone(cfg.state_file)


class OwnNodesAreNotDevicesTests(unittest.TestCase):
    """A device pointing at one of our own channels gets routed like any other
    device, and the route check only looks at the alias. That is how a feedback
    loop gets built with nothing objecting."""

    def test_a_device_may_not_be_one_of_our_channels(self):
        from tfcz_audio.config import ConfigError, parse

        for node in ("tfcz.obsmic", "tfcz.obsmix", "tfcz.a_to_b.out", "tfcz.obsmic.a"):
            with self.subTest(node), self.assertRaises(ConfigError) as caught:
                parse({"devices": {"loop": node}})
            self.assertIn("channel of this router", str(caught.exception))

    def test_the_loop_that_used_to_get_through_is_refused(self):
        from tfcz_audio.config import ConfigError, parse

        with self.assertRaises(ConfigError):
            parse({"devices": {"loop": "tfcz.obsmic"},
                   "routes": {"r": {"from": "loop", "to": "obs_mic"}}})

    def test_real_devices_are_untouched(self):
        from tfcz_audio.config import parse

        cfg = parse({"devices": {"mic": "alsa_input.usb-something", "out": "alsa_output.usb-something"}})
        self.assertEqual(cfg.devices["mic"].node, "alsa_input.usb-something")

    def test_a_microphone_for_obs_cannot_be_a_device_alias(self):
        from tfcz_audio.config import ConfigError, parse

        with self.assertRaises(ConfigError) as caught:
            parse({"devices": {"obs_mic": "alsa_input.a"}})
        self.assertIn("reserved", str(caught.exception))
