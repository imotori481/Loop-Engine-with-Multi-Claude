"""What the planner is told about the machine it is planning for.

environment_facts gathers rather than states, because a written-down fact rots
and a gathered one cannot. The display is the case where that matters most: a
criterion like "the window opens" becomes a test that raises TclError, which is
an honest red -- RED_GATE passes it -- and which no implementation can ever turn
green. The step burns every attempt of every tier, then an escalation, and the
cause appears in nothing the solver or the planner can see.

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


class ThePageBelongsToTypeScript(unittest.TestCase):
    """35-node.sh は言語に関係なく index.html と vitest.config.mjs を置く。

    Python の計画にそれを見せると、クリティックは「人が開く index.html から
    コードに届かない」と指摘し、プランナーはそれに答えられない。
    """

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

    def test_a_python_plan_is_not_told_about_the_page(self):
        facts = self.facts("python")
        self.assertNotIn("index.html", facts)
        self.assertNotIn("vitest.config.mjs", facts)

    def test_a_typescript_plan_is_told_the_page_is_already_wired(self):
        facts = self.facts("typescript")
        self.assertIn("It is what a person opens", facts)
        self.assertIn('import { start } from "/src/main.ts"', facts)


if __name__ == "__main__":
    unittest.main()
