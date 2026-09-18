import json
import unittest

from tfcz_audio.pw import (
    LoopbackSpec,
    cubic_to_db,
    cubic_to_linear,
    db_to_cubic,
    linear_to_cubic,
    parse_dump,
    spa_json,
)

DUMP = json.dumps(
    [
        {
            "id": 40,
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {
                    "node.name": "alsa_input.usb-Headset-00.mono-fallback",
                    "node.description": "Headset Mono",
                    "media.class": "Audio/Source",
                },
                "params": {"Props": [{"volume": 1.0, "mute": False, "channelVolumes": [1.0]}]},
            },
        },
        {
            "id": 41,
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {"node.name": "tfcz.a_to_b.out", "media.class": "Stream/Output/Audio"},
                "params": {"Props": [{"volume": 1.0, "mute": True, "channelVolumes": [0.125, 0.125]}]},
            },
        },
        {"id": 42, "type": "PipeWire:Interface:Client", "info": {"props": {}}},
        {
            "id": 50,
            "type": "PipeWire:Interface:Link",
            "info": {"output-node-id": 40, "input-node-id": 41},
        },
    ]
)


class ParseDumpTests(unittest.TestCase):
    def test_nodes_and_links(self):
        g = parse_dump(DUMP)
        self.assertEqual(set(g.nodes), {40, 41})
        mic = g.by_name("alsa_input.usb-Headset-00.mono-fallback")
        self.assertEqual(mic.description, "Headset Mono")
        self.assertEqual(mic.media_class, "Audio/Source")
        self.assertEqual(mic.volume, 1.0)
        self.assertFalse(mic.mute)
        out = g.by_name("tfcz.a_to_b.out")
        self.assertAlmostEqual(out.volume, 0.5, places=3)  # cbrt(0.125)
        self.assertTrue(out.mute)
        self.assertTrue(g.has_output_link(40))
        self.assertTrue(g.has_input_link(41))
        self.assertFalse(g.has_input_link(40))

    def test_tolerates_concatenated_documents(self):
        g = parse_dump(DUMP + "\n" + DUMP)
        self.assertEqual(len(g.nodes), 2)

    def test_audio_devices_filter(self):
        g = parse_dump(DUMP)
        self.assertEqual([n.id for n in g.audio_devices()], [40])


class VolumeMathTests(unittest.TestCase):
    def test_roundtrip(self):
        for v in (0.0, 0.25, 0.5, 1.0, 1.5):
            self.assertAlmostEqual(linear_to_cubic(cubic_to_linear(v)), v, places=6)

    def test_db(self):
        self.assertEqual(cubic_to_db(1.0), 0.0)
        self.assertAlmostEqual(cubic_to_db(0.5), -18.06, places=2)
        self.assertIsNone(cubic_to_db(0.0))
        self.assertAlmostEqual(db_to_cubic(-18.06), 0.5, places=3)


class LoopbackSpecTests(unittest.TestCase):
    def test_spa_json(self):
        s = spa_json({"node.name": "x y", "node.dont-fallback": True, "audio.position": ["FL", "FR"], "n": 2})
        self.assertEqual(s, '{ node.name = "x y" node.dont-fallback = true audio.position = [ FL FR ] n = 2 }')

    def test_command(self):
        spec = LoopbackSpec(
            name="tfcz.r",
            capture_props={"node.name": "tfcz.r.in", "target.object": "src"},
            playback_props={"node.name": "tfcz.r.out", "target.object": "dst"},
        )
        cmd = spec.command()
        self.assertEqual(cmd[:5], ["pw-loopback", "-n", "tfcz.r", "-c", "2"])
        self.assertIn("-m", cmd)
        self.assertTrue(any(a.startswith("--capture-props={ node.name = \"tfcz.r.in\"") for a in cmd))
        self.assertTrue(any('target.object = "dst"' in a for a in cmd if a.startswith("--playback-props=")))
        self.assertEqual(spec.capture_node, "tfcz.r.in")
        self.assertEqual(spec.playback_node, "tfcz.r.out")


class BrokenDumpTests(unittest.TestCase):
    """pw-dump output that cannot be read means the same as a dead pw-dump, and
    has to arrive as the same error -- everything already handles that one."""

    def test_garbage_raises_the_declared_error(self):
        from tfcz_audio.pw import PwError, parse_dump

        for text in ("", "not json", "<html>nope</html>", "{", "\x00\x01\x02"):
            if text == "":
                self.assertEqual(len(parse_dump(text).nodes), 0)
                continue
            with self.assertRaises(PwError, msg=text):
                parse_dump(text)

    def test_one_unreadable_object_does_not_cost_the_graph(self):
        from tfcz_audio.pw import parse_dump

        graph = parse_dump(
            '[{"id": 3, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "keep"}}},'
            ' {"id": 1e999, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "broken"}}},'
            ' {"id": "abc", "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "also-broken"}}}]'
        )
        self.assertEqual([n.name for n in graph.nodes.values()], ["keep"])

    def test_a_link_with_impossible_ids_is_skipped_not_fatal(self):
        from tfcz_audio.pw import parse_dump

        graph = parse_dump(
            '[{"id": 9, "type": "PipeWire:Interface:Link", "info": {"output-node-id": 1e999, "input-node-id": 2}},'
            ' {"id": 10, "type": "PipeWire:Interface:Link", "info": {"output-node-id": 1, "input-node-id": 2}}]'
        )
        self.assertEqual([(l.output_node, l.input_node) for l in graph.links], [(1, 2)])

    def test_the_daemon_treats_it_as_pipewire_being_unreachable(self):
        from tfcz_audio.pw import PwError
        from tfcz_audio.router import Router

        from .helpers import fake_backend, minimal_config

        backend = fake_backend()
        backend.graph = lambda *a, **k: (_ for _ in ()).throw(PwError("cannot read the output of pw-dump"))
        router = Router(minimal_config(), backend, node_wait=0.01, sleep=lambda s: None)
        router.start()
        try:
            router.reconcile()  # must not raise
            self.assertIn("cannot talk to PipeWire", router.last_error)
        finally:
            router.stop()


class DrainedProcessTests(unittest.TestCase):
    """The stderr reader must end with the helper, even when the helper leaves
    a child holding the pipe: one leaked thread per restart adds up on a
    machine that runs for weeks."""

    def test_the_reader_ends_when_the_helper_is_terminated(self):
        import subprocess
        import threading
        import time

        from tfcz_audio.pw import DrainedProcess

        before = threading.active_count()
        # sh keeps the pipe open through its child, so terminating sh alone
        # would leave the reader waiting
        procs = [DrainedProcess(subprocess.Popen(["/bin/sh", "-c", "sleep 30"],
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
                 for _ in range(10)]
        self.assertGreaterEqual(threading.active_count(), before + 10)
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        deadline = time.monotonic() + 5
        while threading.active_count() > before and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertLessEqual(threading.active_count(), before, "stderr readers outlived their helpers")

    def test_the_error_message_survives_the_cleanup(self):
        import subprocess
        import time

        from tfcz_audio.pw import DrainedProcess

        proc = DrainedProcess(subprocess.Popen(["/bin/sh", "-c", "echo 'cannot connect' >&2; exit 1"],
                                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
        deadline = time.monotonic() + 5
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn("cannot connect", proc.stderr_tail)
        self.assertIn("cannot connect", proc.stderr_head)
