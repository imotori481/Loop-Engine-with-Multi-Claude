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
        modules = self.project / "docs" / "modules"
        modules.mkdir(parents=True)
        for module in ("S1", "S2"):
            (modules / f"{module}.md").write_text("## 振る舞い", encoding="utf-8")
        self.state = DashboardState(self.project, root / "state")

    def tearDown(self):
        self.temp.cleanup()

    def stage(self, name):
        (self.project / "plan" / "stage.json").write_text(
            json.dumps({"stage": name}), encoding="utf-8")

    def all_green(self):
        self.stage("review")
        self.ledger({"event": "GREEN", "step": "S1"}, {"event": "GREEN", "step": "S2"},
                    {"event": "ALL_GREEN", "steps": ["S1", "S2"]})

    def ledger(self, *records):
        (self.project / "plan" / "ledger.jsonl").write_text(
            "".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")


class ReadingState(DashboardFixture):
    def test_green_progress_is_derived_from_the_ledger(self):
        self.ledger({"event": "GREEN", "step": "S1"})
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["steps"], {"total": 2, "green": 1, "ids": ["S1", "S2"]})

    def test_a_reopened_module_is_no_longer_green(self):
        self.ledger({"event": "GREEN", "step": "S1"}, {"event": "GREEN", "step": "S2"},
                    {"event": "REOPEN", "modules": ["S1"]})
        self.assertEqual(self.state.snapshot()["steps"]["green"], 1)

    def test_module_ids_follow_the_paths_under_docs_modules(self):
        nested = self.project / "docs" / "modules" / "game"
        nested.mkdir()
        (nested / "board.md").write_text("", encoding="utf-8")
        self.assertEqual(self.state.snapshot()["steps"]["ids"], ["S1", "S2", "game/board"])

    def test_the_phase_is_the_stage(self):
        self.assertEqual(self.state.snapshot()["phase"], "not_started")
        self.stage("midstream")
        self.assertEqual(self.state.snapshot()["phase"], "midstream")

    def test_all_green_at_the_review_stage_asks_for_the_final_check(self):
        self.all_green()
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["phase"], "review")
        self.assertEqual(snapshot["pending"][0]["kind"], "review")

    def test_an_all_green_left_behind_by_a_reopen_asks_for_nothing(self):
        self.all_green()
        self.stage("midstream")
        self.assertEqual(self.state.snapshot()["pending"], [])

    def test_unreadable_lines_are_reported_instead_of_silently_dropped(self):
        path = self.project / "plan" / "ledger.jsonl"
        path.write_text('{"event":"GREEN"}\nnot json\n', encoding="utf-8")
        self.assertEqual(read_jsonl(path)[1], {"event": "UNREADABLE_LEDGER_RECORD", "line": 2})


def usage(who, **tokens):
    return {"event": "USAGE", "who": who, "usage": tokens}


class TokenRuns(DashboardFixture):
    def mirror(self, name, *records):
        path = Path(self.temp.name) / name / "plan" / "ledger.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")

    def test_a_loop_runs_from_the_spec_import_and_counts_the_design_too(self):
        # 設計と批評は run --all より前に流れる。そこを落とすと、
        # クリティックは常に 0 になる。
        self.ledger(
            {"ts": "1", "event": "SPEC_IMPORT"},
            usage("planner", input_tokens=10, cache_read_input_tokens=5, output_tokens=2),
            usage("critic", cache_creation_input_tokens=7),
            {"event": "RUN_ALL_START"},
            usage("solver", input_tokens=3),
            {"event": "RUN_ALL_STOP", "reason": "cap reached"},
            {"event": "RUN_ALL_START"},
            usage("solver", input_tokens=4),
            {"event": "ALL_GREEN", "steps": ["S1", "S2"]},
        )
        runs = self.state.snapshot()["token_runs"]
        self.assertEqual([(r["run"], r["outcome"], r["tokens"]) for r in runs], [
            (1, "green", {"planner": 17, "critic": 7, "solver": 7}),
        ])

    def test_each_role_keeps_its_tokens_by_kind_and_its_cost(self):
        # 合計だけでは、ブリーフが大きいのか、ターンが多いのかが分からない。
        self.ledger(
            {"ts": "1", "event": "SPEC_IMPORT"},
            {**usage("critic", input_tokens=4, cache_creation_input_tokens=100,
                     cache_read_input_tokens=120, output_tokens=15), "cost_usd": 0.5},
            {**usage("critic", cache_read_input_tokens=30, output_tokens=5), "cost_usd": 0.25},
            usage("solver", input_tokens=1),
        )
        run = self.state.snapshot()["token_runs"][0]
        self.assertEqual(run["kinds"]["critic"],
                         {"input": 4, "cache_write": 100, "cache_read": 150, "output": 20})
        self.assertEqual(run["tokens"]["critic"], 274)
        self.assertEqual(run["usd"], {"planner": 0.0, "critic": 0.75, "solver": 0.0})

    def test_a_second_spec_import_starts_the_next_loop(self):
        self.ledger(
            {"ts": "1", "event": "SPEC_IMPORT"}, usage("planner", output_tokens=1),
            {"ts": "2", "event": "SPEC_IMPORT"}, usage("planner", output_tokens=2),
        )
        runs = self.state.snapshot()["token_runs"]
        self.assertEqual([(r["outcome"], r["tokens"]["planner"]) for r in runs],
                         [("abandoned", 1), ("running", 2)])

    def test_records_before_any_spec_import_form_the_first_loop(self):
        self.ledger({"ts": "1", "event": "STAGE"}, usage("solver", input_tokens=5))
        self.assertEqual(self.state.snapshot()["token_runs"][0]["tokens"]["solver"], 5)

    def test_a_loop_without_usage_is_left_out(self):
        self.ledger({"ts": "1", "event": "SPEC_IMPORT"}, {"event": "ALL_GREEN"})
        self.assertEqual(self.state.snapshot()["token_runs"], [])

    def test_the_history_spans_every_mirror_in_start_order(self):
        # 1つの台帳には、ふつう1回分しか入らない。点が1つでは線にならない。
        self.mirror("runs/run-001", {"ts": "1", "event": "SPEC_IMPORT"},
                    usage("planner", output_tokens=1))
        self.mirror("projects/game", {"ts": "3", "event": "SPEC_IMPORT"},
                    usage("planner", output_tokens=3))
        self.ledger({"ts": "2", "event": "SPEC_IMPORT"}, usage("planner", output_tokens=2))
        runs = self.state.snapshot()["token_runs"]
        self.assertEqual([(r["run"], r["source"]) for r in runs],
                         [(1, "runs/run-001"), (2, "project"), (3, "projects/game")])

    def test_a_run_stays_after_its_ledger_is_gone(self):
        # 写しの台帳は作業ツリーにある。ブランチを切り替えると読めなくなる。
        self.mirror("projects/game", {"ts": "1", "event": "SPEC_IMPORT"},
                    usage("planner", output_tokens=3))
        self.state.snapshot()
        (Path(self.temp.name) / "projects" / "game" / "plan" / "ledger.jsonl").unlink()
        runs = self.state.snapshot()["token_runs"]
        self.assertEqual([(r["run"], r["source"], r["tokens"]["planner"]) for r in runs],
                         [(1, "projects/game", 3)])

    def test_a_run_seen_again_updates_the_stored_one(self):
        self.ledger({"ts": "1", "event": "SPEC_IMPORT"}, usage("planner", output_tokens=1))
        self.state.snapshot()
        self.ledger({"ts": "1", "event": "SPEC_IMPORT"}, usage("planner", output_tokens=1),
                    usage("solver", input_tokens=4), {"event": "ALL_GREEN"})
        runs = self.state.snapshot()["token_runs"]
        self.assertEqual([(r["outcome"], r["calls"], r["tokens"]["solver"]) for r in runs],
                         [("green", 2, 4)])

    def test_the_stored_run_keeps_its_kinds_and_cost(self):
        self.ledger({"ts": "1", "event": "SPEC_IMPORT"},
                    {**usage("critic", input_tokens=4, cache_creation_input_tokens=100,
                             cache_read_input_tokens=120, output_tokens=15), "cost_usd": 0.5})
        before = self.state.snapshot()["token_runs"]
        (self.project / "plan" / "ledger.jsonl").unlink()
        after = self.state.snapshot()["token_runs"]
        self.assertEqual(after, before)

    def test_the_project_name_is_kept_when_only_the_live_mirror_is_newer(self):
        self.mirror("projects/game", {"ts": "1", "event": "SPEC_IMPORT"},
                    usage("planner", output_tokens=1))
        self.ledger({"ts": "1", "event": "SPEC_IMPORT"}, usage("planner", output_tokens=1),
                    usage("solver", input_tokens=2))
        runs = self.state.snapshot()["token_runs"]
        self.assertEqual([(r["source"], r["calls"]) for r in runs], [("projects/game", 2)])

    def test_an_unreadable_stored_row_is_skipped(self):
        (Path(self.temp.name) / "token-history.csv").write_text(
            "source,started\nbroken,1\n", encoding="utf-8")
        self.ledger({"ts": "2", "event": "SPEC_IMPORT"}, usage("planner", output_tokens=1))
        runs = self.state.snapshot()["token_runs"]
        self.assertEqual([r["started"] for r in runs], ["2"])

    def test_the_live_mirror_does_not_repeat_its_project(self):
        records = ({"ts": "1", "event": "SPEC_IMPORT"}, usage("planner", output_tokens=1))
        self.mirror("projects/game", *records)
        self.ledger(*records)
        runs = self.state.snapshot()["token_runs"]
        self.assertEqual([r["source"] for r in runs], ["projects/game"])


class Decisions(DashboardFixture):
    def test_a_decision_answers_only_the_exact_pending_review(self):
        self.all_green()
        request = self.state.snapshot()["pending"][0]
        record = self.state.decide("review", request["id"], "approve", "played it")
        self.assertEqual(record["decision"], "approve")
        snapshot = self.state.snapshot()
        self.assertEqual((snapshot["pending"], snapshot["phase"]), ([], "human_reviewed"))

    def test_a_stale_or_invented_request_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no longer pending"):
            self.state.decide("review", "invented", "approve", "")

    def test_recording_an_answer_never_rewrites_the_earlier_ones(self):
        self.state.data_dir.mkdir(parents=True, exist_ok=True)
        self.state.decisions_file.write_text('{"event":"HUMAN_DECISION","kind":"old"}\n',
                                             encoding="utf-8")
        self.all_green()
        request = self.state.snapshot()["pending"][0]
        self.state.decide("review", request["id"], "approve", "played it")
        lines = self.state.decisions_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["kind"], "old")

    def test_a_phone_may_refuse_the_work_but_may_not_certify_it(self):
        # 最終確認があるのは、どの機械も画面を見られないからだ。窓を開けない
        # 端末に、窓が大丈夫だと言わせてはならない。ただし「まだ足りない」と
        # 言わせても、失うものは無い。
        self.all_green()
        request = self.state.snapshot()["pending"][0]
        with self.assertRaisesRegex(ValueError, "machine that can run the result"):
            self.state.decide("review", request["id"], "approve", "",
                              scope="remote", user="someone@example.com")
        record = self.state.decide("review", request["id"], "revise", "実行できない",
                                   scope="remote", user="someone@example.com")
        self.assertEqual((record["scope"], record["user"]),
                         ("remote", "someone@example.com"))

    def test_where_the_answer_came_from_is_part_of_the_answer(self):
        self.all_green()
        request = self.state.snapshot()["pending"][0]
        record = self.state.decide("review", request["id"], "approve", "played it")
        self.assertEqual((record["scope"], record["user"]), ("local", ""))

    def test_a_revision_cannot_be_empty(self):
        self.all_green()
        request = self.state.snapshot()["pending"][0]
        with self.assertRaisesRegex(ValueError, "requires a note"):
            self.state.decide("review", request["id"], "revise", "  ")


class PullRequestRecord(DashboardFixture):
    def approve(self):
        self.all_green()
        request = self.state.snapshot()["pending"][0]
        self.state.decide("review", request["id"], "approve", "played it")
        return request["id"]

    def test_nothing_to_send_until_the_review_is_approved(self):
        self.all_green()
        self.assertIsNone(self.state.approved())
        self.assertIsNone(self.state.snapshot()["pull_request"])

    def test_the_approval_carries_the_steps_and_the_note(self):
        request = self.approve()
        approval = self.state.approved()
        self.assertEqual((approval["request_id"], approval["steps"], approval["note"]),
                         (request, ["S1", "S2"], "played it"))
        self.assertEqual(self.state.snapshot()["pull_request"],
                         {"request_id": request, "result": None})

    def test_the_latest_result_for_the_approved_review_is_shown(self):
        request = self.approve()
        self.state.record_pull_request(request, {"error": "gh is not logged in"})
        self.state.record_pull_request(request, {"url": "https://github.com/o/r/pull/1"})
        result = self.state.snapshot()["pull_request"]["result"]
        self.assertEqual(result["url"], "https://github.com/o/r/pull/1")

    def test_a_later_all_green_is_not_covered_by_the_earlier_approval(self):
        self.approve()
        self.ledger({"event": "ALL_GREEN", "steps": ["S1", "S2"]},
                    {"event": "ALL_GREEN", "steps": ["S1", "S2", "S3"]})
        self.assertIsNone(self.state.approved())


if __name__ == "__main__":
    unittest.main()
