import os
import unittest
from unittest import mock

from tfcz_audio import repair


class PlanTests(unittest.TestCase):
    def setUp(self):
        repair.invalidate()  # the plan and the sudo probe are cached
    def test_a_missing_tool_becomes_a_package_to_install(self):
        with mock.patch.object(repair.shutil, "which", lambda name: None):
            actions = repair.detect()
        install = next(a for a in actions if a.id == "install-packages")
        self.assertTrue(install.needs_root)
        self.assertEqual(install.command[:3], ("apt-get", "install", "-y"))
        self.assertIn("pipewire-bin", install.command)

    def test_nothing_the_caller_sends_reaches_a_command_line(self):
        """The id only picks one of our own entries."""
        self.assertIsNone(repair.action_by_id("rm -rf /"))
        self.assertIsNone(repair.action_by_id("install-packages; reboot"))

    def test_an_action_is_rebuilt_from_a_fresh_check(self):
        with mock.patch.object(repair.shutil, "which", lambda name: None):
            first = repair.action_by_id("install-packages")
            self.assertIsNotNone(first)
        # tools present again -> the entry is gone, not stale
        with mock.patch.object(repair.shutil, "which", lambda name: f"/usr/bin/{name}"), \
             mock.patch.object(repair, "_run", lambda cmd, timeout=5.0: (0, "active")), \
             mock.patch.object(repair, "_unit_active", lambda unit: True):
            self.assertIsNone(repair.action_by_id("install-packages"))

    def test_the_plan_is_cached_but_a_repair_clears_it(self):
        with mock.patch.object(repair.shutil, "which", lambda name: None):
            first = repair.plan()
            self.assertTrue(first["actions"])
            self.assertIs(repair.plan(), first)          # same object: served from the cache
            self.assertIsNot(repair.plan(fresh=True), first)
        repair.invalidate()
        self.assertIsNot(repair.plan(), first)

    def test_root_is_never_taken_quietly(self):
        with mock.patch.object(repair.shutil, "which", lambda name: None), \
             mock.patch.dict(os.environ, {"DISPLAY": "", "WAYLAND_DISPLAY": ""}, clear=False), \
             mock.patch.object(repair.os, "getuid", lambda: 1000):
            mode = repair.root_mode()
        self.assertFalse(mode["available"])
        self.assertIn("Terminal", mode["why"])

    def test_passwordless_sudo_is_used_when_it_exists(self):
        with mock.patch.object(repair.shutil, "which", lambda name: f"/usr/bin/{name}"), \
             mock.patch.object(repair, "_run", lambda cmd, timeout=5.0: (0, "")), \
             mock.patch.object(repair.os, "getuid", lambda: 1000):
            mode = repair.root_mode()
        self.assertEqual(mode["how"], "sudo")
        self.assertEqual(repair._elevate(("apt-get", "install"), mode), ["sudo", "-n", "apt-get", "install"])

    def test_the_deep_one_is_never_offered_as_a_button(self):
        """Swapping PulseAudio for PipeWire changes the whole machine."""
        with mock.patch.object(repair.shutil, "which", lambda name: f"/usr/bin/{name}"), \
             mock.patch.object(repair, "_run", lambda cmd, timeout=5.0: (0, "Server Name: pulseaudio")), \
             mock.patch.object(repair, "_unit_active", lambda unit: True):
            actions = {a.id: a for a in repair.detect()}
        self.assertTrue(actions["pulseaudio"].manual)
        self.assertFalse(actions["pulseaudio"].to_dict(repair.root_mode())["runnable"])


class RunnerTests(unittest.TestCase):
    def test_a_repair_that_cannot_start_does_not_hang_the_runner(self):
        runner = repair.Runner()
        action = repair.Action(id="x", title="x", why="", effect="", command=("/does/not/exist",))
        runner.start(action, {"available": False, "how": ""})
        for _ in range(200):
            if not runner.state()["running"]:
                break
            import time

            time.sleep(0.02)
        state = runner.state()
        self.assertFalse(state["running"])
        self.assertNotEqual(state["rc"], 0)
        self.assertIn("fertig", state["output"])

    def test_only_one_repair_runs_at_a_time(self):
        runner = repair.Runner()
        runner.running = True
        result = runner.start(repair.Action(id="x", title="x", why="", effect=""), {"available": True, "how": ""})
        self.assertFalse(result["started"])
        self.assertIn("läuft bereits", result["problem"])

    def test_the_rate_drop_in_is_written_where_pipewire_reads_it(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
                runner = repair.Runner()
                rc = runner._python_fix(repair.Action(id="rate-dropin", title="", why="", effect="", python="rate_dropin"))
                path = repair.rate_drop_in()
            self.assertEqual(rc, 0)
            self.assertTrue(path.is_file())
            self.assertIn("48000", path.read_text())
            self.assertIn("pipewire.conf.d", str(path))


class HdmiRuleTests(unittest.TestCase):
    """The rule that keeps an HDMI capture input from driving the graph can go
    missing -- deleted, or left in the wrong format when WirePlumber is upgraded
    from 0.4 to 0.5. Nothing shows it until the HDMI source is switched off and
    everything stalls at once, so the daemon carries the text and can put it
    back."""

    def test_the_embedded_rule_is_the_one_the_installer_writes(self):
        from pathlib import Path

        self.assertEqual(repair.HDMI_RULE_CONF, Path("wireplumber/52-tfcz-hdmi-priority.conf").read_text())
        self.assertEqual(repair.HDMI_RULE_LUA, Path("wireplumber/52-tfcz-hdmi-priority.lua").read_text())

    def test_which_format_this_wireplumber_reads(self):
        for version, wanted in (((0, 4), ["lua"]), ((0, 5), ["conf"]), ((1, 0), ["conf"]), (None, ["conf", "lua"])):
            with mock.patch.object(repair, "wireplumber_major", lambda v=version: v):
                self.assertEqual(repair.hdmi_rule_state()["wanted"], wanted, version)

    def test_it_is_only_offered_on_a_machine_with_such_a_card(self):
        with mock.patch.object(repair, "has_capture_card", lambda: False), \
             mock.patch.object(repair.shutil, "which", lambda name: f"/usr/bin/{name}"), \
             mock.patch.object(repair, "_run", lambda cmd, timeout=5.0: (0, "active")), \
             mock.patch.object(repair, "_unit_active", lambda unit: True):
            self.assertNotIn("hdmi-rule", [a.id for a in repair.detect()])

    def test_a_missing_rule_is_offered_as_a_repair(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}), \
             mock.patch.object(repair, "has_capture_card", lambda: True), \
             mock.patch.object(repair, "wireplumber_major", lambda: (0, 5)), \
             mock.patch.object(repair.shutil, "which", lambda name: f"/usr/bin/{name}"), \
             mock.patch.object(repair, "_run", lambda cmd, timeout=5.0: (0, "active")), \
             mock.patch.object(repair, "_unit_active", lambda unit: True):
            action = next(a for a in repair.detect() if a.id == "hdmi-rule")
            self.assertIn("Taktgeber", action.why)
            self.assertFalse(action.needs_root, "it writes into the user's own config")

    def test_writing_it_puts_the_file_where_this_version_reads_it(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}), \
             mock.patch.object(repair, "wireplumber_major", lambda: (0, 4)), \
             mock.patch.object(repair, "_run", lambda cmd, timeout=5.0: (0, "restarted")):
            runner = repair.Runner()
            rc = runner._python_fix(repair.Action(id="hdmi-rule", title="", why="", effect="", python="hdmi_rule"))
            written = Path(tmp, "wireplumber", "main.lua.d", "52-tfcz-hdmi-priority.lua")
            self.assertEqual(rc, 0)
            self.assertTrue(written.is_file())
            self.assertEqual(written.read_text(), repair.HDMI_RULE_LUA)
            self.assertFalse(Path(tmp, "wireplumber", "wireplumber.conf.d").exists(),
                             "0.4 does not read the conf format")

    def test_an_existing_rule_is_not_reported_as_missing(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}), \
             mock.patch.object(repair, "wireplumber_major", lambda: (0, 5)):
            path = Path(tmp, "wireplumber", "wireplumber.conf.d", "52-tfcz-hdmi-priority.conf")
            path.parent.mkdir(parents=True)
            path.write_text(repair.HDMI_RULE_CONF)
            self.assertEqual(repair.hdmi_rule_state()["missing"], [])
