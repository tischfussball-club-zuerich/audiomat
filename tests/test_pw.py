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
