"""プランナーが、計画を立てる相手の機械について何を伝えられるか。

environment_facts は書き写さずに集める。書き写した事実は古くなり、集めた事実は
古くならないからだ。それが最も効くのは画面だ。「窓が開く」のような条件は
TclError を投げるテストになる。それは正直な赤で、RED_GATE を通り、どの実装も
緑にできない。ステップはすべての段の試行を使い、エスカレーションまで使い、
その原因はソルバーにもプランナーにも見えるところに無い。

    python3 -m unittest discover -s runner/tests
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop  # noqa: E402


class WhatTheMachineLooksLike(unittest.TestCase):
    def facts(self, display: str, tk_ok: bool = True) -> str:
        with patch.dict(os.environ, {"DISPLAY": display}), patch("loop.run") as run:
            run.return_value = SimpleNamespace(
                returncode=0 if tk_ok else 1, stdout="Python 3.12.3", stderr="")
            return loop.environment_facts()

    def test_no_display_is_stated_together_with_what_to_do_about_it(self):
        facts = self.facts(display="")
        self.assertIn("NONE. DISPLAY is not set", facts)
        self.assertIn("checkable by pytest with no display", facts)

    def test_a_machine_that_does_have_a_screen_says_so_and_drops_the_warning(self):
        facts = self.facts(display=":0")
        self.assertIn("DISPLAY=:0", facts)
        self.assertNotIn("checkable by pytest with no display", facts)

    def test_a_missing_toolkit_is_reported_rather_than_assumed(self):
        self.assertIn("tkinter does NOT import", self.facts(display="", tk_ok=False))
        self.assertIn("tkinter imports", self.facts(display="", tk_ok=True))


class RootFiles:
    """35-node.sh と同じく、根に index.html と vitest.config.mjs を置いた箱。"""

    def facts(self, language: str) -> str:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "src").mkdir()
            (project / "index.html").write_text(
                '<script type="module">import { start } from "/src/main.ts";</script>',
                encoding="utf-8")
            (project / "vitest.config.mjs").write_text("export default {};",
                                                       encoding="utf-8")
            with patch.object(loop, "PROJECT", project), \
                 patch.dict(loop.LANGUAGE, loop.LANGUAGES[language], clear=True), \
                 patch.dict(os.environ, {"DISPLAY": ""}), \
                 patch("loop.run") as run:
                run.return_value = SimpleNamespace(
                    returncode=0, stdout="v22.23.3", stderr="")
                return loop.environment_facts()


class ThePageBelongsToTypeScript(RootFiles, unittest.TestCase):
    """35-node.sh は言語に関係なく index.html と vitest.config.mjs を置く。

    Python の計画にそれを見せると、クリティックは「人が開く index.html から
    コードに届かない」と指摘し、プランナーはそれに答えられない。
    """

    def test_a_python_plan_is_not_told_about_the_page(self):
        facts = self.facts("python")
        self.assertNotIn("index.html", facts)
        self.assertNotIn("vitest.config.mjs", facts)

    def test_a_typescript_plan_is_told_the_page_is_already_wired(self):
        facts = self.facts("typescript")
        self.assertIn("It is what a person opens", facts)
        self.assertIn('import { start } from "/src/main.ts"', facts)


class TheRootBelongsToTheEnvironment(RootFiles, unittest.TestCase):
    """根のファイルは最初のステップより前に完成している。

    run 8 の S10 は index.html の中身を確かめる条件を持っていた。スタブの時点で
    通るので R4 で必ず止まり、プランナーは人間に差し戻した。
    """

    def test_the_files_on_disk_are_named_and_the_trap_is_explained(self):
        facts = self.facts("typescript")
        self.assertIn("belong to the environment: index.html, vitest.config.mjs", facts)
        self.assertIn("already true against the stub", facts)
        self.assertIn("(R4)", facts)

    def test_the_page_gets_an_example_of_what_to_test_instead(self):
        self.assertIn("call `start` on an element", self.facts("typescript"))

    def test_a_python_plan_is_not_told_about_files_it_is_not_shown(self):
        facts = self.facts("python")
        self.assertNotIn("belong to the environment", facts)
        self.assertNotIn("call `start`", facts)


class WhatTheSolverCanDo(unittest.TestCase):
    """テストのコマンドだけを見せると、プランナーは「走らせて確かめろ」と書く。"""

    def facts(self, tiers: list[str]) -> str:
        with patch.object(loop, "SOLVER_TIERS", tiers), \
             patch.dict(os.environ, {"DISPLAY": ""}), \
             patch("loop.run") as run:
            run.return_value = SimpleNamespace(
                returncode=0, stdout="Python 3.12.3", stderr="")
            return loop.environment_facts()

    def test_a_solver_without_commands_is_described_as_one(self):
        for tiers in (["claude"], ["local"], ["local", "claude"]):
            with self.subTest(tiers=tiers):
                self.assertIn("The solver cannot run commands", self.facts(tiers))

    def test_a_tier_that_can_run_commands_drops_the_warning(self):
        self.assertNotIn("The solver cannot run commands",
                         self.facts(["claude", "codex"]))


if __name__ == "__main__":
    unittest.main()
