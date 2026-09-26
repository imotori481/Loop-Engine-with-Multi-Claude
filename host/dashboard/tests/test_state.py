import json
import tempfile
import unittest
from pathlib import Path

from host.dashboard.state import DashboardState, read_jsonl


class DashboardFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.project = root / "project"
        (self.project / "plan").mkdir(parents=True)
        (self.project / "plan" / "tasks.json").write_text(
            json.dumps({"steps": [{"id": "S1"}, {"id": "S2"}]}), encoding="utf-8")
        self.state = DashboardState(self.project, root / "state")

    def tearDown(self):
        self.temp.cleanup()

    def ledger(self, *records):
        (self.project / "plan" / "ledger.jsonl").write_text(
            "".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")


class ReadingState(DashboardFixture):
    def test_green_progress_is_derived_from_the_ledger(self):
        self.ledger({"event": "GREEN", "step": "S1"})
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["steps"], {"total": 2, "green": 1, "ids": ["S1", "S2"]})

    def test_all_green_creates_a_planned_review_not_an_escalation(self):
        self.ledger({"event": "GREEN", "step": "S1"}, {"event": "GREEN", "step": "S2"},
                    {"event": "ALL_GREEN", "steps": ["S1", "S2"]})
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["phase"], "review_required")
        self.assertEqual(snapshot["pending"][0]["kind"], "review")

    def test_an_escalation_remains_distinct_from_a_review(self):
        self.ledger({"event": "RUN_ALL_STOP", "reason": "cap reached"})
        (self.project / "plan" / "ESCALATION.md").write_text("# ESCALATION\nreason", encoding="utf-8")
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["phase"], "escalated")
        self.assertEqual(snapshot["pending"][0]["kind"], "escalation")

    def test_unreadable_lines_are_reported_instead_of_silently_dropped(self):
        path = self.project / "plan" / "ledger.jsonl"
        path.write_text('{"event":"GREEN"}\nnot json\n', encoding="utf-8")
        self.assertEqual(read_jsonl(path)[1], {"event": "UNREADABLE_LEDGER_RECORD", "line": 2})


class Decisions(DashboardFixture):
    def test_a_decision_answers_only_the_exact_pending_review(self):
        self.ledger({"event": "ALL_GREEN", "steps": ["S1", "S2"]})
        request = self.state.snapshot()["pending"][0]
        record = self.state.decide("review", request["id"], "approve", "played it")
        self.assertEqual(record["decision"], "approve")
        self.assertEqual(self.state.snapshot()["pending"], [])

    def test_a_stale_or_invented_request_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no longer pending"):
            self.state.decide("review", "invented", "approve", "")

    def test_an_answered_escalation_is_not_presented_again(self):
        self.ledger({"event": "RUN_ALL_STOP", "reason": "cap reached"})
        (self.project / "plan" / "ESCALATION.md").write_text("reason", encoding="utf-8")
        request = self.state.snapshot()["pending"][0]
        self.state.decide("escalation", request["id"], "respond", "change the boundary")
        self.assertEqual(self.state.snapshot()["pending"], [])

    def test_recording_an_answer_never_rewrites_the_earlier_ones(self):
        self.state.data_dir.mkdir(parents=True, exist_ok=True)
        self.state.decisions_file.write_text('{"event":"HUMAN_DECISION","kind":"old"}\n',
                                             encoding="utf-8")
        self.ledger({"event": "ALL_GREEN", "steps": ["S1", "S2"]})
        request = self.state.snapshot()["pending"][0]
        self.state.decide("review", request["id"], "approve", "played it")
        lines = self.state.decisions_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["kind"], "old")

    def test_a_phone_may_refuse_the_work_but_may_not_certify_it(self):
        # レビューがあるのは、どの機械も画面を見られないからだ。窓を開けない
        # 端末に、窓が大丈夫だと言わせてはならない。ただし「まだ足りない」と
        # 言わせても、失うものは無い。
        self.ledger({"event": "ALL_GREEN", "steps": ["S1", "S2"]})
        request = self.state.snapshot()["pending"][0]
        with self.assertRaisesRegex(ValueError, "machine that can run the result"):
            self.state.decide("review", request["id"], "approve", "",
                              scope="remote", user="someone@example.com")
        record = self.state.decide("review", request["id"], "revise", "実行できない",
                                   scope="remote", user="someone@example.com")
        self.assertEqual((record["scope"], record["user"]),
                         ("remote", "someone@example.com"))

    def test_an_escalation_can_be_answered_or_stopped_from_anywhere(self):
        self.ledger({"event": "RUN_ALL_STOP", "reason": "cap reached"})
        (self.project / "plan" / "ESCALATION.md").write_text("reason", encoding="utf-8")
        request = self.state.snapshot()["pending"][0]
        record = self.state.decide("escalation", request["id"], "stop", "",
                                   scope="remote", user="someone@example.com")
        self.assertEqual(record["decision"], "stop")

    def test_where_the_answer_came_from_is_part_of_the_answer(self):
        self.ledger({"event": "ALL_GREEN", "steps": ["S1", "S2"]})
        request = self.state.snapshot()["pending"][0]
        record = self.state.decide("review", request["id"], "approve", "played it")
        self.assertEqual((record["scope"], record["user"]), ("local", ""))

    def test_a_revision_or_response_cannot_be_empty(self):
        self.ledger({"event": "ALL_GREEN", "steps": ["S1", "S2"]})
        request = self.state.snapshot()["pending"][0]
        with self.assertRaisesRegex(ValueError, "requires a note"):
            self.state.decide("review", request["id"], "revise", "  ")


class TwoKindsOfStuck(DashboardFixture):
    """ランナーが詰まったことと、プランナーが断ったことは別の要求だ。

    どちらも同じ人に届く。だからこそ1つにまとめてはならない。「ランナーが
    このステップを通せなかった」に答えても、「許された改訂ではどれも役に
    立たない」には答えていない。基準か設計を変えなければならないのは、
    後者のほうだ。
    """

    def planner_escalation(self, text="# ESCALATE -- unreachable criterion"):
        (self.project / "plan" / "PLANNER_ESCALATION.md").write_text(text, encoding="utf-8")

    def test_the_planners_refusal_reaches_the_dashboard(self):
        self.planner_escalation()
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["phase"], "planner_escalated")
        self.assertEqual(snapshot["pending"][0]["kind"], "planner")
        self.assertIn("unreachable criterion", snapshot["pending"][0]["detail"])

    def test_the_two_escalations_are_separate_requests(self):
        self.ledger({"event": "RUN_ALL_STOP", "reason": "cap reached"})
        (self.project / "plan" / "ESCALATION.md").write_text("step S10 stopped", encoding="utf-8")
        self.planner_escalation()
        pending = self.state.snapshot()["pending"]
        self.assertEqual({item["kind"] for item in pending}, {"escalation", "planner"})

        runner_request = next(i for i in pending if i["kind"] == "escalation")
        self.state.decide("escalation", runner_request["id"], "respond", "criteria widened")
        remaining = self.state.snapshot()["pending"]
        self.assertEqual([item["kind"] for item in remaining], ["planner"])

    def test_a_phone_may_answer_or_stop_the_planners_refusal(self):
        # エスカレーションと同じ理由による。これは基準についての判断で、
        # 画面に何が出たかの主張ではない。
        self.planner_escalation()
        request = self.state.snapshot()["pending"][0]
        record = self.state.decide("planner", request["id"], "respond", "要件を書き直す",
                                   scope="remote", user="someone@example.com")
        self.assertEqual((record["kind"], record["scope"]), ("planner", "remote"))

    def test_an_answered_refusal_is_not_presented_again(self):
        self.planner_escalation()
        request = self.state.snapshot()["pending"][0]
        self.state.decide("planner", request["id"], "stop", "")
        self.assertEqual(self.state.snapshot()["pending"], [])


if __name__ == "__main__":
    unittest.main()
