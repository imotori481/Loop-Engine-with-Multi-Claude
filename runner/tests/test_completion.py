"""完了の事実は、次のステップを待たずに、最後の push で届かなければならない。"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop  # noqa: E402


class GreenCheckpoint(unittest.TestCase):
    @patch("loop.publish")
    @patch("loop.run")
    @patch("loop.ledger")
    def test_green_is_written_before_the_step_is_committed(self, ledger, run, publish):
        order = []
        ledger.side_effect = lambda *args, **kwargs: order.append("ledger")
        run.side_effect = lambda *args, **kwargs: order.append("git")
        publish.side_effect = lambda *args, **kwargs: order.append("publish")

        loop.complete_green("S11", "finish the game", 1)

        self.assertEqual(order, ["ledger", "git", "git", "git", "publish"])
        ledger.assert_called_once_with("GREEN", step="S11", attempts=1)
        self.assertEqual(run.call_args_list[0], call(["git", "add", "-A"], check=True))


class RunCheckpoint(unittest.TestCase):
    @patch("loop.publish")
    @patch("loop.run")
    @patch("loop.ledger")
    @patch("loop.completion_recorded", return_value=False)
    def test_all_green_is_committed_before_it_is_published(
            self, recorded, ledger, run, publish):
        order = []
        ledger.side_effect = lambda *args, **kwargs: order.append("ledger")
        run.side_effect = lambda *args, **kwargs: order.append("git")
        publish.side_effect = lambda *args, **kwargs: order.append("publish")

        loop.complete_run({"S2", "S1"}, {"steps": [{"id": "S1"}, {"id": "S2"}]})

        self.assertEqual(order, ["ledger", "git", "git", "publish"])
        self.assertEqual(ledger.call_args.args[0], "ALL_GREEN")
        self.assertEqual(ledger.call_args.kwargs["steps"], ["S1", "S2"])
        self.assertEqual(run.call_args_list[0], call(
            ["git", "add", "--", "plan/ledger.jsonl"], check=True))
        publish.assert_called_once_with("run completion")

    @patch("loop.publish")
    @patch("loop.run")
    @patch("loop.ledger")
    @patch("loop.completion_recorded", return_value=True)
    def test_reopening_a_completed_plan_does_not_create_another_commit(
            self, recorded, ledger, run, publish):
        loop.complete_run({"S1"}, {"steps": [{"id": "S1"}]})
        ledger.assert_not_called()
        run.assert_not_called()
        publish.assert_not_called()


class DurableCompletion(unittest.TestCase):
    """「記録した」は「コミットにある」を意味しなければならない。そうでないと、
    記録を書いてからコミットするまでの隙間が、抜け出せない状態になる。"""

    @patch("loop.run")
    def test_the_committed_ledger_is_what_is_searched(self, run):
        run.return_value = SimpleNamespace(
            returncode=0, stdout='{"event": "ALL_GREEN", "plan_sha256": "abc"}\n')
        self.assertTrue(loop.completion_recorded("abc"))
        self.assertFalse(loop.completion_recorded("a different plan"))

    @patch("loop.run")
    def test_a_ledger_absent_from_HEAD_is_not_a_completed_run(self, run):
        run.return_value = SimpleNamespace(returncode=128, stdout="")
        self.assertFalse(loop.completion_recorded("abc"))

    @patch("loop.publish")
    @patch("loop.run")
    @patch("loop.ledger")
    @patch("loop.completion_written", return_value=True)
    @patch("loop.completion_recorded", return_value=False)
    def test_a_record_that_never_reached_a_commit_is_committed_once_more(
            self, recorded, written, ledger, run, publish):
        loop.complete_run({"S1"}, {"steps": [{"id": "S1"}]})

        # 記録はすでにファイルにある。2回書くと、走行が2回完了したと報告する。
        ledger.assert_not_called()
        self.assertEqual([args.args[0][:2] for args in run.call_args_list],
                         [["git", "add"], ["git", "commit"]])
        publish.assert_called_once_with("run completion")


if __name__ == "__main__":
    unittest.main()
