import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from host.dashboard.live import Live


def answered(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class LiveView(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Path(self.temp.name) / "config.json"
        self.live = Live(self.config)

    def tearDown(self):
        self.temp.cleanup()

    def configure(self, live):
        self.config.write_text(json.dumps({"live": live}), encoding="utf-8")

    @patch("host.dashboard.live.subprocess.run")
    def test_it_asks_the_sandbox_with_a_fixed_command_and_no_shell(self, run):
        run.return_value = answered('{"running": true}')
        self.assertEqual(self.live.fetch(), {"running": True})
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "ssh")
        self.assertEqual(argv[-3:], ["loop-dev", "loop", "now"])
        self.assertIs(run.call_args.kwargs["shell"], False)

    @patch("host.dashboard.live.subprocess.run")
    def test_the_host_comes_from_the_config(self, run):
        self.configure({"ssh_host": "other-box"})
        run.return_value = answered("{}")
        self.live.fetch()
        self.assertIn("other-box", run.call_args.args[0])

    @patch("host.dashboard.live.subprocess.run")
    def test_a_host_that_would_be_read_as_an_option_is_refused(self, run):
        self.configure({"ssh_host": "-oProxyCommand=calc.exe"})
        self.assertIn("error", self.live.fetch())
        run.assert_not_called()

    @patch("host.dashboard.live.subprocess.run")
    def test_it_can_be_turned_off(self, run):
        self.configure({"enabled": False})
        self.assertIn("disabled", self.live.fetch()["error"])
        run.assert_not_called()

    @patch("host.dashboard.live.subprocess.run")
    def test_an_unreachable_sandbox_is_an_error_not_a_crash(self, run):
        run.return_value = answered(returncode=255, stderr="ssh: connect to host refused")
        self.assertIn("refused", self.live.fetch()["error"])

    @patch("host.dashboard.live.subprocess.run")
    def test_a_hung_ssh_is_an_error_not_a_crash(self, run):
        run.side_effect = subprocess.TimeoutExpired("ssh", 15)
        self.assertIn("could not reach", self.live.fetch()["error"])

    @patch("host.dashboard.live.subprocess.run")
    def test_an_answer_that_is_not_json_is_an_error(self, run):
        run.return_value = answered("bash: loop: command not found")
        self.assertIn("not JSON", self.live.fetch()["error"])

    @patch("host.dashboard.live.subprocess.run")
    def test_many_viewers_share_one_ssh_call(self, run):
        run.return_value = answered("{}")
        for _ in range(5):
            self.live.fetch()
        self.assertEqual(run.call_count, 1)


class ActOnTheSandbox(LiveView):
    @patch("host.dashboard.live.subprocess.run")
    def test_a_rewrite_goes_through_stdin_and_never_into_the_command(self, run):
        run.return_value = answered("直した")
        title = "; rm -rf / #"
        self.assertEqual(self.live.rewrite_finding("trace", 1, title, "S3"), "直した")
        argv = run.call_args.args[0]
        self.assertEqual(argv[-4:], ["loop-dev", "loop", "findings", "set"])
        self.assertNotIn(title, " ".join(argv))
        self.assertEqual(json.loads(run.call_args.kwargs["input"]),
                         {"mode": "trace", "index": 1, "title": title, "evidence": "S3"})
        self.assertIs(run.call_args.kwargs["shell"], False)

    @patch("host.dashboard.live.subprocess.run")
    def test_a_refusal_carries_the_sandbox_reason(self, run):
        run.return_value = answered(returncode=1, stderr="loop findings: title が空")
        with self.assertRaisesRegex(ValueError, "title が空"):
            self.live.rewrite_finding("trace", 0, "", "")

    def dash_request(self, run):
        """`loop dash` に渡った要求。コマンドは sudoers が許すものから動かない。"""
        self.assertEqual(run.call_args.args[0][-4:], ["sudo", "-n", "/usr/local/bin/loop", "dash"])
        self.assertIs(run.call_args.kwargs["shell"], False)
        return json.loads(run.call_args.kwargs["input"])

    @patch("host.dashboard.live.subprocess.run")
    def test_continue_and_stop_go_through_the_one_command_sudoers_allows(self, run):
        run.return_value = answered("裏で走らせた")
        self.live.continue_loop()
        self.assertEqual(self.dash_request(run), {"action": "continue"})
        self.live.stop_loop()
        self.assertEqual(self.dash_request(run), {"action": "stop"})
        self.live.reset_step("S12")
        self.assertEqual(self.dash_request(run), {"action": "reset", "step": "S12"})

    @patch("host.dashboard.live.subprocess.run")
    def test_the_requirements_travel_on_stdin_and_never_on_the_command_line(self, run):
        run.return_value = answered("裏で走らせた")
        text = "# 要件\n$(rm -rf /)\n"
        self.live.start_loop(text, "csharp", "unity")
        self.assertEqual(self.dash_request(run),
                         {"action": "go", "requirements": text, "language": "csharp",
                          "framework": "unity"})
        self.assertNotIn("rm -rf", " ".join(run.call_args.args[0]))

    @patch("host.dashboard.live.subprocess.run")
    def test_switching_and_model_changes_are_requests_too(self, run):
        run.return_value = answered("ok")
        self.live.use_project("game-2")
        self.assertEqual(self.dash_request(run), {"action": "use", "project": "game-2"})
        self.live.set_model("solver", "claude-opus-5-5", "high")
        self.assertEqual(self.dash_request(run), {"action": "model", "role": "solver",
                                                  "model": "claude-opus-5-5", "effort": "high"})

    @patch("host.dashboard.live.subprocess.run")
    def test_settings_are_read_as_json(self, run):
        run.return_value = answered('{"roles": {"solver": {"model": "", "effort": ""}}}')
        self.assertEqual(self.live.settings()["roles"]["solver"], {"model": "", "effort": ""})
        self.assertEqual(self.dash_request(run), {"action": "settings"})
        run.return_value = answered("sudo: a password is required", returncode=1)
        with self.assertRaisesRegex(ValueError, "password"):
            self.live.settings()

    @patch("host.dashboard.live.subprocess.run")
    def test_an_action_drops_the_cached_view(self, run):
        run.return_value = answered("{}")
        self.live.fetch()
        self.live.continue_loop()
        self.live.fetch()
        self.assertEqual([call.args[0][-1] for call in run.call_args_list],
                         ["now", "dash", "now"])


if __name__ == "__main__":
    unittest.main()
