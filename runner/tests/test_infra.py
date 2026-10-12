"""基盤の故障と人による計画の書き換えで、エスカレーションの上限を使い切らない。

柵の下のディレクトリが 2755 で作られ、ソルバーの Write がすべて EACCES で落ちた。
ソルバーにもプランナーにも直せない停止が試行を使い切り、エスカレーションになり、
上限まで数えられた。

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

    def test_it_is_recorded_and_stops_without_escalating(self):
        events = []
        with mock.patch.object(loop, "ledger", lambda e, **f: events.append((e, f))):
            with self.assertRaises(loop.InfraBroken):
                loop.check_infra("TEST_WRITE",
                                 f"EACCES: permission denied, open '{loop.PROJECT}/src/a.py'")
        self.assertEqual(events[0][0], "INFRA_FAILURE")
        self.assertFalse(issubclass(loop.InfraBroken, loop.Halt))


class TheRunStopsWithoutAskingThePlanner(unittest.TestCase):
    def test_run_all_records_the_stop_and_does_not_propose(self):
        events = []
        with tempfile.TemporaryDirectory() as temp:
            plan = Path(temp)
            (plan / "tasks.json").write_text(json.dumps({"steps": [{"id": "S1"}]}),
                                             encoding="utf-8")
            with mock.patch.object(loop, "PLAN", plan), \
                 mock.patch.object(loop, "ESCALATION", plan / "ESCALATION.md"), \
                 mock.patch.object(loop, "load_settings"), \
                 mock.patch.object(loop, "validate_plan", return_value=[]), \
                 mock.patch.object(loop, "green_steps", return_value=set()), \
                 mock.patch.object(loop, "ledger", lambda e, **f: events.append((e, f))), \
                 mock.patch.object(loop, "cmd_plan_propose") as propose, \
                 mock.patch.object(loop, "run_step",
                                   side_effect=loop.InfraBroken("IMPL", "EACCES ...")):
                self.assertEqual(loop.cmd_run_all(), 6)
        propose.assert_not_called()
        self.assertEqual(events[-1][0], "RUN_ALL_STOP")
        self.assertIn(loop.INFRA_REFUSED, events[-1][1]["reason"])


class AnAmendedStepCountsAgain(unittest.TestCase):
    def count(self, *records) -> int:
        with tempfile.TemporaryDirectory() as temp:
            ledger = Path(temp) / "ledger.jsonl"
            ledger.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
            with mock.patch.object(loop, "LEDGER", ledger):
                return loop.escalation_count("S2")

    def test_escalations_before_the_amendment_are_not_counted(self):
        self.assertEqual(self.count({"event": "ESCALATED", "step": "S2"},
                                    {"event": "ESCALATED", "step": "S2"},
                                    {"event": "PLAN_AMEND", "step": "S2"},
                                    {"event": "ESCALATED", "step": "S2"}), 1)

    def test_an_amendment_of_another_step_changes_nothing(self):
        self.assertEqual(self.count({"event": "ESCALATED", "step": "S2"},
                                    {"event": "PLAN_AMEND", "step": "S3"},
                                    {"event": "INFRA_FAILURE", "step": "S2"}), 1)


if __name__ == "__main__":
    unittest.main()
