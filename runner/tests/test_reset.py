"""やり直しと reset が、既存のファイルと柵のディレクトリを失わないこと。

取り込んだ Unity のプロジェクトの S1 で、2つが続けて起きた。TEST_WRITE のやり直しが
既存のコードを消したまま戻さず、柵の外への書き込みとしてエスカレーションした。
その後の reset が、追跡されたファイルの無いテストの柵をディレクトリごと消した。

    python3 -m unittest discover -s runner/tests
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop  # noqa: E402


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout


class Repository(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "runner@example.com")
        git(self.root, "config", "user.name", "runner")
        (self.root / ".gitignore").write_text(".runner/\n", encoding="utf-8")
        self.code = self.root / "Assets" / "Source" / "Gameplay" / "Tactical.cs"
        self.code.parent.mkdir(parents=True)
        self.code.write_text("class Tactical {}\n", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        # テストの柵は作業ツリーにだけあり、追跡されたファイルが無い。
        self.tests = self.root / "Assets" / "Tests" / "Editor" / "Loop"
        self.tests.mkdir(parents=True)

        self.writable = mock.Mock()
        real_run = loop.run
        plan = self.root / "plan"
        for name, value in {
            "PROJECT": self.root, "TESTS": self.tests, "SRC": self.code.parent,
            "PLAN": plan, "LEDGER": plan / "ledger.jsonl", "STATE": self.root / ".runner",
            "ESCALATION": plan / "ESCALATION.md",
            "run": lambda cmd, **kw: real_run(cmd, **{**kw, "cwd": self.root}),
            "set_writable": self.writable,
            "ledger": lambda *a, **k: None,
        }.items():
            p = mock.patch.object(loop, name, value)
            p.start()
            self.addCleanup(p.stop)


class RetryingTestWrite(Repository):
    FILES = ["Assets/Tests/Editor/Loop/TacticalTests.cs", "Assets/Source/Gameplay/Tactical.cs"]

    def test_an_existing_file_comes_back_even_beside_a_new_one(self):
        (self.tests / "TacticalTests.cs").write_text("broken", encoding="utf-8")
        self.code.write_text("class Tactical { stub }\n", encoding="utf-8")
        loop.restore_to_head(self.FILES)
        self.assertEqual(self.code.read_text(encoding="utf-8"), "class Tactical {}\n")
        self.assertFalse((self.tests / "TacticalTests.cs").exists())
        self.assertEqual(loop.touched_paths(), set())

    def test_new_files_alone_are_just_removed(self):
        (self.tests / "TacticalTests.cs").write_text("broken", encoding="utf-8")
        loop.restore_to_head(self.FILES[:1])
        self.assertFalse((self.tests / "TacticalTests.cs").exists())


class Reset(Repository):
    def test_a_fence_without_tracked_files_survives_the_clean(self):
        (self.tests / "TacticalTests.cs").write_text("written by the solver", encoding="utf-8")
        self.code.write_text("class Tactical { half done }\n", encoding="utf-8")
        loop.cmd_reset("S1")
        self.assertTrue(self.tests.is_dir())
        self.assertEqual(list(self.tests.iterdir()), [])
        self.assertEqual(self.code.read_text(encoding="utf-8"), "class Tactical {}\n")
        # 作り直した柵にも、ソルバーが書ける権限を当て直す。
        self.writable.assert_called_with(tests=True, src=True)

    def test_a_fence_already_gone_is_made_again(self):
        self.tests.rmdir()
        loop.cmd_reset("S1")
        self.assertTrue(self.tests.is_dir())


if __name__ == "__main__":
    unittest.main()
