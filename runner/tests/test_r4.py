"""R4 で空と分かった条件は、プランナーが消してよい。消すことだけを、数で縛って。

スタブに対して通るテストは、落ちうることを一度も示していない。run 8 の S10 は、
環境が置いた index.html の中身を確かめる条件で R4 に止まり、プランナーは (b) と
して人間に差し戻し、人間が計画を手で直した。

    python3 -m unittest discover -s runner/tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop  # noqa: E402

A = {"case": "normal", "given": "start(root)", "then": "root has 1 button"}
B = {"case": "normal", "given": "index.html", "then": "contains '/src/main.ts'"}
C = {"case": "boundary", "given": "no save", "then": "resource == 0"}
D = {"case": "error", "given": "bad save", "then": "raises SyntaxError"}


def plan(acceptance, expected, sid="S10"):
    return {"steps": [{"id": sid, "acceptance": acceptance, "expected_tests": expected}]}


class OnlyDeletionCounts(unittest.TestCase):
    def test_deleting_entries_is_measured(self):
        self.assertEqual(loop.removed_entries([A, B, C], [A, C]), 1)
        self.assertEqual(loop.removed_entries([A, B, C], [C]), 2)

    def test_rewording_is_not_deletion(self):
        self.assertIsNone(loop.removed_entries([A, B], [A, {**B, "then": "anything"}]))

    def test_adding_or_reordering_is_not_deletion(self):
        self.assertIsNone(loop.removed_entries([A, B], [A, B, C]))
        self.assertIsNone(loop.removed_entries([A, B, C], [C, A]))


class TheGuardAfterR4(unittest.TestCase):
    """check_proposal に、R4 で止まったステップと通ったテストの数を渡した場合。"""

    def check(self, old, new, allowance=("S10", 1)):
        with mock.patch.object(loop, "green_steps", return_value=set()), \
             mock.patch.object(loop, "r4_allowance", return_value=allowance):
            return loop.check_proposal(old, new)

    def test_deleting_the_passing_criterion_is_accepted(self):
        self.assertEqual(self.check(plan([A, B, C, D], 4), plan([A, C, D], 3)), [])

    def test_keeping_expected_tests_is_also_accepted(self):
        # 下げるのは「まで」で、下げなくてもよい。
        self.assertEqual(self.check(plan([A, B, C, D], 4), plan([A, C, D], 4)), [])

    def test_deleting_more_than_passed_is_refused(self):
        problems = self.check(plan([A, B, C, D], 4), plan([C, D], 2))
        self.assertTrue(any("R4 found only 1" in p for p in problems), problems)

    def test_rewording_is_still_refused(self):
        problems = self.check(plan([A, B], 2), plan([A, {**B, "then": "x"}], 2))
        self.assertTrue(any("word for word" in p for p in problems), problems)

    def test_lowering_expected_tests_beyond_the_deletion_is_refused(self):
        problems = self.check(plan([A, B, C, D], 5), plan([A, C, D], 3))
        self.assertTrue(any(p.startswith("P3") for p in problems), problems)

    def test_another_step_gets_no_exception(self):
        problems = self.check(plan([A, B], 2, sid="S9"), plan([A], 1, sid="S9"))
        self.assertTrue(any("case (b)" in p for p in problems), problems)

    def test_without_an_r4_escalation_nothing_changes(self):
        problems = self.check(plan([A, B], 2), plan([A], 1), allowance=None)
        self.assertTrue(any("case (b)" in p for p in problems), problems)
        self.assertTrue(any(p.startswith("P3") for p in problems), problems)


class TheAllowanceComesFromTheLedger(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.ledger = root / "ledger.jsonl"
        self.escalation = root / "ESCALATION.md"
        for p in (mock.patch.object(loop, "LEDGER", self.ledger),
                  mock.patch.object(loop, "ESCALATION", self.escalation)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.temp.cleanup)

    def write(self, *records, open_escalation=True):
        self.ledger.write_text(
            "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        if open_escalation:
            self.escalation.write_text("# ESCALATION", encoding="utf-8")

    def test_an_open_r4_escalation_gives_the_step_and_the_count(self):
        self.write({"event": "ESCALATED", "step": "S10", "phase": "RED_GATE",
                    "reason": "R4: some tests already pass", "r4_passing": 1})
        self.assertEqual(loop.r4_allowance(), ("S10", 1))

    def test_only_the_latest_escalation_counts(self):
        self.write({"event": "ESCALATED", "step": "S10", "phase": "RED_GATE",
                    "reason": "R4: ...", "r4_passing": 1},
                   {"event": "ESCALATED", "step": "S10", "phase": "IMPL",
                    "reason": "still failing after 3 attempts"})
        self.assertIsNone(loop.r4_allowance())

    def test_an_answered_escalation_gives_nothing(self):
        # plan apply が答えると ESCALATION.md は消える。
        self.write({"event": "ESCALATED", "step": "S10", "phase": "RED_GATE",
                    "reason": "R4: ...", "r4_passing": 1}, open_escalation=False)
        self.assertIsNone(loop.r4_allowance())

    def test_other_red_gate_rules_give_nothing(self):
        self.write({"event": "ESCALATED", "step": "S10", "phase": "RED_GATE",
                    "reason": "R5: failures are not assertions"})
        self.assertIsNone(loop.r4_allowance())


class TheEscalationRecordsTheCount(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.escalation = Path(self.temp.name) / "ESCALATION.md"
        self.events: list[tuple[str, dict]] = []
        for p in (mock.patch.object(loop, "ESCALATION", self.escalation),
                  mock.patch.object(loop, "escalation_count", return_value=0),
                  mock.patch.object(loop, "ledger",
                                    lambda event, **f: self.events.append((event, f)))):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.temp.cleanup)
        self.step = {"id": "S10", "max_attempts": 3}

    def test_r4_writes_the_passing_count_and_the_exception(self):
        red = loop.TestRun(9, 8, 0, 0, ["AssertionError"] * 8, ["has index.html"], "")
        halt = loop.Halt("RED_GATE", "R4: some tests already pass against the stub")
        loop.escalate(self.step, halt, 0, red)
        self.assertEqual(self.events[0][1]["r4_passing"], 1)
        self.assertIn("you may DELETE up to 1", self.escalation.read_text(encoding="utf-8"))

    def test_other_halts_record_nothing_extra(self):
        halt = loop.Halt("IMPL", "still failing after 3 attempts")
        loop.escalate(self.step, halt, 3, None)
        self.assertNotIn("r4_passing", self.events[0][1])
        self.assertNotIn("DELETE", self.escalation.read_text(encoding="utf-8"))


class TheRevisionBriefSaysSo(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        plan_dir = Path(self.temp.name)
        (plan_dir / "CONTEXT.md").write_text("context", encoding="utf-8")
        (plan_dir / "tasks.json").write_text("{}", encoding="utf-8")
        for p in (mock.patch.object(loop, "PLAN", plan_dir),
                  mock.patch.object(loop, "SYSTEM_SPEC", plan_dir / "SYSTEM_SPEC.md"),
                  mock.patch.object(loop, "green_steps", return_value=set())):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.temp.cleanup)

    def test_after_r4_the_brief_offers_deletion(self):
        with mock.patch.object(loop, "r4_allowance", return_value=("S10", 1)):
            brief = loop.brief_plan_revise(None, "R4")
        self.assertIn("you may DELETE such acceptance entries", brief)
        self.assertIn("at most 1 of them", brief)

    def test_otherwise_it_does_not(self):
        with mock.patch.object(loop, "r4_allowance", return_value=None):
            brief = loop.brief_plan_revise(None, "IMPL")
        self.assertNotIn("DELETE", brief)


if __name__ == "__main__":
    unittest.main()
