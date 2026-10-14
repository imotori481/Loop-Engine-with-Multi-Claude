"""完了の事実は、次のモジュールを待たずに、最後の push で届かなければならない。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop  # noqa: E402


class GreenCheckpoint(unittest.TestCase):
    @patch("loop.publish")
    @patch("loop.run")
    @patch("loop.ledger")
    def test_green_is_written_before_the_module_is_committed(self, ledger, run, publish):
        order = []
        ledger.side_effect = lambda *args, **kwargs: order.append("ledger")
        run.side_effect = lambda *args, **kwargs: order.append("git")
        publish.side_effect = lambda *args, **kwargs: order.append("publish")

        loop.complete_green("game/state", "finish the game", 1, design="d1")

        self.assertEqual(order, ["ledger", "git", "git", "git", "publish"])
        ledger.assert_called_once_with("GREEN", step="game/state", attempts=1, design="d1")
        self.assertEqual(run.call_args_list[0], call(["git", "add", "-A"], check=True))
        # タグの名前に / は使えない。
        self.assertEqual(run.call_args_list[2],
                         call(["git", "tag", "-f", "module-game--state"], check=True))


class RunCheckpoint(unittest.TestCase):
    """全部が緑になったら、台帳と段だけをコミットして最終確認の段へ移す。"""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        plan = Path(temp.name) / "plan"
        plan.mkdir()
        self.ledger_file = plan / "ledger.jsonl"
        self.stage_file = plan / "stage.json"
        self.stage_file.write_text('{"stage": "downstream"}', encoding="utf-8")
        for p in (patch.object(loop, "LEDGER", self.ledger_file),
                  patch.object(loop, "STAGE_FILE", self.stage_file)):
            p.start()
            self.addCleanup(p.stop)

    def complete(self):
        with patch("loop.commit_paths") as commit:
            loop.complete_run({"b", "a"}, {"steps": [{"id": "a"}, {"id": "b"}],
                                           "digest": "abc"})
        return commit

    def test_all_green_is_recorded_and_the_stage_moves_to_review(self):
        commit = self.complete()
        record = json.loads(self.ledger_file.read_text(encoding="utf-8"))
        self.assertEqual((record["event"], record["steps"], record["plan_sha256"]),
                         ("ALL_GREEN", ["a", "b"], "abc"))
        self.assertEqual(loop.read_stage()["stage"], "review")
        commit.assert_called_once_with([self.ledger_file, self.stage_file],
                                       "run: all modules green", "run completion")

    def test_a_record_that_never_reached_a_commit_is_committed_once_more(self):
        # 記録はすでにファイルにある。2回書くと、走行が2回完了したと報告する。
        self.complete()
        self.complete().assert_called_once()
        lines = self.ledger_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)


class GreenIsReadFromTheLedger(unittest.TestCase):
    """REOPEN は、そのモジュールの緑を取り消す。"""

    def test_a_reopened_module_is_no_longer_green(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ledger.jsonl"
            path.write_text("\n".join(json.dumps(r) for r in [
                {"event": "GREEN", "step": "a", "design": "d1"},
                {"event": "GREEN", "step": "b", "design": "d2"},
                {"event": "REOPEN", "modules": ["a"]},
            ]) + "\n", encoding="utf-8")
            with patch.object(loop, "LEDGER", path):
                self.assertEqual(loop.green_designs(), {"b": "d2"})


if __name__ == "__main__":
    unittest.main()
