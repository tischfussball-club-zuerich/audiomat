import unittest

class SignalStatsTests(unittest.TestCase):
    """Telling apart what a device delivers: speech, silence, a clipped signal,
    or a compressed stream that is not audio at all."""

    RATE = 48000

    def pcm(self, fn, frames=None):
        import struct

        frames = frames or self.RATE // 2
        return b"".join(struct.pack("<hh", *fn(i)) for i in range(frames))

    def speech(self):
        import math

        return self.pcm(lambda i: (int(9000 * math.sin(i / 60) * (0.3 + 0.7 * abs(math.sin(i / 8000)))),) * 2)

    def test_speech_is_not_complained_about(self):
        from tfcz_audio.meters import signal_stats, signal_verdict

        stats = signal_stats(self.speech())
        self.assertLess(stats["zcr"], 0.1, "a tone crosses zero rarely")
        self.assertEqual(signal_verdict(stats), [])

    def test_a_compressed_stream_is_recognised_as_noise(self):
        """Dolby/DTS over HDMI arrives as loud, structureless data."""
        import random

        from tfcz_audio.meters import signal_stats, signal_verdict

        random.seed(4)
        data = self.pcm(lambda i: (random.randint(-32768, 32767), random.randint(-32768, 32767)))
        stats = signal_stats(data)
        self.assertGreater(stats["zcr"], 0.4)
        titles = [v["title"] for v in signal_verdict(stats)]
        self.assertTrue(any("Rauschen" in t for t in titles), titles)
        self.assertTrue(any("PCM" in v.get("fix", "") for v in signal_verdict(stats)))

    def test_quiet_hiss_is_not_called_noise(self):
        """A microphone in a quiet room is also random -- but quiet. Flagging it
        would make the check useless."""
        import random

        from tfcz_audio.meters import signal_stats, signal_verdict

        random.seed(5)
        data = self.pcm(lambda i: (random.randint(-500, 500), random.randint(-500, 500)))
        titles = [v["title"] for v in signal_verdict(signal_stats(data))]
        self.assertFalse(any("Rauschen" in t for t in titles), titles)

    def test_digital_silence_is_reported_once(self):
        from tfcz_audio.meters import signal_stats, signal_verdict

        verdicts = signal_verdict(signal_stats(self.pcm(lambda i: (0, 0))))
        self.assertEqual(len(verdicts), 1)
        self.assertIn("Stille", verdicts[0]["title"])

    def test_clipping_is_measured_not_guessed(self):
        import math

        from tfcz_audio.meters import signal_stats, signal_verdict

        data = self.pcm(lambda i: (max(-32768, min(32767, int(60000 * math.sin(i / 50)))),) * 2)
        stats = signal_stats(data)
        self.assertGreater(stats["clipped"], 0.1)
        self.assertTrue(any("bersteuert" in v["title"] for v in signal_verdict(stats)))

    def test_one_dead_channel_is_named(self):
        import math

        from tfcz_audio.meters import signal_stats, signal_verdict

        data = self.pcm(lambda i: (int(9000 * math.sin(i / 60)), 0))
        stats = signal_stats(data)
        self.assertGreater(stats["channels"][0], 0.2)
        self.assertEqual(stats["channels"][1], 0.0)
        self.assertTrue(any("ein Kanal" in v["title"] for v in signal_verdict(stats)))

    def test_channels_are_measured_apart(self):
        """Interleaved samples would fake a zero crossing on every other value
        as soon as the two channels differ."""
        import math

        from tfcz_audio.meters import signal_stats

        data = self.pcm(lambda i: (int(9000 * math.sin(i / 60)), 0))
        self.assertLess(signal_stats(data)["zcr"], 0.05)

    def test_odd_and_tiny_buffers_do_not_raise(self):
        from tfcz_audio.meters import signal_stats, signal_verdict

        for chunk in (b"", b"\x00", b"\x01\x02\x03", bytes(range(7)), bytes(range(9))):
            stats = signal_stats(chunk)
            self.assertLessEqual(stats["frames"], 2, chunk)
            verdicts = signal_verdict(stats)
            self.assertEqual(len(verdicts), 1, "a buffer this short can only say one thing")
            self.assertIn(verdicts[0]["level"], ("error", "warning"))
