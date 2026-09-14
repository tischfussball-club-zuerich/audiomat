import tempfile
import tomllib
import unittest
from pathlib import Path

from tfcz_audio import edit
from tfcz_audio.config import ConfigError, dumps, load, parse, save, to_dict
from tfcz_audio.router import UnknownRoute

from .helpers import MINIMAL, example_config, fake_backend, minimal_config
from .test_router import make_router


class SerialisationTests(unittest.TestCase):
    def test_roundtrip_example(self):
        cfg = example_config()
        again = parse(tomllib.loads(dumps(to_dict(cfg))))
        self.assertEqual(to_dict(again), to_dict(cfg))
        self.assertEqual(again.routes["a_to_obs"].sink_ref, "obs_mic")
        self.assertEqual(again.routes["a_to_b"].source_ref, "headset_a_mic")

    def test_preset_shapes(self):
        cfg = minimal_config()
        d = to_dict(cfg)
        self.assertEqual(d["presets"]["quiet"], {"hdmi_to_a": 0.2})
        self.assertEqual(d["presets"]["hdmi_off"], {"hdmi_to_a": {"mute": True}})

    def test_state_file_disabled_serialised(self):
        cfg = parse(tomllib.loads("state_file = false\n" + MINIMAL))
        text = dumps(to_dict(cfg))
        self.assertIn("state_file = false", text.splitlines()[3])
        self.assertIsNone(parse(tomllib.loads(text)).state_file)

    def test_quoting(self):
        cfg = minimal_config('[routes.q]\nfrom = "a_mic"\nto = "a_out"\ndescription = "say \\"hi\\" \\\\ ok"\n')
        again = parse(tomllib.loads(dumps(to_dict(cfg))))
        self.assertEqual(again.routes["q"].description, 'say "hi" \\ ok')

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "config.toml"
            cfg = minimal_config()
            save(cfg, path)
            loaded = load(path)
            self.assertEqual(to_dict(loaded), to_dict(cfg))
            self.assertEqual(loaded.path, path)


class ReloadTests(unittest.TestCase):
    def test_reload_restarts_only_changed_routes(self):
        router, backend = make_router()
        router.start()
        router.set_route("hdmi_to_a", volume=0.4)
        before = dict(router.procs)
        new = minimal_config()
        new.devices["b_out"] = "alsa_output.b2"
        new.routes["a_to_b"].sink = "alsa_output.b2"
        backend.add_device("alsa_output.b2", "Audio/Sink")
        router.reload(new)
        self.assertIsNot(router.procs["a_to_b"], before["a_to_b"], "changed route restarted")
        self.assertIs(router.procs["hdmi_to_a"], before["hdmi_to_a"], "unchanged route kept")
        self.assertIs(router.procs["__virtual__"], before["__virtual__"])
        self.assertEqual(router.desired["hdmi_to_a"].volume, 0.4, "runtime volume kept")
        self.assertTrue(before["a_to_b"].terminated)
        self.assertEqual(backend.graph().by_name("tfcz.a_to_b.out").volume, 1.0)

    def test_reload_adds_and_removes_routes(self):
        router, backend = make_router()
        router.start()
        new = minimal_config('[routes.extra]\nfrom = "hdmi"\nto = "b_out"\nvolume = 0.5\n')
        del new.routes["b_to_a"]
        router.reload(new)
        self.assertNotIn("b_to_a", router.procs)
        self.assertIsNone(backend.graph().by_name("tfcz.b_to_a.out"))
        self.assertIn("extra", router.procs)
        self.assertEqual(backend.graph().by_name("tfcz.extra.out").volume, 0.5)
        self.assertNotIn("b_to_a", router.status()["routes"])

    def test_reload_adopts_changed_config_default(self):
        router, _ = make_router()
        router.start()
        router.set_route("hdmi_to_a", volume=0.4)
        new = minimal_config()
        new.routes["hdmi_to_a"].volume = 0.9
        router.reload(new)
        self.assertEqual(router.desired["hdmi_to_a"].volume, 0.9)


class EditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"
        cfg = minimal_config()
        save(cfg, self.path)
        self.cfg = load(self.path)
        self.backend = fake_backend()
        self.router, _ = make_router(cfg=self.cfg, backend=self.backend)
        self.router.start()

    def tearDown(self):
        self.tmp.cleanup()

    def reloaded(self):
        return load(self.path)

    def test_set_devices_writes_file_and_reloads(self):
        self.backend.add_device("alsa_input.hdmi2", "Audio/Source")
        devices = dict(self.cfg.devices, hdmi="alsa_input.hdmi2")
        status = edit.set_devices(self.router, devices)
        self.assertEqual(self.reloaded().devices["hdmi"], "alsa_input.hdmi2")
        self.assertEqual(self.router.cfg.routes["hdmi_to_a"].source, "alsa_input.hdmi2")
        self.assertEqual(status["routes"]["hdmi_to_a"]["from"], "alsa_input.hdmi2")

    def test_set_devices_refuses_removing_used_alias(self):
        devices = dict(self.cfg.devices)
        del devices["hdmi"]
        with self.assertRaises(ConfigError):
            edit.set_devices(self.router, devices)
        self.assertIn("hdmi", self.reloaded().devices)

    def test_set_devices_can_add_unused_alias_and_remove_it(self):
        edit.set_devices(self.router, dict(self.cfg.devices, spare="alsa_output.x"))
        self.assertIn("spare", self.reloaded().devices)
        edit.set_devices(self.router, dict(self.cfg.devices))
        self.assertNotIn("spare", self.reloaded().devices)

    def test_upsert_and_delete_route(self):
        edit.upsert_route(self.router, "hdmi_to_b", {"from": "hdmi", "to": "b_out", "volume": 0.7, "description": "x"})
        cfg = self.reloaded()
        self.assertEqual(cfg.routes["hdmi_to_b"].volume, 0.7)
        self.assertEqual(cfg.routes["hdmi_to_b"].sink_ref, "b_out")
        self.assertIn("hdmi_to_b", self.router.procs)
        # editing without volume keeps the old one
        edit.upsert_route(self.router, "hdmi_to_b", {"from": "hdmi", "to": "a_out"})
        self.assertEqual(self.reloaded().routes["hdmi_to_b"].volume, 0.7)
        # delete also drops it from presets
        edit.upsert_preset(self.router, "p", {"hdmi_to_b": 0.1, "hdmi_to_a": 0.1})
        edit.delete_route(self.router, "hdmi_to_b")
        cfg = self.reloaded()
        self.assertNotIn("hdmi_to_b", cfg.routes)
        self.assertEqual(set(cfg.presets["p"]), {"hdmi_to_a"})
        with self.assertRaises(UnknownRoute):
            edit.delete_route(self.router, "hdmi_to_b")

    def test_upsert_route_validation(self):
        with self.assertRaises(ConfigError):
            edit.upsert_route(self.router, "Bad Name", {"from": "hdmi", "to": "a_out"})
        with self.assertRaises(ConfigError):
            edit.upsert_route(self.router, "x", {"from": "hdmi"})
        with self.assertRaises(ConfigError):
            edit.upsert_route(self.router, "x", {"from": "hdmi", "to": "a_out", "volume": 9})
        self.assertNotIn("x", self.reloaded().routes)

    def test_presets_edit(self):
        edit.upsert_preset(self.router, "night", {"hdmi_to_a": {"mute": True}})
        self.assertTrue(self.reloaded().presets["night"]["hdmi_to_a"].mute)
        self.router.apply_preset("night")
        edit.delete_preset(self.router, "night")
        self.assertNotIn("night", self.reloaded().presets)
        with self.assertRaises(ConfigError):
            edit.upsert_preset(self.router, "bad", {"nope": 0.5})

    def test_save_current_as_defaults(self):
        self.router.set_route("hdmi_to_a", volume=0.12, mute=True)
        self.router.set_route("a_to_b", volume=0.5)
        edit.save_current_as_defaults(self.router)
        cfg = self.reloaded()
        self.assertEqual(cfg.routes["hdmi_to_a"].volume, 0.12)
        self.assertTrue(cfg.routes["hdmi_to_a"].mute)
        self.assertEqual(cfg.routes["a_to_b"].volume, 0.5)
        self.assertFalse(cfg.routes["a_to_b"].mute)

    def test_public_config_hides_token(self):
        self.router.cfg.api.token = "secret"
        data = edit.public_config(self.router.cfg)
        self.assertNotIn("token", data["api"])
        self.assertTrue(data["api"]["token_set"])
        self.assertEqual(data["path"], str(self.path))
