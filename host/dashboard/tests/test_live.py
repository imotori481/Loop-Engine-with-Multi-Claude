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


if __name__ == "__main__":
    unittest.main()
