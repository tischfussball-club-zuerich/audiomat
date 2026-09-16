import json
import tempfile
import unittest
from pathlib import Path

from tfcz_audio.config import OBS_MIC
from tfcz_audio.pw import Graph
from tfcz_audio.router import VIRTUAL, Router, RouterError, UnknownPreset, UnknownRoute, resolve_devices, route_spec, virtual_spec

from .helpers import fake_backend, minimal_config


def make_router(cfg=None, backend=None):
    cfg = cfg or minimal_config()
    backend = backend or fake_backend()
    router = Router(cfg, backend, node_wait=0.1, sleep=lambda s: None)
    return router, backend


class SpecTests(unittest.TestCase):
    def test_route_spec_targets(self):
        cfg = minimal_config()
        spec = route_spec(cfg, cfg.routes["a_to_b"], resolve_devices(cfg, Graph()))
        self.assertEqual(spec.capture_props["target.object"], "alsa_input.a")
        self.assertEqual(spec.playback_props["target.object"], "alsa_output.b")
        self.assertTrue(spec.capture_props["node.dont-fallback"])
        self.assertNotIn("node.latency", spec.capture_props, "auto: the system decides the buffer size")
        self.assertNotIn("node.latency", spec.playback_props)

    def test_obs_route_targets_mix_bus(self):
        cfg = minimal_config()
        spec = route_spec(cfg, cfg.routes["a_to_obs"], resolve_devices(cfg, Graph()))
        self.assertEqual(spec.playback_props["target.object"], cfg.virtual.obs_mix_name)

    def test_virtual_spec(self):
        cfg = minimal_config()
        spec = virtual_spec(cfg)
        self.assertEqual(spec.capture_props["media.class"], "Audio/Sink")
        self.assertEqual(spec.playback_props["media.class"], "Audio/Source/Virtual")
        self.assertEqual(spec.playback_node, "tfcz.obsmic")

    def test_capture_sink_flag(self):
        cfg = minimal_config('[routes.mon]\nfrom = "a_out"\nto = "b_out"\ncapture_sink = true\n')
        spec = route_spec(cfg, cfg.routes["mon"], resolve_devices(cfg, Graph()))
        self.assertTrue(spec.capture_props["stream.capture.sink"])


class RouterTests(unittest.TestCase):
    def test_start_spawns_everything_and_applies_volumes(self):
        router, backend = make_router()
        router.start()
        spawned = [c[1] for c in backend.calls if c[0] == "spawn"]
        self.assertEqual(spawned[0], "tfcz.virtual")
        self.assertEqual(set(spawned[1:]), {"tfcz.a_to_b", "tfcz.b_to_a", "tfcz.hdmi_to_a", "tfcz.a_to_obs"})
        g = backend.graph()
        self.assertEqual(g.by_name("tfcz.hdmi_to_a.out").volume, 0.6)
        self.assertEqual(g.by_name("tfcz.a_to_b.out").volume, 1.0)
        status = router.status()
        self.assertTrue(status["virtual_mic"]["running"])
        self.assertTrue(status["virtual_mic"]["present"])
        self.assertTrue(status["routes"]["a_to_b"]["connected"])
        self.assertTrue(all(d["present"] for d in status["devices"].values()))

    def test_apply_is_idempotent(self):
        router, backend = make_router()
        router.start()
        n = len(backend.calls)
        router.reconcile()
        router.reconcile()
        self.assertEqual(len(backend.calls), n)

    def test_set_route_volume_and_mute(self):
        router, backend = make_router()
        router.start()
        st = router.set_route("hdmi_to_a", volume=0.3)
        self.assertEqual(st["volume"], 0.3)
        self.assertEqual(st["actual"]["volume"], 0.3)
        self.assertAlmostEqual(st["volume_db"], -31.37, places=1)
        st = router.set_route("hdmi_to_a", mute=True)
        self.assertTrue(st["mute"])
        self.assertTrue(backend.graph().by_name("tfcz.hdmi_to_a.out").mute)
        st = router.toggle_mute("hdmi_to_a")
        self.assertFalse(st["mute"])

    def test_set_route_validation(self):
        router, _ = make_router()
        router.start()
        with self.assertRaises(UnknownRoute):
            router.set_route("nope", volume=0.5)
        with self.assertRaises(RouterError):
            router.set_route("a_to_b", volume=2.0)

    def test_presets(self):
        router, backend = make_router()
        router.start()
        result = router.apply_preset("quiet")
        self.assertEqual(result["routes"]["hdmi_to_a"]["volume"], 0.2)
        result = router.apply_preset("hdmi_off")
        self.assertTrue(result["routes"]["hdmi_to_a"]["mute"])
        self.assertEqual(result["routes"]["hdmi_to_a"]["volume"], 0.2, "mute-only preset keeps volume")
        with self.assertRaises(UnknownPreset):
            router.apply_preset("nope")
        self.assertEqual(router.list_presets()["hdmi_off"], {"hdmi_to_a": {"mute": True}})

    def test_reset_to_config(self):
        router, _ = make_router()
        router.start()
        router.set_route("hdmi_to_a", volume=0.1, mute=True)
        st = router.reset_to_config()["routes"]["hdmi_to_a"]
        self.assertEqual(st["volume"], 0.6)
        self.assertFalse(st["mute"])

    def test_missing_device_reported_not_fatal(self):
        backend = fake_backend()
        backend.remove_node("alsa_output.b")
        router, _ = make_router(backend=backend)
        router.start()
        st = router.route_status("a_to_b")
        self.assertTrue(st["running"])
        self.assertTrue(st["source_present"])
        self.assertFalse(st["sink_present"])
        self.assertFalse(st["connected"])
        self.assertEqual(st["volume"], 1.0)

    def test_crashed_loopback_is_respawned_and_volume_reapplied(self):
        router, backend = make_router()
        router.start()
        router.set_route("hdmi_to_a", volume=0.4)
        proc = router.procs["hdmi_to_a"]
        proc.crash()
        self.assertIsNone(backend.graph().by_name("tfcz.hdmi_to_a.out"))
        clock = [100.0]
        router._clock = lambda: clock[0]
        router.reconcile()  # notices exit, schedules retry
        self.assertNotIn("hdmi_to_a", router.procs)
        self.assertFalse(router.route_status("hdmi_to_a")["running"])
        clock[0] += 60
        router.reconcile()  # respawns
        self.assertIn("hdmi_to_a", router.procs)
        node = backend.graph().by_name("tfcz.hdmi_to_a.out")
        self.assertIsNotNone(node)
        self.assertEqual(node.volume, 0.4)

    def test_stop_terminates_children(self):
        router, backend = make_router()
        router.start()
        procs = list(router.procs.values())
        router.stop()
        self.assertTrue(all(p.terminated for p in procs))
        self.assertEqual(router.procs, {})
        self.assertIsNone(backend.graph().by_name("tfcz.obsmic"))

    def test_state_persist_and_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            cfg = minimal_config(state_file=state)
            router, _ = make_router(cfg=cfg)
            router.start()
            router.set_route("hdmi_to_a", volume=0.33, mute=True)
            data = json.loads(state.read_text())
            self.assertEqual(data["routes"]["hdmi_to_a"], {"volume": 0.33, "mute": True})

            cfg2 = minimal_config(state_file=state)
            router2, backend2 = make_router(cfg=cfg2)
            router2.start()
            self.assertEqual(router2.desired["hdmi_to_a"].volume, 0.33)
            self.assertTrue(backend2.graph().by_name("tfcz.hdmi_to_a.out").mute)

    def test_state_ignores_unknown_and_garbage(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text('{"routes": {"ghost": {"volume": 0.1}, "a_to_b": {"volume": "x"}, "b_to_a": {"volume": 9}}}')
            router, _ = make_router(cfg=minimal_config(state_file=state))
            self.assertEqual(router.desired["a_to_b"].volume, 1.0)
            self.assertEqual(router.desired["b_to_a"].volume, 1.5, "clamped to max")

    def test_devices_listing(self):
        router, _ = make_router()
        names = {d["name"] for d in router.devices()}
        self.assertIn("alsa_input.hdmi", names)

    def test_virtual_key_not_a_route(self):
        router, _ = make_router()
        router.start()
        self.assertNotIn(VIRTUAL, router.status()["routes"])
        self.assertEqual(router.cfg.routes["a_to_obs"].sink, OBS_MIC)


class LatencyRequestTests(unittest.TestCase):
    def test_explicit_latency_is_requested_on_both_sides(self):
        from tfcz_audio.pw import Graph
        from tfcz_audio.router import resolve_devices, virtual_spec

        cfg = minimal_config()
        cfg.audio.latency = "512/48000"
        spec = route_spec(cfg, cfg.routes["a_to_b"], resolve_devices(cfg, Graph()))
        self.assertEqual(spec.capture_props["node.latency"], "512/48000")
        self.assertEqual(spec.playback_props["node.latency"], "512/48000")
        v = virtual_spec(cfg)
        self.assertEqual(v.capture_props["node.latency"], "512/48000")

    def test_auto_asks_for_nothing_anywhere(self):
        from tfcz_audio.pw import Graph
        from tfcz_audio.router import resolve_devices, virtual_spec

        cfg = minimal_config()
        self.assertEqual(cfg.audio.latency, "auto")
        for spec in [route_spec(cfg, r, resolve_devices(cfg, Graph())) for r in cfg.routes.values()] + [virtual_spec(cfg)]:
            self.assertNotIn("node.latency", spec.capture_props)
            self.assertNotIn("node.latency", spec.playback_props)

    def test_latency_value_is_validated(self):
        import tomllib

        from tfcz_audio.config import ConfigError, parse

        from .helpers import MINIMAL

        parse(tomllib.loads('[audio]\nlatency = "auto"\n' + MINIMAL))
        parse(tomllib.loads('[audio]\nlatency = "512/48000"\n' + MINIMAL))
        with self.assertRaises(ConfigError):
            parse(tomllib.loads('[audio]\nlatency = "low"\n' + MINIMAL))
