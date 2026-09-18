"""One microphone for OBS, or one per person.

Separate ones are the point where OBS can filter each voice on its own; a
shared one is what every existing installation has, so both have to work and
switching between them must not need a reinstall.
"""

import tempfile
import tomllib
import unittest
from pathlib import Path

from tfcz_audio import edit
from tfcz_audio.config import OBS_MIC, ConfigError, parse, save, to_dict
from tfcz_audio.meters import OBS_KEY, meter_specs, obs_meter_key
from tfcz_audio.router import Router, virtual_key, virtual_names, virtual_spec

from .helpers import MINIMAL, fake_backend, minimal_config

TWO = """
[virtual.outputs.obs_mic_a]
description = "TFCZ Hans"
[virtual.outputs.obs_mic_b]
description = "TFCZ Karl"
"""


def two_config(extra: str = ""):
    """The usual config, but with two microphones for OBS. The route that fed
    the shared one has to follow along -- a route to a microphone that is not
    declared is refused, which is the point of the check."""
    return parse(tomllib.loads(MINIMAL.replace('to = "obs_mic"', 'to = "obs_mic_a"') + TWO + extra))


class ConfigTests(unittest.TestCase):
    def test_without_a_section_there_is_exactly_the_microphone_there_always_was(self):
        cfg = minimal_config()
        self.assertEqual(list(cfg.virtual.outputs), [OBS_MIC])
        out = cfg.virtual.outputs[OBS_MIC]
        self.assertEqual((out.mic_name, out.mix_name), ("tfcz.obsmic", "tfcz.obsmix"))
        self.assertFalse(cfg.virtual.separate)

    def test_two_outputs_get_their_own_node_names(self):
        cfg = two_config()
        self.assertEqual(sorted(cfg.virtual.outputs), ["obs_mic_a", "obs_mic_b"])
        self.assertEqual(cfg.virtual.outputs["obs_mic_a"].mic_name, "tfcz.obsmic.a")
        self.assertEqual(cfg.virtual.outputs["obs_mic_b"].mix_name, "tfcz.obsmix.b")
        self.assertTrue(cfg.virtual.separate)

    def test_a_route_may_target_any_of_them(self):
        cfg = two_config('\n[routes.x]\nfrom = "a_mic"\nto = "obs_mic_b"\n')
        self.assertEqual(cfg.routes["x"].sink_ref, "obs_mic_b")

    def test_a_microphone_for_obs_is_never_a_source(self):
        with self.assertRaises(ConfigError) as caught:
            two_config('\n[routes.x]\nfrom = "obs_mic_a"\nto = "a_out"\n')
        self.assertIn("only be used as 'to'", str(caught.exception))

    def test_two_outputs_cannot_share_a_node_name(self):
        with self.assertRaises(ConfigError) as caught:
            minimal_config('\n[virtual.outputs.obs_mic_a]\nmic_name = "tfcz.same"\n'
                           '[virtual.outputs.obs_mic_b]\nmic_name = "tfcz.same"\n')
        self.assertIn("already used", str(caught.exception))

    def test_the_names_stay_inside_our_own_prefix(self):
        with self.assertRaises(ConfigError):
            minimal_config('\n[virtual.outputs.obs_mic_a]\nmic_name = "alsa_input.something"\n')

    def test_a_config_with_one_microphone_is_written_the_way_it_always_was(self):
        data = to_dict(minimal_config())
        self.assertNotIn("outputs", data["virtual"])

    def test_two_microphones_survive_a_round_trip(self):
        again = parse(to_dict(two_config()))
        self.assertEqual([o.description for o in again.virtual.outputs.values()], ["TFCZ Hans", "TFCZ Karl"])


class RouterTests(unittest.TestCase):
    def _router(self, cfg=None):
        router = Router(cfg or minimal_config(), fake_backend(), node_wait=0.05, sleep=lambda s: None)
        router.start()
        return router

    def test_one_loopback_per_microphone(self):
        router = self._router(two_config())
        try:
            names = virtual_names(router.cfg)
            self.assertEqual(len(names), 2)
            self.assertEqual({virtual_key(n) for n in names}, {"obs_mic_a", "obs_mic_b"})
            for name in names:
                self.assertIn(name, router.procs)
            graph = router.backend.graph()
            self.assertIsNotNone(graph.by_name("tfcz.obsmic.a"))
            self.assertIsNotNone(graph.by_name("tfcz.obsmic.b"))
        finally:
            router.stop()

    def test_the_first_one_keeps_its_supervisor_name(self):
        """A saved state file and every log line refer to it."""
        router = self._router()
        try:
            self.assertEqual(virtual_names(router.cfg), ["__virtual__"])
        finally:
            router.stop()

    def test_the_specs_differ_so_one_cannot_overwrite_the_other(self):
        cfg = two_config()
        a, b = virtual_spec(cfg, "obs_mic_a"), virtual_spec(cfg, "obs_mic_b")
        self.assertNotEqual(a.name, b.name)
        self.assertNotEqual(a.capture_props["node.name"], b.capture_props["node.name"])
        self.assertNotEqual(a.playback_props["media.role"], b.playback_props["media.role"])

    def test_status_lists_every_microphone_and_what_feeds_it(self):
        router = self._router(two_config('\n[routes.x]\nfrom = "a_mic"\nto = "obs_mic_b"\n'))
        try:
            st = router.status()
            self.assertTrue(st["separate_obs"])
            mics = {m["key"]: m for m in st["virtual_mics"]}
            self.assertEqual(mics["obs_mic_a"]["feeders"], ["a_to_obs"])
            self.assertEqual(mics["obs_mic_b"]["feeders"], ["x"])
            self.assertTrue(all(m["running"] for m in st["virtual_mics"]))
        finally:
            router.stop()

    def test_a_microphone_nobody_feeds_is_reported(self):
        """With one microphone per person, a single dead one is exactly what
        nobody notices until the stream is out."""
        router = self._router(two_config())  # only obs_mic_a is fed
        try:
            titles = [p["title"] for p in router.problems()]
            self.assertTrue(any("TFCZ Karl" in t and "bekommt nichts" in t for t in titles), titles)
        finally:
            router.stop()

    def test_each_microphone_gets_its_own_meter(self):
        specs = {s.key: s.node for s in meter_specs(two_config(), {})}
        self.assertEqual(specs["obs_mic_a"], "tfcz.obsmic.a")
        self.assertEqual(specs["obs_mic_b"], "tfcz.obsmic.b")
        self.assertEqual(obs_meter_key(OBS_MIC), OBS_KEY, "the single one keeps the short key")


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "config.toml"
        cfg = minimal_config()
        cfg.path = path
        cfg.labels = {"headset_a": "Hans", "headset_b": "Karl"}
        save(cfg, path)
        self.router = Router(cfg, fake_backend(), node_wait=0.05, sleep=lambda s: None)
        self.router.start()
        edit.upsert_route(self.router, "b_to_obs", {"from": "b_mic", "to": "obs_mic", "volume": 1.0})

    def tearDown(self):
        self.router.stop()
        self.tmp.cleanup()

    def test_switching_on_renames_the_routes_with_it(self):
        edit.set_obs_mode(self.router, True)
        targets = {n: r.sink_ref for n, r in self.router.cfg.routes.items() if "obs" in r.sink_ref}
        self.assertEqual(targets, {"a_to_obs": "obs_mic_a", "b_to_obs": "obs_mic_b"})
        # the descriptions name whoever really feeds them: these devices are
        # called a_mic/b_mic, so that is what they say
        self.assertEqual([m["description"] for m in self.router.virtual_mics(self.router.backend.graph())],
                         ["TFCZ A", "TFCZ B"])

    def test_switching_back_leaves_one_microphone_and_one_target(self):
        edit.set_obs_mode(self.router, True)
        edit.set_obs_mode(self.router, False)
        self.assertEqual(list(self.router.cfg.virtual.outputs), [OBS_MIC])
        self.assertTrue(all(r.sink_ref == OBS_MIC for r in self.router.cfg.routes.values() if "obs" in r.sink_ref))
        graph = self.router.backend.graph()
        self.assertIsNotNone(graph.by_name("tfcz.obsmic"))
        self.assertIsNone(graph.by_name("tfcz.obsmic.a"), "the extra microphone is gone from the graph")

    def test_the_change_is_written_to_the_config_file(self):
        edit.set_obs_mode(self.router, True)
        data = tomllib.loads(self.router.cfg.path.read_text())
        self.assertEqual(sorted(data["virtual"]["outputs"]), ["obs_mic_a", "obs_mic_b"])

    def test_switching_on_without_two_routes_is_refused_with_a_reason(self):
        edit.delete_route(self.router, "b_to_obs")
        with self.assertRaises(edit.EditError) as caught:
            edit.set_obs_mode(self.router, True)
        self.assertIn("zwei Verbindungen", str(caught.exception))

    def test_the_shared_microphone_keeps_running_while_switching(self):
        """Switching must not restart what does not change."""
        before = self.router.procs["__virtual__"]
        edit.set_obs_mode(self.router, False)
        self.assertIs(self.router.procs["__virtual__"], before)


class ApiTests(unittest.TestCase):
    """The page switches the mode over one endpoint; the spec must know it and
    the wizard must keep whatever is set."""

    def test_the_endpoint_is_documented(self):
        from tfcz_audio import openapi

        self.assertIn("/config/obs-mode", openapi.document()["paths"])

    def test_setup_keeps_the_current_mode_unless_told_otherwise(self):
        from tfcz_audio.edit import obs_outputs

        self.assertEqual(obs_outputs(False, "Hans", "Karl"), {})
        two = obs_outputs(True, "Hans", "Karl")
        self.assertEqual([o["description"] for o in two.values()], ["TFCZ Hans", "TFCZ Karl"])

    def test_the_page_offers_both_microphones_as_a_target(self):
        from importlib import resources

        page = resources.files("tfcz_audio").joinpath("ui.html").read_text()
        self.assertIn('id="obs-separate"', page)
        self.assertIn("virtual_mics", page)
        self.assertNotIn("state.levels.obs;", page, "the meter key comes from the status, not from a fixed name")


class DiagramTests(unittest.TestCase):
    def test_the_columns_are_stacked_by_their_own_heights(self):
        """A box for OBS is taller than a device box. With a fixed row height
        the first one would overlap the next, which is what happened with two."""
        from importlib import resources

        page = resources.files("tfcz_audio").joinpath("ui.html").read_text()
        self.assertIn("const stack = (aliases, side, x)", page)
        self.assertNotIn("top + i * (nodeH + gap)", page, "no fixed row height any more")


class StableNameTests(unittest.TestCase):
    """OBS stores the node name in the scene. It must survive everything that
    is not an explicit change of the setup, and no two may ever collide."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"
        cfg = minimal_config()
        cfg.path = self.path
        cfg.labels = {"headset_a": "Hans", "headset_b": "Karl"}
        save(cfg, self.path)
        self.router = Router(cfg, fake_backend(), node_wait=0.05, sleep=lambda s: None)
        self.router.start()
        edit.upsert_route(self.router, "b_to_obs", {"from": "b_mic", "to": "obs_mic", "volume": 1.0})
        edit.set_obs_mode(self.router, True)

    def tearDown(self):
        self.router.stop()
        self.tmp.cleanup()

    def names(self):
        return [o.mic_name for o in self.router.cfg.virtual.outputs.values()]

    def test_the_names_are_derived_from_the_key_not_from_discovery(self):
        self.assertEqual(self.names(), ["tfcz.obsmic.a", "tfcz.obsmic.b"])

    def test_they_survive_a_restart(self):
        reloaded = parse(tomllib.loads(self.path.read_text()))
        self.assertEqual([o.mic_name for o in reloaded.virtual.outputs.values()], self.names())

    def test_renaming_a_person_changes_the_label_but_not_the_name(self):
        before = self.names()
        edit.set_labels(self.router, {"headset_a": "Hansruedi", "headset_b": "Karl"})
        self.assertEqual(self.names(), before, "OBS would lose the source")
        self.assertEqual([o.description for o in self.router.cfg.virtual.outputs.values()],
                         ["TFCZ Hansruedi", "TFCZ Karl"])

    def test_changing_a_volume_or_a_route_leaves_them_alone(self):
        before = self.names()
        self.router.set_route("a_to_obs", volume=0.5)
        edit.upsert_route(self.router, "a_to_b", {"from": "a_mic", "to": "b_out", "volume": 0.3})
        self.assertEqual(self.names(), before)

    def test_two_microphones_can_never_share_a_name(self):
        with self.assertRaises(ConfigError):
            parse(tomllib.loads(MINIMAL.replace('to = "obs_mic"', 'to = "obs_mic_a"')
                                + '[virtual.outputs.obs_mic_a]\nmic_name = "tfcz.x"\n'
                                  '[virtual.outputs.obs_mic_b]\nmic_name = "tfcz.x"\n'))

    def test_a_name_is_never_reused_for_the_other_person(self):
        """obs_mic_a belongs to the route from headset A, in both directions of
        the switch."""
        targets = {n: r.sink_ref for n, r in self.router.cfg.routes.items() if "obs" in r.sink_ref}
        self.assertEqual(targets["a_to_obs"], "obs_mic_a")
        edit.set_obs_mode(self.router, False)
        edit.set_obs_mode(self.router, True)
        targets = {n: r.sink_ref for n, r in self.router.cfg.routes.items() if "obs" in r.sink_ref}
        self.assertEqual(targets["a_to_obs"], "obs_mic_a")
        self.assertEqual(targets["b_to_obs"], "obs_mic_b")


class AssignmentTests(unittest.TestCase):
    """Which voice ends up on which microphone. Getting this wrong is silent:
    the stream carries the right sound under the wrong name."""

    HEADSETS = (
        '[devices]\n'
        'headset_a_mic = "alsa_input.a"\n'
        'headset_b_mic = "alsa_input.b"\n'
        '[labels]\n'
        'headset_a = "Hans"\n'
        'headset_b = "Karl"\n'
    )

    def route(self, name, source):
        return f'[routes.{name}]\nfrom = "{source}"\nto = "obs_mic"\n'

    def _switch(self, routes):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.toml"
        cfg = parse(tomllib.loads(self.HEADSETS + routes))
        cfg.path = path
        save(cfg, path)
        router = Router(cfg, fake_backend(), node_wait=0.05, sleep=lambda s: None)
        router.start()
        self.addCleanup(router.stop)
        edit.set_obs_mode(router, True)
        return {name: (r.sink_ref, router.cfg.virtual.outputs[r.sink_ref].description)
                for name, r in router.cfg.routes.items() if "obs" in r.sink_ref}

    def test_route_names_in_the_wrong_order_do_not_swap_the_people(self):
        result = self._switch(self.route("zebra", "headset_a_mic") + self.route("alpha", "headset_b_mic"))
        self.assertEqual(result["zebra"], ("obs_mic_a", "TFCZ Hans"))
        self.assertEqual(result["alpha"], ("obs_mic_b", "TFCZ Karl"))

    def test_the_usual_names_end_up_the_usual_way(self):
        result = self._switch(self.route("a_to_obs", "headset_a_mic") + self.route("b_to_obs", "headset_b_mic"))
        self.assertEqual(result["a_to_obs"], ("obs_mic_a", "TFCZ Hans"))
        self.assertEqual(result["b_to_obs"], ("obs_mic_b", "TFCZ Karl"))

    def test_two_routes_from_the_same_person_still_get_two_names(self):
        """Two identically named sources in OBS would be a guessing game."""
        result = self._switch(self.route("eins", "headset_a_mic") + self.route("zwei", "headset_a_mic"))
        descriptions = [d for _, d in result.values()]
        self.assertEqual(len(set(descriptions)), 2, descriptions)
