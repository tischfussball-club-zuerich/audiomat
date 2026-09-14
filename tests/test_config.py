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

    def test_requires_routes(self):
        with self.assertRaises(ConfigError):
            parse(tomllib.loads('[devices]\na = "x"\n'))

    def test_state_file_can_be_disabled(self):
        cfg = parse(tomllib.loads("state_file = false\n" + MINIMAL))
        self.assertIsNone(cfg.state_file)
        cfg = parse(tomllib.loads(MINIMAL))
        self.assertIsNotNone(cfg.state_file)
