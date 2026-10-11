"""人の判断を計画に届ける。

受け入れ条件の書き換え (b) と上流からの作り直し (c) は人の権限で、プランナーの経路では
拒まれる。人はそれを plan/tasks.json の手編集でしか入れられず、`reset` の
`git reset --hard` がコミットしていない計画を捨てた（RUNNER_SPEC 6-2、run 7）。
ダッシュボードの回答もホストに残るだけで、プランナーに届かなかった。

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

STEP = {"id": "S2", "goal": "g", "acceptance": [{"case": "normal", "given": "a", "then": "b"}]}
GREEN = {"id": "S1", "goal": "g", "acceptance": []}


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout


def plan(*steps: dict) -> str:
    return json.dumps({"steps": list(steps)}, indent=2) + "\n"


class Project(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "project"
        self.human = Path(temp.name) / "human"
        self.human.mkdir()
        self.plan = self.root / "plan"
        self.plan.mkdir(parents=True)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "runner@example.com")
        git(self.root, "config", "user.name", "runner")
        (self.root / ".gitignore").write_text(".runner/\n", encoding="utf-8")
        self.code = self.root / "src" / "a.py"
        self.code.parent.mkdir()
        self.code.write_text("GREEN\n", encoding="utf-8")
        (self.root / "tests").mkdir()
        (self.plan / "tasks.json").write_text(plan(GREEN, STEP), encoding="utf-8")
        (self.plan / "ESCALATION.md").write_text("# ESCALATION: step S2\n", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")

        self.events: list[tuple[str, dict]] = []
        self.green = {"S1"}
        real_run = loop.run
        for name, value in {
            "PROJECT": self.root, "PLAN": self.plan, "LEDGER": self.plan / "ledger.jsonl",
            "STATE": self.root / ".runner", "TESTS": self.root / "tests",
            "SRC": self.code.parent, "ESCALATION": self.plan / "ESCALATION.md",
            "PLANNER_ESCALATION": self.plan / "PLANNER_ESCALATION.md",
            "HUMAN_ANSWER": self.human / "ANSWER.md",
            "AMENDED_PLAN": self.human / "tasks.json",
            "run": lambda cmd, **kw: real_run(cmd, **{**kw, "cwd": self.root}),
            "set_writable": mock.Mock(), "adopt": mock.Mock(), "publish": mock.Mock(),
            "validate_plan": mock.Mock(return_value=[]),
            "green_steps": lambda: set(self.green),
            "ledger": self.record,
        }.items():
            p = mock.patch.object(loop, name, value)
            p.start()
            self.addCleanup(p.stop)

    def record(self, event: str, **fields) -> None:
        self.events.append((event, fields))
        with (self.plan / "ledger.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": event, **fields}) + "\n")

    def amend(self, text: str, step: str = "S2") -> int:
        (self.human / "tasks.json").write_text(text, encoding="utf-8")
        return loop.cmd_plan_amend(step)


class Amending(Project):
    REWRITTEN = {**STEP, "acceptance": [{"case": "normal", "given": "a", "then": "c"}]}

    def test_the_plan_is_committed_and_the_step_starts_again_from_the_last_green(self):
        self.code.write_text("HALF DONE\n", encoding="utf-8")
        (self.human / "ANSWER.md").write_text("rewrite S2", encoding="utf-8")
        self.assertEqual(self.amend(plan(GREEN, self.REWRITTEN)), 0)

        # reset の後も、書き換えた計画が HEAD に残る。
        self.assertEqual(json.loads(git(self.root, "show", "HEAD:plan/tasks.json")),
                         {"steps": [GREEN, self.REWRITTEN]})
        self.assertIn("the human amended step S2", git(self.root, "log", "-1", "--format=%s"))
        self.assertEqual(self.code.read_text(encoding="utf-8"), "GREEN\n")
        # 計画を直したことが、エスカレーションと人の答えへの答えになる。
        for answered in ("ESCALATION.md",):
            self.assertFalse((self.plan / answered).exists())
            self.assertEqual(git(self.root, "ls-files", f"plan/{answered}"), "")
        self.assertFalse((self.human / "ANSWER.md").exists())
        self.assertFalse((self.human / "tasks.json").exists())
        self.assertEqual([e for e, _ in self.events], ["PLAN_AMEND", "RESET"])
        self.assertEqual(git(self.root, "status", "--porcelain", "--", "plan/tasks.json"), "")

    def test_a_plan_the_linter_refuses_changes_nothing(self):
        loop.validate_plan.return_value = ["L6: step S2 has no error case"]
        self.assertEqual(self.amend(plan(GREEN, self.REWRITTEN)), 2)
        self.assertEqual(json.loads((self.plan / "tasks.json").read_text(encoding="utf-8")),
                         {"steps": [GREEN, STEP]})
        self.assertTrue((self.human / "tasks.json").exists())
        self.assertTrue((self.plan / "ESCALATION.md").exists())
        self.assertEqual(self.events[0][0], "PLAN_AMEND_REJECT")

    def test_the_step_must_be_in_the_plan(self):
        self.assertEqual(self.amend(plan(GREEN, self.REWRITTEN), step="S9"), 2)
        self.assertIn("no step S9", self.events[0][1]["violations"][0])

    def test_text_that_is_not_json_is_refused(self):
        self.assertEqual(self.amend("{ not json"), 2)

    def test_nothing_placed_is_nothing_to_do(self):
        self.assertEqual(loop.cmd_plan_amend("S2"), 1)

    def test_a_changed_green_step_is_named_because_it_does_not_run_again(self):
        self.assertEqual(self.amend(plan({**GREEN, "goal": "other"}, self.REWRITTEN)), 0)
        self.assertEqual(self.events[0], ("PLAN_AMEND", {"step": "S2", "green_changed": ["S1"]}))


class TheHumansAnswerReachesThePlanner(Project):
    def setUp(self) -> None:
        super().setUp()
        (self.plan / "CONTEXT.md").write_text("context", encoding="utf-8")
        for p in (mock.patch.object(loop, "SYSTEM_SPEC", self.plan / "SYSTEM_SPEC.md"),
                  mock.patch.object(loop, "r4_allowance", return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def test_the_revision_brief_carries_the_answer(self):
        (self.human / "ANSWER.md").write_text("Drop the timing criterion of S2.\n",
                                              encoding="utf-8")
        brief = loop.brief_plan_revise(None, "IMPL")
        self.assertIn("# The human's answer", brief)
        self.assertIn("Drop the timing criterion of S2.", brief)
        self.assertIn("loop plan amend", brief)

    def test_without_an_answer_the_brief_has_no_section(self):
        self.assertNotIn("The human's answer", loop.brief_plan_revise(None, "IMPL"))

    def test_an_applied_plan_has_used_the_answer(self):
        (self.human / "ANSWER.md").write_text("answer", encoding="utf-8")
        out = self.root / ".runner" / "out"
        out.mkdir(parents=True)
        proposal = {"tasks.json": plan(GREEN, {**STEP, "goal": "tighter"})}
        with mock.patch.object(loop, "read_proposal", return_value=proposal), \
             mock.patch.object(loop, "proposal_problems", return_value=[]), \
             mock.patch.object(loop, "PLANNER_OUT", out), \
             mock.patch.object(loop, "REFINE_STATE", self.root / ".runner" / "refine.json"), \
             mock.patch.object(loop, "settle_critique"), \
             mock.patch.dict(loop.PROPOSAL_FILES, {"tasks.json": self.plan / "tasks.json"},
                             clear=True):
            self.assertEqual(loop.cmd_plan_apply(), 0)
        self.assertFalse((self.human / "ANSWER.md").exists())


class AnAnsweredEscalationIsRevisedFirst(Project):
    """止まったステップを走らせ直すと、上限を使い切ったステップはまた止まり、答えは
    プランナーに届かない。だから答えがあれば、先にプランナーを通す。"""

    def run_all(self) -> list[str]:
        calls: list[str] = []
        self.green = {"S1", "S2"}
        with mock.patch.object(loop, "load_settings"), \
             mock.patch.object(loop, "complete_run"), \
             mock.patch.object(loop, "cmd_plan_propose",
                               side_effect=lambda s: calls.append(f"propose {s}") or 0), \
             mock.patch.object(loop, "cmd_plan_apply",
                               side_effect=lambda: calls.append("apply") or 0), \
             mock.patch.object(loop, "cmd_reset",
                               side_effect=lambda s: calls.append(f"reset {s}") or 0), \
             mock.patch.object(loop, "run_step",
                               side_effect=lambda s, **k: calls.append(f"run {s}") or 0):
            self.assertEqual(loop.cmd_run_all(), 0)
        return calls

    def test_an_answer_goes_to_the_planner_before_the_step_runs(self):
        (self.human / "ANSWER.md").write_text("answer", encoding="utf-8")
        self.assertEqual(self.run_all(), ["propose S2", "apply", "reset S2"])

    def test_without_an_answer_nothing_is_asked(self):
        self.assertEqual(self.run_all(), [])

    def test_the_step_is_read_from_the_heading(self):
        self.assertEqual(loop.escalated_step(), "S2")
        (self.plan / "ESCALATION.md").unlink()
        self.assertIsNone(loop.escalated_step())


if __name__ == "__main__":
    unittest.main()
