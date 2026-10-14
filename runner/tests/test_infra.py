"""基盤の故障では、設計を中段に差し戻さずに止まる。

柵の下のディレクトリが 2755 で作られ、ソルバーの Write がすべて EACCES で落ちた。
ソルバーにもプランナーにも直せない停止が試行を使い切り、設計の誤りとして
扱われた。

    python3 -m unittest discover -s runner/tests
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop  # noqa: E402


@unittest.skipUnless(os.sep == "/", "the sandbox's paths are POSIX")
class WhatCountsAsTheSandboxRefusing(unittest.TestCase):
    def setUp(self) -> None:
        for name, value in {"SRC": loop.PROJECT / "src", "TESTS": loop.PROJECT / "tests"}.items():
            p = mock.patch.object(loop, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_a_refused_write_under_the_code_fence_is_the_sandbox(self):
        for line in (f"EACCES: permission denied, open '{loop.PROJECT}/src/Managers/Game.cs'",
                     f"PermissionError: [Errno 13] Permission denied: '{loop.PROJECT}/src/a.py'",
                     f"error MSB3021: EROFS {loop.DOTNET_BUILD}/code/obj/x.dll"):
            with self.subTest(line=line):
                self.assertEqual(loop.infra_evidence("IMPL", "noise\n" + line + "\n"), line)

    def test_a_refusal_the_fence_means_is_the_solver_s_own(self):
        for line in (f"EACCES: permission denied, open '{loop.PROJECT}/plan/tasks.json'",
                     f"EACCES: permission denied, open '{loop.PROJECT}/.runner/freeze/S1.json'",
                     f"EACCES: permission denied, open '{loop.PROJECT}/tests/test_a.py'"):
            with self.subTest(line=line):
                self.assertIsNone(loop.infra_evidence("IMPL", line))

    def test_the_test_fence_counts_only_while_tests_are_written(self):
        line = f"EACCES: permission denied, open '{loop.PROJECT}/tests/test_a.py'"
        self.assertEqual(loop.infra_evidence("TEST_WRITE", line), line)

    def test_the_words_alone_are_not_enough(self):
        self.assertIsNone(loop.infra_evidence(
            "IMPL", "raise PermissionError('EACCES') when the save file is read-only"))

    def test_it_is_recorded_and_is_not_a_halt_of_the_module(self):
        events = []
        with mock.patch.object(loop, "ledger", lambda e, **f: events.append((e, f))):
            with self.assertRaises(loop.InfraBroken):
                loop.check_infra("TEST_WRITE",
                                 f"EACCES: permission denied, open '{loop.PROJECT}/src/a.py'")
        self.assertEqual(events[0][0], "INFRA_FAILURE")
        self.assertFalse(issubclass(loop.InfraBroken, loop.Halt))


class TheRunStopsWithoutSendingTheDesignBack(unittest.TestCase):
    def test_run_all_records_the_stop_and_stays_downstream(self):
        events = []
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "stage.json").write_text(json.dumps({"stage": "downstream"}),
                                             encoding="utf-8")
            (root / "tasks.json").write_text(json.dumps({"steps": [{"id": "S1"}]}),
                                             encoding="utf-8")
            with mock.patch.object(loop, "STAGE_FILE", root / "stage.json"), \
                 mock.patch.object(loop, "TASKS", root / "tasks.json"), \
                 mock.patch.object(loop, "load_settings"), \
                 mock.patch.object(loop, "green_steps", return_value=set()), \
                 mock.patch.object(loop, "ledger", lambda e, **f: events.append((e, f))), \
                 mock.patch.object(loop, "fail_module") as fail, \
                 mock.patch.object(loop, "run_step",
                                   side_effect=loop.InfraBroken("IMPL", "EACCES ...")):
                self.assertEqual(loop.cmd_run_all(), 6)
                self.assertEqual(loop.read_stage()["stage"], "downstream")
        fail.assert_not_called()
        self.assertEqual(events[-1][0], "RUN_ALL_STOP")
        self.assertIn(loop.INFRA_REFUSED, events[-1][1]["reason"])


if __name__ == "__main__":
    unittest.main()
