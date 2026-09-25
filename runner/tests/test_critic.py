"""The fourth role: what it is told, and what the runner does with its answer.

The critic is the only part of this machine that has an opinion. Everything
else counts, hashes or compares. So the things worth pinning down are the ones
that would quietly turn an opinion into a rubber stamp: what reaches the brief,
and what happens to an answer that cannot be read.

    python3 -m unittest discover -s runner/tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop  # noqa: E402
from loop import (  # noqa: E402
    CRITIQUE_MODES, FINDINGS_NAME, brief_critique_coverage, brief_critique_trace,
    read_findings, render_findings,
)

REQUIREMENTS = "- 開始直後から遊べること\n- セーブを消去できること\n"
TASKS = json.dumps({"language": "python", "steps": [{"id": "S1"}]}, ensure_ascii=False)


class WhatTheCriticIsTold(unittest.TestCase):
    def test_both_modes_exist_and_are_the_whole_list(self):
        # Measured on run 7's plan: coverage found an unreachable feature, an
        # unverified launch path and a criterion asserting against its own
        # requirement; trace found that the initial state was a fixed point.
        # The findings were disjoint, so neither mode is redundant.
        self.assertEqual(sorted(CRITIQUE_MODES), ["coverage", "trace"])

    def test_coverage_is_given_the_requirements_and_the_plan(self):
        brief = brief_critique_coverage(REQUIREMENTS, TASKS)
        self.assertIn("開始直後から遊べること", brief)
        self.assertIn('"id": "S1"', brief)

    def test_trace_is_not_given_the_requirements(self):
        # This is the property the mode is FOR. On run 7 it derived the deadlock
        # from the criteria alone, which is what lets it catch a product that
        # cannot work even when the requirements never mentioned the missing
        # piece -- run 7's requirements never mentioned a starting state. A
        # tracer handed the requirements would grade against them instead, and
        # this mode would collapse into the other one.
        brief = brief_critique_trace(TASKS)
        self.assertNotIn("開始直後から遊べること", brief)
        self.assertNotIn("セーブを消去", brief)
        self.assertIn('"id": "S1"', brief)

    def test_every_brief_names_the_one_file_and_forbids_approval(self):
        for brief in (brief_critique_coverage(REQUIREMENTS, TASKS),
                      brief_critique_trace(TASKS)):
            self.assertIn(FINDINGS_NAME, brief)
            self.assertIn("empty `findings` list", brief)
            self.assertIn("cannot approve", brief)

    def test_the_tracer_is_told_a_defence_is_not_dead_code(self):
        # run 8 の計画では、壊れたセーブや知らない ID への守りのテストを、
        # 3回の批評すべてで「遊んでいても届かない状態」と指摘した。訊いた問いが
        # そう答えるよう誘っていた。外から来る入力は対象外だと伝える。
        brief = brief_critique_trace(TASKS)
        self.assertIn("the artifact's OWN logic", brief)
        self.assertIn("checking a defence, not exercising dead code", brief)


class ReadingTheAnswer(unittest.TestCase):
    """An unreadable critique has said nothing, and must never read as clean."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name)
        self.saved_out = loop.CRITIC_OUT
        self.saved_ledger = loop.ledger
        loop.CRITIC_OUT = self.out
        self.events: list[tuple[str, dict]] = []
        loop.ledger = lambda event, **fields: self.events.append((event, fields))

    def tearDown(self) -> None:
        loop.CRITIC_OUT = self.saved_out
        loop.ledger = self.saved_ledger
        self.temp.cleanup()

    def write(self, body: str) -> None:
        (self.out / FINDINGS_NAME).write_text(body, encoding="utf-8")

    def test_findings_come_back_as_a_list(self):
        self.write(json.dumps({"findings": [
            {"title": "the initial state is a fixed point",
             "evidence": "S1 pins new_game() to resource=0.0",
             "machine_would_notice": False}]}))
        findings = read_findings()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["title"], "the initial state is a fixed point")

    def test_an_empty_list_is_an_answer(self):
        self.write(json.dumps({"findings": []}))
        self.assertEqual(read_findings(), [])

    def test_no_file_at_all_halts_rather_than_reading_as_clean(self):
        # "the critic found nothing" and "the critic never ran" must not look
        # alike. Same reason parse_junit refuses to read a broken report as
        # zero failures.
        with self.assertRaises(loop.Halt):
            read_findings()

    def test_unreadable_json_halts(self):
        self.write("{ not json")
        with self.assertRaises(loop.Halt):
            read_findings()

    def test_a_file_without_a_findings_list_halts(self):
        self.write(json.dumps({"verdict": "looks good to me"}))
        with self.assertRaises(loop.Halt):
            read_findings()

    def test_a_stray_file_is_deleted_and_recorded(self):
        # Same shape as the planner's B3: the runner owns the directory, so it
        # does the deleting. Unlike B3 this cannot cost an attempt -- there is
        # no retry loop here to spend.
        self.write(json.dumps({"findings": []}))
        (self.out / "notes.md").write_text("scratch", encoding="utf-8")
        read_findings()
        self.assertFalse((self.out / "notes.md").exists())
        self.assertEqual([e for e, _ in self.events], ["CRITIQUE_PRUNED"])


class Reporting(unittest.TestCase):
    def test_a_finding_no_gate_would_catch_is_marked_as_such(self):
        text = render_findings({"trace": [
            {"title": "nothing is reachable", "evidence": "0.0 < 15.0",
             "machine_would_notice": False}]})
        self.assertIn("NO GATE WOULD CATCH THIS", text)

    def test_a_mode_that_found_nothing_says_so(self):
        self.assertIn("(nothing found)", render_findings({"coverage": []}))


class TheRefineLoop(unittest.TestCase):
    """Findings go back to the planner without a person in between.

    A critique nobody routes is a report, and a report leaves the human inside
    the cycle at exactly the point the machine was built to handle. The
    findings are addressed to the planner anyway.
    """

    def test_the_brief_says_the_criteria_are_still_open(self):
        # This is the difference from `plan propose`. Nothing is built, nothing
        # is green, so P5 does not bite and the acceptance criteria can still
        # change -- which is why the critique happens before `plan apply` and
        # not after.
        brief = loop.brief_plan_refine("要件", "{}", "1. something is wrong")
        self.assertIn("NOTHING HAS BEEN BUILT YET", brief)
        self.assertIn("still yours to change", brief)
        self.assertIn("something is wrong", brief)

    def test_the_brief_says_the_critic_may_be_wrong(self):
        # The first production critique spent two of its five findings on a
        # file the environment provides. A planner told to satisfy every
        # finding would have contorted the plan to build something that was
        # already there.
        brief = loop.brief_plan_refine("要件", "{}", "1. x")
        self.assertIn("could be wrong", brief)
        self.assertIn("already provides", brief)

    def test_deleting_the_criterion_is_named_as_the_move_not_to_make(self):
        brief = loop.brief_plan_refine("要件", "{}", "1. x")
        self.assertIn("deleting the criterion", brief)
        self.assertIn("expected_tests", brief)


if __name__ == "__main__":
    unittest.main()
