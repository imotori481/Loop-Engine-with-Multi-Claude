"""写しの更新は、固定の loop-pull.cmd を裏で1つだけ流し、出力の末尾を返す。"""

import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from host.dashboard.mirror import MirrorSync, TAIL_LINES


def process(lines, returncode=0, gate=None):
    fake = MagicMock()

    def output():
        if gate is not None:
            gate.wait(5)
        yield from (line + "\n" for line in lines)

    fake.stdout = output()
    fake.wait.return_value = returncode
    return fake


class Pull(unittest.TestCase):
    def setUp(self):
        self.sync = MirrorSync(Path("C:/loop/host/loop-pull.cmd"))

    def wait(self):
        self.sync.thread.join(5)

    @patch("host.dashboard.mirror.subprocess.Popen")
    def test_it_runs_the_fixed_script_without_a_shell(self, popen):
        popen.return_value = process(["done."])
        self.sync.start()
        self.wait()
        argv = popen.call_args.args[0]
        self.assertEqual(argv[0], "cmd.exe")
        self.assertEqual(argv[-1], str(Path("C:/loop/host/loop-pull.cmd")))
        self.assertIs(popen.call_args.kwargs["shell"], False)
        status = self.sync.status()
        self.assertFalse(status["running"])
        self.assertEqual(status["returncode"], 0)
        self.assertEqual(status["output"], ["done."])

    @patch("host.dashboard.mirror.subprocess.Popen")
    def test_only_one_runs_at_a_time(self, popen):
        gate = threading.Event()
        popen.return_value = process(["x"], gate=gate)
        self.sync.start()
        with self.assertRaisesRegex(ValueError, "already running"):
            self.sync.start()
        gate.set()
        self.wait()
        self.assertEqual(popen.call_count, 1)

    @patch("host.dashboard.mirror.subprocess.Popen")
    def test_only_the_tail_of_the_output_is_kept(self, popen):
        popen.return_value = process([f"line {n}" for n in range(100)], returncode=1)
        self.sync.start()
        self.wait()
        status = self.sync.status()
        self.assertEqual(len(status["output"]), TAIL_LINES)
        self.assertEqual(status["output"][-1], "line 99")
        self.assertEqual(status["returncode"], 1)

    @patch("host.dashboard.mirror.subprocess.Popen", side_effect=OSError("no cmd.exe"))
    def test_a_script_that_cannot_start_is_reported(self, popen):
        self.sync.start()
        self.wait()
        status = self.sync.status()
        self.assertFalse(status["running"])
        self.assertIn("no cmd.exe", status["output"][-1])


if __name__ == "__main__":
    unittest.main()
