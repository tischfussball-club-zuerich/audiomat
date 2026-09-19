"""The test tone: the only check for what leaves the router.

A level bar cannot tell you that the headphone on person A's head is the one
the page calls "Hans", that both ears work, or that left is left.
"""

import io
import struct
import threading
import time
import unittest
import wave
from unittest import mock

from tfcz_audio import tone
from tfcz_audio.router import Router, RouterError

from .helpers import fake_backend, minimal_config


def channels_of(data: bytes) -> tuple[int, int]:
    with wave.open(io.BytesIO(data)) as handle:
        frames = handle.readframes(handle.getnframes())
    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    return max(abs(s) for s in samples[0::2]), max(abs(s) for s in samples[1::2])


class WavTests(unittest.TestCase):
    def test_one_side_means_silence_on_the_other(self):
        """That is the whole point: it turns "I hear something" into "I hear it
        on the left", which is what finds a swapped pair."""
        left, right = channels_of(tone.make_wav("left", 0.3))
        self.assertGreater(left, 1000)
        self.assertEqual(right, 0)
        left, right = channels_of(tone.make_wav("right", 0.3))
        self.assertEqual(left, 0)
        self.assertGreater(right, 1000)
        left, right = channels_of(tone.make_wav("both", 0.3))
        self.assertGreater(min(left, right), 1000)

    def test_it_is_never_loud_enough_to_hurt(self):
        """It plays into headphones somebody is wearing."""
        for side in tone.SIDES:
            peak = max(channels_of(tone.make_wav(side, 0.3)))
            self.assertLess(peak, 32768 * 0.3, "louder than a quarter of full scale")

    def test_it_starts_and_ends_quietly(self):
        """A square edge at the start is a click in someone's ear."""
        data = tone.make_wav("both", 0.5)
        with wave.open(io.BytesIO(data)) as handle:
            frames = handle.readframes(handle.getnframes())
        samples = struct.unpack(f"<{len(frames) // 2}h", frames)
        self.assertLess(abs(samples[0]), 100)
        self.assertLess(abs(samples[-1]), 100)

    def test_the_length_is_bounded(self):
        for asked, expected in ((0.0, 0.2), (99.0, tone.MAX_SECONDS)):
            with wave.open(io.BytesIO(tone.make_wav("both", asked))) as handle:
                self.assertAlmostEqual(handle.getnframes() / handle.getframerate(), expected, places=1)

    def test_a_nonsense_side_is_refused(self):
        with self.assertRaises(ValueError):
            tone.make_wav("upside-down", 0.3)


class PlayerTests(unittest.TestCase):
    def wait(self, player, limit=5.0):
        deadline = time.monotonic() + limit
        while player.state()["running"] and time.monotonic() < deadline:
            time.sleep(0.02)
        return player.state()

    def test_the_tone_goes_to_the_named_device_not_the_default(self):
        seen = []

        def fake_run(cmd, **kwargs):
            seen.append(cmd)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(tone.shutil, "which", lambda name: f"/usr/bin/{name}"), \
             mock.patch.object(tone.subprocess, "run", fake_run):
            player = tone.TonePlayer()
            player.play("alsa_output.headset_a", "Hans Kopfhörer", "left", 0.3)
            state = self.wait(player)
        self.assertEqual(state["error"], "")
        self.assertIn("alsa_output.headset_a", " ".join(seen[0]))
        self.assertIn("--target", seen[0])

    def test_a_failing_player_is_retried_with_the_next_one(self):
        attempts = []

        def fake_run(cmd, **kwargs):
            attempts.append(cmd[0])
            return mock.Mock(returncode=0 if cmd[0] == "paplay" else 1, stdout="", stderr="no such target")

        with mock.patch.object(tone.shutil, "which", lambda name: f"/usr/bin/{name}"), \
             mock.patch.object(tone.subprocess, "run", fake_run):
            player = tone.TonePlayer()
            player.play("x", "X", "both", 0.3)
            state = self.wait(player)
        self.assertEqual(state["error"], "")
        self.assertEqual(attempts[-1], "paplay")

    def test_without_any_player_it_says_which_package_to_install(self):
        with mock.patch.object(tone.shutil, "which", lambda name: None):
            player = tone.TonePlayer()
            player.play("x", "X", "both", 0.3)
            state = self.wait(player)
        self.assertIn("pw-play", state["error"])
        self.assertIn("pipewire-bin", state["error"])

    def test_the_temporary_file_is_cleaned_up(self):
        import os
        import tempfile

        created = []
        real_named = tempfile.NamedTemporaryFile

        def watched(*args, **kwargs):
            handle = real_named(*args, **kwargs)
            created.append(handle.name)
            return handle

        with mock.patch.object(tone.shutil, "which", lambda name: f"/usr/bin/{name}"), \
             mock.patch.object(tone.subprocess, "run", lambda cmd, **kw: mock.Mock(returncode=0, stdout="", stderr="")), \
             mock.patch.object(tone.tempfile, "NamedTemporaryFile", watched):
            player = tone.TonePlayer()
            player.play("x", "X", "both", 0.3)
            self.wait(player)
        self.assertTrue(created)
        for path in created:
            self.assertFalse(os.path.exists(path), "the wav file was left behind")

    def test_only_one_tone_at_a_time(self):
        player = tone.TonePlayer()
        player.running = True
        result = player.play("x", "X", "both", 0.3)
        self.assertFalse(result["started"])
        self.assertIn("bereits", result["problem"])

    def test_a_second_request_answers_instead_of_hanging(self):
        player = tone.TonePlayer()
        player.running = True
        done = threading.Event()
        threading.Thread(target=lambda: (player.play("x", "X", "both", 0.3), done.set()), daemon=True).start()
        self.assertTrue(done.wait(timeout=5), "play() deadlocked on its own lock")

    def test_a_player_that_hangs_is_cut_off(self):
        import subprocess as sp

        def hanging(cmd, **kwargs):
            raise sp.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 1))

        with mock.patch.object(tone.shutil, "which", lambda name: "/usr/bin/pw-play"), \
             mock.patch.object(tone.subprocess, "run", hanging):
            player = tone.TonePlayer()
            player.play("x", "X", "both", 0.3)
            state = self.wait(player)
        self.assertIn("abgebrochen", state["error"].lower())


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.router = Router(minimal_config(), fake_backend(), node_wait=0.05, sleep=lambda s: None)
        self.router.start()
        self.addCleanup(self.router.stop)

    def test_only_devices_that_play_sound_are_offered(self):
        aliases = [t["alias"] for t in self.router.output_targets()]
        self.assertIn("a_out", aliases)
        self.assertIn("b_out", aliases)
        self.assertNotIn("a_mic", aliases, "a tone at a microphone would do nothing")
        self.assertNotIn("hdmi", aliases)

    def test_a_microphone_is_refused_with_a_reason(self):
        with self.assertRaises(RouterError) as caught:
            self.router.tone_target("a_mic")
        self.assertIn("kein Ausgabegerät", str(caught.exception))

    def test_an_unknown_device_is_refused_in_german(self):
        with self.assertRaises(RouterError) as caught:
            self.router.tone_target("gibtsnicht")
        self.assertIn("nicht eingerichtet", str(caught.exception))

    def test_a_missing_device_is_refused_before_anything_plays(self):
        graph = self.router.backend.graph()
        node = graph.by_name("alsa_output.a")
        del graph.nodes[node.id]
        self.router.refresh_devices()
        with self.assertRaises(RouterError) as caught:
            self.router.tone_target("a_out")
        self.assertIn("nicht angeschlossen", str(caught.exception))


class WizardToneTests(unittest.TestCase):
    """The wizard plays a tone before anything is configured -- that is the
    moment the question matters: which headset is this one?"""

    def setUp(self):
        self.router = Router(minimal_config(), fake_backend(), node_wait=0.05, sleep=lambda s: None)
        self.router.start()
        self.addCleanup(self.router.stop)

    def test_a_plain_node_name_works_as_a_target(self):
        node, label = self.router.tone_target("alsa_output.a")
        self.assertEqual(node, "alsa_output.a")
        self.assertTrue(label)

    def test_a_node_that_records_is_still_refused(self):
        with self.assertRaises(RouterError) as caught:
            self.router.tone_target("alsa_input.a")
        self.assertIn("kein Ausgabegerät", str(caught.exception))

    def test_something_that_is_neither_is_refused(self):
        with self.assertRaises(RouterError) as caught:
            self.router.tone_target("alsa_output.does-not-exist")
        self.assertIn("nicht eingerichtet", str(caught.exception))

    def test_the_wizard_offers_the_button_without_selecting_the_headset(self):
        from importlib import resources

        page = resources.files("tfcz_audio").joinpath("ui.html").read_text()
        self.assertIn("data-tone-node", page)
        self.assertIn("e.stopPropagation()", page)


class SideParsingTests(unittest.TestCase):
    """The side is written into a URL by hand as often as it is clicked."""

    def test_case_and_whitespace_do_not_matter(self):
        for text in ("LEFT", " left ", "Left", "lEfT"):
            self.assertEqual(tone.clean_side(text), "left")

    def test_nothing_means_both_sides(self):
        for text in ("", None, "   "):
            self.assertEqual(tone.clean_side(text), "both")

    def test_something_else_is_refused_in_german(self):
        with self.assertRaises(ValueError) as caught:
            tone.clean_side("diagonal")
        self.assertIn("Seite", str(caught.exception))
        self.assertNotIn("must be", str(caught.exception))


class NeverOnTheStreamTests(unittest.TestCase):
    """The mix bus for OBS is a sink like any other. A tone into it would go
    out on the stream and into both headsets at once."""

    def setUp(self):
        self.router = Router(minimal_config(), fake_backend(), node_wait=0.05, sleep=lambda s: None)
        self.router.start()
        self.router.reconcile()
        self.addCleanup(self.router.stop)

    def test_our_own_channels_are_refused(self):
        for name in ("tfcz.obsmix", "tfcz.obsmic", "tfcz.a_to_b.out"):
            with self.subTest(name), self.assertRaises(RouterError) as caught:
                self.router.tone_target(name)
            self.assertIn("echte Geräte", str(caught.exception))

    def test_real_devices_are_still_allowed(self):
        node, _ = self.router.tone_target("alsa_output.a")
        self.assertEqual(node, "alsa_output.a")

    def test_the_listed_targets_never_contain_our_own(self):
        for entry in self.router.output_targets():
            self.assertFalse(entry["node"].startswith("tfcz."), entry)
