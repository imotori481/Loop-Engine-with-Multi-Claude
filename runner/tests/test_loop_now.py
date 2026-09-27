"""`loop now` が、ログから今の回のトークン数とクリティックの指摘を読むこと。

台帳は runner だけが読め、写しに届くのはコミットのときだけだ。計画づくりの途中や
refine で止まったときの消費と指摘は、保守ユーザーが読めるログにしか無い。

    python3 -m unittest discover -s runner/tests
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "provision"))

import loop_now  # noqa: E402


class CurrentLoop(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.logs = Path(temp.name)

    def log(self, name: str, *lines: str) -> None:
        (self.logs / name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def loop(self, project: str = "game"):
        return loop_now.current_loop(loop_now.project_logs(str(self.logs), project))

    def test_usage_lines_are_summed_per_role_from_the_last_go(self):
        self.log("game-20260101-000000.log",
                 "=== loop go 開始 (2026-01-01 00:00:00) プロジェクト=game",
                 "[USAGE] who=planner phase=PLAN_PROPOSE model=m in=100 out=1 sec=1")
        self.log("game-20260102-000000.log",
                 "=== loop go 開始 (2026-01-02 00:00:00) プロジェクト=game",
                 "[USAGE] who=planner phase=PLAN_PROPOSE model=m in=10 out=2 sec=1 usd=0.10",
                 "[USAGE] who=critic phase=CRITIQUE model=m in=5 out=1 sec=1")
        self.log("game-20260102-010000.log",
                 "=== loop continue 開始 (2026-01-02 01:00:00) プロジェクト=game",
                 "[USAGE] who=solver phase=IMPL model=m in=7 out=3 sec=1 ERROR")
        loop = self.loop()
        self.assertTrue(loop["started"].startswith("2026-01-02T00:00:00"))
        self.assertEqual(loop["tokens"], {"planner": 12, "critic": 6, "solver": 10})
        self.assertEqual(loop["calls"], 3)

    def test_the_last_critique_is_kept_with_the_time_of_its_command(self):
        self.log("game-20260102-000000.log",
                 "=== loop go 開始 (2026-01-02 00:00:00) プロジェクト=game",
                 "--- loop.py plan refine (2026-01-02 00:05:00)",
                 "=== critique 1 of at most 3 ===",
                 "## coverage",
                 "1. first",
                 "[PLAN_REFINE] round=1 findings=1",
                 "=== critique 2 of at most 3 ===",
                 "## trace",
                 "1. second",
                 "   NO GATE WOULD CATCH THIS",
                 "[REFINE_CAP] round=2 findings=1",
                 "=== loop 終了 (2026-01-02 00:30:00): クリティックの指摘が残った")
        critique = self.loop()["critique"]
        self.assertTrue(critique["at"].startswith("2026-01-02T00:05:00"))
        self.assertEqual(critique["text"], "=== critique 2 of at most 3 ===\n## trace\n"
                                           "1. second\n   NO GATE WOULD CATCH THIS")

    def test_other_projects_and_the_latest_link_are_not_read(self):
        self.log("game-latest.log", "=== loop go 開始 (2026-01-03 00:00:00) プロジェクト=game")
        self.log("game-2-20260104-000000.log",
                 "=== loop go 開始 (2026-01-04 00:00:00) プロジェクト=game-2")
        self.assertIsNone(self.loop())

    def test_the_outcome_follows_the_run_and_the_last_result(self):
        self.assertEqual(loop_now.outcome(True, None), "running")
        self.assertEqual(loop_now.outcome(False, "loop 終了 (x): 全ステップが緑になった"), "green")
        self.assertEqual(loop_now.outcome(False, "loop 終了 (x): 利用枠が尽きた"), "stopped")


if __name__ == "__main__":
    unittest.main()
