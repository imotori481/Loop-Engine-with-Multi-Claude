"""終えた計画を退避して、同じリポジトリで次の計画を起こせるか。

新しい計画のステップ id は前の計画と重なる。台帳が残ると、前の計画の GREEN を
新しいステップの緑と読む。

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

TWO_STEPS = {"steps": [{"id": "S1"}, {"id": "S2"}]}


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout


class ArchiveThePlan(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        plan = self.root / "plan"
        state = self.root / ".runner"
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "runner@example.com")
        git(self.root, "config", "user.name", "runner")
        self.publish = mock.Mock()
        real_run = loop.run
        for name, value in {
            "PROJECT": self.root, "PLAN": plan, "STATE": state,
            "LEDGER": plan / "ledger.jsonl",
            "ESCALATION": plan / "ESCALATION.md",
            "PLANNER_ESCALATION": plan / "PLANNER_ESCALATION.md",
            "REFINE_ESCALATION": state / "refine-escalation.md",
            "PLAN_ARCHIVE": plan / "archive",
            "PROPOSAL_FILES": {"SYSTEM_SPEC.md": plan / "SYSTEM_SPEC.md",
                               "CONTEXT.md": plan / "CONTEXT.md",
                               "tasks.json": plan / "tasks.json"},
            "run": lambda cmd, **kw: real_run(cmd, **{**kw, "cwd": self.root}),
            "publish": self.publish,
        }.items():
            p = mock.patch.object(loop, name, value)
            p.start()
            self.addCleanup(p.stop)

        (self.root / ".gitignore").write_text(".runner/\n", encoding="utf-8")
        plan.mkdir()
        (plan / "tasks.json").write_text(json.dumps(TWO_STEPS), encoding="utf-8")
        (plan / "SYSTEM_SPEC.md").write_text("spec", encoding="utf-8")
        (plan / "CONTEXT.md").write_text("context", encoding="utf-8")
        (self.root / "src").mkdir()
        (self.root / "src" / "game.py").write_text("x = 1\n", encoding="utf-8")
        (state / "contracts").mkdir(parents=True)
        (state / "contracts" / "S1.json").write_text("{}", encoding="utf-8")
        (state / "freeze").mkdir()
        (state / "freeze" / "S1.json").write_text("{}", encoding="utf-8")

    def green(self, *ids: str) -> None:
        for sid in ids:
            loop.ledger("GREEN", step=sid, attempts=1)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "steps")

    def test_a_finished_plan_moves_with_its_ledger_and_is_committed(self):
        self.green("S1", "S2")
        self.assertIsNone(loop.archive_plan())

        archived = self.root / "plan" / "archive" / "001"
        self.assertEqual(sorted(p.name for p in archived.iterdir()),
                         ["CONTEXT.md", "SYSTEM_SPEC.md", "ledger.jsonl", "tasks.json"])
        self.assertFalse((self.root / "plan" / "tasks.json").exists())
        # 新しい台帳には退避の記録だけがあり、前の GREEN は無い。
        self.assertEqual(loop.green_steps(), set())
        records = [json.loads(line) for line in
                   (self.root / "plan" / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["event"] for r in records], ["PLAN_ARCHIVE"])
        self.assertEqual(records[0]["to"], "plan/archive/001")
        # 退避はコミットされ、コードはそのまま残る。
        self.assertEqual(git(self.root, "status", "--porcelain"), "")
        self.assertIn("plan/archive/001/tasks.json", git(self.root, "ls-files"))
        self.assertTrue((self.root / "src" / "game.py").exists())
        self.publish.assert_called_once_with("the archived plan")

    def test_contracts_and_freeze_manifests_of_the_old_plan_are_dropped(self):
        self.green("S1", "S2")
        loop.archive_plan()
        self.assertFalse((self.root / ".runner" / "contracts").exists())
        self.assertFalse((self.root / ".runner" / "freeze").exists())

    def test_the_next_archive_gets_the_next_number(self):
        self.green("S1", "S2")
        loop.archive_plan()
        (self.root / "plan" / "tasks.json").write_text(json.dumps(TWO_STEPS), encoding="utf-8")
        self.green("S1", "S2")
        self.assertIsNone(loop.archive_plan())
        self.assertTrue((self.root / "plan" / "archive" / "002" / "tasks.json").exists())
        self.assertEqual(sorted(p.name for p in (self.root / "plan" / "archive").iterdir()),
                         ["001", "002"])

    def test_a_plan_green_only_in_part_is_not_moved(self):
        self.green("S1")
        refused = loop.archive_plan()
        self.assertIn("S1 already green but S2 not", refused)
        self.assertTrue((self.root / "plan" / "tasks.json").exists())
        self.assertFalse((self.root / "plan" / "archive").exists())

    def test_a_plan_with_no_green_step_is_moved(self):
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "plan")
        self.assertIsNone(loop.archive_plan())
        self.assertTrue((self.root / "plan" / "archive" / "001" / "tasks.json").exists())

    def test_a_dirty_tree_is_not_moved(self):
        self.green("S1", "S2")
        (self.root / "src" / "game.py").write_text("x = 2\n", encoding="utf-8")
        refused = loop.archive_plan()
        self.assertIn("dirty", refused)
        self.assertIn("src/game.py", refused)
        self.assertTrue((self.root / "plan" / "tasks.json").exists())


class BootstrapOverAPlan(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.requirements = root / "REQUIREMENTS.md"
        self.requirements.write_text("make it", encoding="utf-8")
        (root / "plan").mkdir()
        self.planner = mock.Mock(return_value=0)
        for name, value in {
            "PLAN": root / "plan",
            "ledger": lambda event, **f: None,
            "plan_with_retry": self.planner,
            "read_proposal": lambda: {},
            "stamp_language": lambda name: None,
        }.items():
            p = mock.patch.object(loop, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.tasks = root / "plan" / "tasks.json"

    def test_without_a_plan_nothing_is_archived(self):
        with mock.patch.object(loop, "archive_plan") as archive:
            self.assertEqual(loop.cmd_plan_bootstrap(str(self.requirements)), 0)
        archive.assert_not_called()
        self.planner.assert_called_once()

    def test_a_plan_is_archived_before_the_planner_is_asked(self):
        self.tasks.write_text("{}", encoding="utf-8")
        with mock.patch.object(loop, "archive_plan", return_value=None) as archive:
            self.assertEqual(loop.cmd_plan_bootstrap(str(self.requirements)), 0)
        archive.assert_called_once_with()
        self.planner.assert_called_once()

    def test_a_refused_archive_stops_before_the_planner(self):
        self.tasks.write_text("{}", encoding="utf-8")
        with mock.patch.object(loop, "archive_plan", return_value="refusing: no"):
            self.assertEqual(loop.cmd_plan_bootstrap(str(self.requirements)), 1)
        self.planner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
