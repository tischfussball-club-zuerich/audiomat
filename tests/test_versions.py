import shutil
import unittest
from unittest import mock

from tfcz_audio import versions


class CollectTests(unittest.TestCase):
    def setUp(self):
        versions._cache = None

    def test_every_group_is_present_and_shaped_the_same(self):
        report = versions.collect(fresh=True)
        titles = [g["title"] for g in report["groups"]]
        self.assertEqual(titles, ["Dieses Programm", "Tonsystem", "Aufnahmekarte", "Werkzeuge", "Pakete"])
        for group in report["groups"]:
            self.assertTrue(group["items"], group["title"])
            for item in group["items"]:
                self.assertEqual(set(item), {"name", "version", "detail", "level"})
                self.assertTrue(item["name"])
                self.assertTrue(item["version"])

    def test_this_program_reports_its_own_version(self):
        from tfcz_audio import __version__

        own = versions.collect(fresh=True)["groups"][0]["items"]
        self.assertEqual(own[0]["version"], __version__)

    def test_tools_that_would_touch_the_audio_graph_are_never_executed(self):
        """pw-loopback would create a node, pw-record would open a device and
        pw-top would never exit: locating them on disk has to be enough."""
        calls = []

        def fake_run(cmd, timeout=3.0):
            calls.append(cmd)
            return 0, "1.2.3"

        with mock.patch.object(versions, "_run", fake_run), \
             mock.patch.object(shutil, "which", lambda name: f"/usr/bin/{name}"):
            versions.collect(fresh=True)
        started = [cmd[0] for cmd in calls]
        self.assertTrue(started, "nothing ran at all")
        for forbidden in versions.NEVER_RUN:
            self.assertNotIn(forbidden, started)

    def test_a_missing_tool_says_how_to_install_it(self):
        with mock.patch.object(shutil, "which", lambda name: None):
            report = versions.collect(fresh=True)
        tools = next(g for g in report["groups"] if g["title"] == "Werkzeuge")
        entry = next(i for i in tools["items"] if i["name"] == "pw-loopback")
        self.assertEqual(entry["level"], "missing")
        self.assertIn("apt install pipewire-bin", entry["detail"])

    def test_a_pulseaudio_server_is_flagged_as_a_problem(self):
        def fake_run(cmd, timeout=3.0):
            if cmd[:2] == ["pactl", "info"]:
                return 0, "Server Name: pulseaudio\nServer Version: 16.1"
            return 0, "1.2.3"

        with mock.patch.object(versions, "_run", fake_run), \
             mock.patch.object(shutil, "which", lambda name: f"/usr/bin/{name}"):
            report = versions.collect(fresh=True)
        sound = next(g for g in report["groups"] if g["title"] == "Tonsystem")
        server = next(i for i in sound["items"] if i["name"] == "Tonserver")
        self.assertEqual(server["level"], "bad")
        self.assertIn("kein PipeWire", server["detail"])

    def test_the_result_is_cached_so_polling_does_not_spawn_processes(self):
        calls = []

        def fake_run(cmd, timeout=3.0):
            calls.append(cmd)
            return 0, "1.2.3"

        with mock.patch.object(versions, "_run", fake_run):
            versions.collect(fresh=True)
            first = len(calls)
            versions.collect()
            self.assertEqual(len(calls), first)
            versions.collect(fresh=True)
            self.assertGreater(len(calls), first)

    def test_a_hanging_tool_does_not_hang_the_collection(self):
        import subprocess

        def boom(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="x", timeout=1)

        with mock.patch.object(versions.subprocess, "run", boom):
            report = versions.collect(fresh=True)
        self.assertTrue(report["groups"])

    def test_text_form_lists_every_group(self):
        text = versions.as_text(versions.collect(fresh=True))
        for title in ("Dieses Programm", "Tonsystem", "Werkzeuge"):
            self.assertIn(title, text)
