import tempfile
import tomllib
import unittest
from pathlib import Path

from tfcz_audio import edit
from tfcz_audio.config import ConfigError, DeviceSpec, dumps, load, parse, save, to_dict
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
        new.devices["b_out"] = DeviceSpec("b_out", node="alsa_output.b2")
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

    def device_values(self, **override):
        values = {a: s.to_value() for a, s in self.router.cfg.devices.items()}
        values.update(override)
        return values

    def test_set_devices_writes_file_and_reloads(self):
        self.backend.add_device("alsa_input.hdmi2", "Audio/Source")
        status = edit.set_devices(self.router, self.device_values(hdmi="alsa_input.hdmi2"))
        self.assertEqual(self.reloaded().devices["hdmi"].node, "alsa_input.hdmi2")
        self.assertEqual(self.router.resolved["hdmi"].node, "alsa_input.hdmi2")
        self.assertEqual(status["routes"]["hdmi_to_a"]["from_node"], "alsa_input.hdmi2")

    def test_set_devices_refuses_removing_used_alias(self):
        devices = self.device_values()
        del devices["hdmi"]
        with self.assertRaises(ConfigError):
            edit.set_devices(self.router, devices)
        self.assertIn("hdmi", self.reloaded().devices)

    def test_set_devices_can_add_unused_alias_and_remove_it(self):
        original = self.device_values()
        edit.set_devices(self.router, self.device_values(spare="alsa_output.x"))
        self.assertIn("spare", self.reloaded().devices)
        edit.set_devices(self.router, original)
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


class ConcurrentEditTests(unittest.TestCase):
    """Two edits at the same time: the page can save names while an automation
    renames a route. Both have to survive."""

    def test_no_edit_is_lost_when_several_arrive_at_once(self):
        import tempfile
        import threading
        from pathlib import Path

        from tfcz_audio import edit
        from tfcz_audio.config import save
        from tfcz_audio.router import Router

        from .helpers import fake_backend, minimal_config

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            cfg = minimal_config()
            cfg.path = path
            save(cfg, path)
            router = Router(cfg, fake_backend(), node_wait=0.05, sleep=lambda s: None)
            router.start()
            try:
                errors = []

                def add(index):
                    try:
                        edit.upsert_route(router, f"r{index}", {"from": "a_mic", "to": "b_out", "volume": 0.5})
                    except Exception as exc:  # noqa: BLE001
                        errors.append(exc)

                threads = [threading.Thread(target=add, args=(i,)) for i in range(8)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                self.assertEqual(errors, [])
                for i in range(8):
                    self.assertIn(f"r{i}", router.cfg.routes, "an edit was overwritten by a parallel one")
            finally:
                router.stop()


class GermanErrorTests(unittest.TestCase):
    """The page is German and its users are not technical. An error that
    reaches them as a toast has to be in the same language as the button that
    produced it."""

    ENGLISH_GIVEAWAYS = (" must ", " is not ", "pick ", " needs ", "Give ", " cannot ")

    def test_what_the_wizard_can_trigger_speaks_german(self):
        import tempfile
        from pathlib import Path as P

        from tfcz_audio import edit
        from tfcz_audio.config import ConfigError, save
        from tfcz_audio.router import Router, RouterError

        from .helpers import fake_backend, minimal_config

        bad_bodies = [
            {},
            {"headset_a": {"mic": "alsa_input.a"}, "headset_b": {"mic": "alsa_input.b", "out": "alsa_output.b"}},
            {"headset_a": {"mic": "alsa_input.a", "out": "alsa_output.a"},
             "headset_b": {"mic": "alsa_input.a", "out": "alsa_output.a"}},
            {"headset_a": {"mic": "alsa_input.a", "out": "alsa_output.a", "label": "Hans"},
             "headset_b": {"mic": "alsa_input.b", "out": "alsa_output.b", "label": "hans"}},
            {"headset_a": {"mic": "alsa_input.a", "out": "alsa_output.a"},
             "headset_b": {"mic": "alsa_input.b", "out": "alsa_output.b"}, "game": "alsa_input.a"},
            {"headset_a": {"mic": "alsa_input.a", "out": "alsa_output.a"},
             "headset_b": {"mic": "alsa_input.b", "out": "alsa_output.b"}, "game_volume": "laut"},
            {"headset_a": {"mic": "alsa_input.a", "out": "alsa_output.a"},
             "headset_b": {"mic": "alsa_input.b", "out": "alsa_output.b"}, "game": "gibtsnicht"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = P(tmp) / "config.toml"
            cfg = minimal_config()
            cfg.path = path
            save(cfg, path)
            router = Router(cfg, fake_backend(), node_wait=0.02, sleep=lambda s: None)
            router.start()
            try:
                for body in bad_bodies:
                    with self.subTest(str(body)[:60]):
                        with self.assertRaises((ConfigError, RouterError)) as caught:
                            edit.setup(router, body)
                        message = str(caught.exception)
                        for giveaway in self.ENGLISH_GIVEAWAYS:
                            self.assertNotIn(giveaway, f" {message} ", message)
            finally:
                router.stop()

    def test_route_and_volume_errors_speak_german_too(self):
        from tfcz_audio import edit
        from tfcz_audio.config import ConfigError
        from tfcz_audio.router import Router, RouterError

        from .helpers import fake_backend, minimal_config

        router = Router(minimal_config(), fake_backend(), node_wait=0.02, sleep=lambda s: None)
        router.start()
        try:
            with self.assertRaises(ConfigError) as caught:
                edit.upsert_route(router, "x", {"from": "a_mic"})
            self.assertIn("«von» und «zu»", str(caught.exception))
            with self.assertRaises(RouterError) as caught:
                router.set_route("a_to_b", volume=99)
            self.assertIn("Lautstärke", str(caught.exception))
            with self.assertRaises(RouterError) as caught:
                router.fix_device("a_mic_missing" if "a_mic_missing" in router.cfg.devices else "nope")
        except Exception:
            raise
        finally:
            router.stop()
