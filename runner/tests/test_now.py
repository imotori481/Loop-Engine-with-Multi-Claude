"""いまの作業を、人が読める形で書き出すこと。

台帳は起きたことの記録だ。「いま誰がどのモジュールの何をしているか」は
now.json で伝える。人が見るための
写しなので、書けなくても走行は止めない。

    python3 -m unittest discover -s runner/tests
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop  # noqa: E402


class Now(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "logs").mkdir()
        (root / "plan").mkdir()
        (root / ".runner").mkdir()
        self.now_file = root / "logs" / "now.json"
        self.plan = root / "plan"
        self.tasks = root / ".runner" / "tasks.json"
        self.tasks.write_text(json.dumps({"steps": [
            {"id": "S1", "goal": "model the game state"},
            {"id": "S2", "goal": "buy a generator"},
            {"id": "S3", "goal": "render the page"},
        ]}), encoding="utf-8")
        (self.plan / "ledger.jsonl").write_text(
            json.dumps({"event": "GREEN", "step": "S1"}) + "\n", encoding="utf-8")
        for p in (mock.patch.object(loop, "NOW_FILE", self.now_file),
                  mock.patch.object(loop, "PLAN", self.plan),
                  mock.patch.object(loop, "LEDGER", self.plan / "ledger.jsonl"),
                  mock.patch.object(loop, "STAGE_FILE", self.plan / "stage.json"),
                  mock.patch.object(loop, "TASKS", self.tasks),
                  mock.patch.dict(loop.NOW, {"command": "run --all", "step": "S2",
                                             "attempt": None, "key": None, "since": None})):
            p.start()
            self.addCleanup(p.stop)

    def read(self) -> dict:
        return json.loads(self.now_file.read_text(encoding="utf-8"))


class WhoIsDoingWhat(Now):
    def test_the_solver_on_a_step_is_described_in_japanese(self):
        loop.NOW["attempt"] = 2
        loop.report_now("solver", "IMPL")
        activity = self.read()["activity"]
        self.assertEqual(activity["who_ja"], "ソルバー")
        self.assertEqual(activity["step"], "S2")
        self.assertEqual(activity["text_ja"], "S2 を実装している（試行 2）")

    def test_the_planner_is_described_by_the_command_it_is_in(self):
        cases = {"basic draft": "仕様書から基本設計仕様書を書いている",
                 "modules draft": "基本設計仕様書からモジュールの詳細設計仕様書を書いている"}
        for command, text in cases.items():
            with self.subTest(command=command):
                loop.NOW["command"] = command
                self.assertEqual(loop.describe_now("planner", "PLAN_PROPOSE"), text)

    def test_the_critic_reads_the_detailed_design(self):
        self.assertIn("詳細設計仕様書", loop.describe_now("critic", "CRITIQUE"))

    def test_the_runner_says_which_gate_it_is_running(self):
        self.assertIn("落ちる", loop.describe_now("runner", "red"))
        self.assertIn("確かめている", loop.describe_now("runner", "verify"))

    def test_nothing_is_an_empty_activity(self):
        loop.report_now("solver", "STUB")
        loop.report_now(None)
        self.assertIsNone(self.read()["activity"])


class TheSteps(Now):
    def test_each_step_carries_its_goal_and_state(self):
        loop.report_now("solver", "TEST_WRITE")
        steps = self.read()["steps"]
        self.assertEqual([(s["id"], s["state"]) for s in steps],
                         [("S1", "green"), ("S2", "active"), ("S3", "pending")])
        self.assertEqual(steps[1]["goal"], "buy a generator")

    def test_no_tasks_yet_is_an_empty_list(self):
        self.tasks.unlink()
        loop.report_now("planner", "PLAN_PROPOSE")
        self.assertEqual(self.read()["steps"], [])


class WhenItStarted(Now):
    def test_the_same_activity_keeps_its_start_time(self):
        with mock.patch.object(loop.time, "strftime", side_effect=["t1", "u1", "u2"]):
            loop.report_now("solver", "IMPL")
            loop.report_now("solver", "IMPL")
        self.assertEqual(self.read()["activity"]["since"], "t1")

    def test_a_new_attempt_starts_a_new_clock(self):
        with mock.patch.object(loop.time, "strftime",
                               side_effect=["t1", "u1", "t2", "u2"]):
            loop.NOW["attempt"] = 1
            loop.report_now("solver", "IMPL")
            loop.NOW["attempt"] = 2
            loop.report_now("solver", "IMPL")
        self.assertEqual(self.read()["activity"]["since"], "t2")


class NeverInTheWay(Now):
    def test_an_unwritable_place_does_not_stop_the_run(self):
        with mock.patch.object(loop, "NOW_FILE", Path("/nonexistent/dir/now.json")):
            loop.report_now("solver", "IMPL")

    def test_an_agent_call_is_bracketed_by_the_report(self):
        seen = []
        with mock.patch.object(loop, "report_now",
                               lambda who, phase="", detail="": seen.append((who, phase, detail))), \
             mock.patch.object(loop, "ledger", lambda *a, **k: None):
            loop.run_agent("critic", "CRITIQUE",
                           lambda: subprocess.CompletedProcess([], 0, "ok", ""),
                           detail="trace")
        self.assertEqual(seen, [("critic", "CRITIQUE", "trace"), ("runner", "check", "")])


if __name__ == "__main__":
    unittest.main()
