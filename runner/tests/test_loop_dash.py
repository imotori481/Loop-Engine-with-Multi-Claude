"""`loop dash` が、ダッシュボードの要求を確かめてから通し、役の設定だけを読み書きすること。

`loop dash` は root で動き、役の .env は起動スクリプトがシェルで読み込む。通した値が
そのままコマンドやシェルの文になるので、形の確かめがここの要だ。

    python3 -m unittest discover -s runner/tests
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "provision"))

import loop_dash  # noqa: E402

TOKEN = "CLAUDE_CODE_OAUTH_TOKEN=sk-secret"


class Request(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)

    def parse(self, request):
        return loop_dash.parse(request, str(self.work))

    def refused(self, request):
        with self.assertRaises(loop_dash.Refused):
            self.parse(request)

    def test_actions_without_values_pass_as_one_line(self):
        for action in ("settings", "continue", "stop"):
            self.assertEqual(self.parse({"action": action}), [action])

    def test_the_requirements_go_to_a_file_and_never_into_a_line(self):
        text = "# 要件\n$(rm -rf /)\n"
        self.assertEqual(self.parse({"action": "go", "requirements": text, "language": "csharp"}),
                         ["go", "csharp"])
        self.assertEqual((self.work / "requirements.md").read_text(encoding="utf-8"), text)

    def test_a_run_needs_a_known_language_and_some_requirements(self):
        self.refused({"action": "go", "requirements": "x", "language": "cobol"})
        self.refused({"action": "go", "requirements": "  \n", "language": "python"})
        self.refused({"action": "go", "requirements": "x" * (loop_dash.MAX_REQUIREMENTS + 1),
                      "language": "python"})

    def test_a_project_name_follows_the_rule_of_loop_project(self):
        self.assertEqual(self.parse({"action": "use", "project": "game-2"}), ["use", "game-2"])
        for name in ("CURRENT", "../etc", "-x", "Game", "a b", "", None, "a\nb"):
            self.refused({"action": "use", "project": name})

    def test_a_model_change_passes_only_names_that_are_safe_in_a_shell(self):
        self.assertEqual(
            self.parse({"action": "model", "role": "solver", "model": "claude-opus-5-5[1m]",
                        "effort": "high"}),
            ["model", "solver", "claude-opus-5-5[1m]", "high"])
        self.assertEqual(self.parse({"action": "model", "role": "critic", "model": "", "effort": ""}),
                         ["model", "critic", "", ""])
        for model in ("x; rm -rf /", "$(id)", "a b", "-v", "a\nb", "'x'", 5):
            self.refused({"action": "model", "role": "solver", "model": model, "effort": ""})
        self.refused({"action": "model", "role": "solver", "model": "", "effort": "ultra"})
        self.refused({"action": "model", "role": "runner", "model": "", "effort": ""})

    def test_an_import_passes_the_project_the_branch_and_the_fences(self):
        self.assertEqual(self.parse({"action": "init", "project": "game", "branch": "loop/x"}),
                         ["init", "game", "loop/x", "", ""])
        self.assertEqual(
            self.parse({"action": "init", "project": "game", "branch": "feature/a.b_c-1",
                        "src": "Assets/Source", "tests": "Assets/Tests"}),
            ["init", "game", "feature/a.b_c-1", "Assets/Source", "Assets/Tests"])

    def test_an_import_refuses_values_that_could_become_an_option_or_a_line(self):
        for branch in ("", "-x", "a b", "a\nb", "$(id)", None, "a;b"):
            self.refused({"action": "init", "project": "game", "branch": branch})
        self.refused({"action": "init", "project": "CURRENT", "branch": "main"})
        for src, tests in (("../x", ""), ("src", "src/t"), ("a b", ""), ("", 5), ("/abs", "")):
            self.refused({"action": "init", "project": "game", "branch": "main",
                          "src": src, "tests": tests})

    def test_anything_else_is_refused(self):
        self.refused([])
        self.refused({"action": "raw"})
        self.refused({})


class RoleSettings(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.etc = Path(temp.name)
        for role in loop_dash.ROLES:
            self.env(role, "# 資格情報", TOKEN, "", "LOOP_MODEL=", "LOOP_EFFORT=")

    def env(self, role, *lines):
        (self.etc / f"{role}.env").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def read(self, role):
        return (self.etc / f"{role}.env").read_text(encoding="utf-8")

    def test_settings_carry_the_model_and_effort_and_never_the_token(self):
        self.env("planner", TOKEN, 'LOOP_MODEL="claude-opus-5-5"', "LOOP_EFFORT=xhigh")
        (self.etc / "critic.env").unlink()
        value = loop_dash.settings(str(self.etc))
        self.assertEqual(value["roles"]["planner"], {"model": "claude-opus-5-5", "effort": "xhigh"})
        self.assertEqual(value["roles"]["solver"], {"model": "", "effort": ""})
        self.assertIsNone(value["roles"]["critic"])
        self.assertIn("csharp", value["languages"])
        self.assertNotIn("sk-secret", json.dumps(value))

    def test_a_change_rewrites_two_lines_and_keeps_the_rest(self):
        loop_dash.set_model(str(self.etc), "solver", "claude-sonnet-5-5", "medium")
        self.assertEqual(self.read("solver"),
                         "# 資格情報\n" + TOKEN + "\n\nLOOP_MODEL=claude-sonnet-5-5\nLOOP_EFFORT=medium\n")
        self.assertEqual(loop_dash.read_role(str(self.etc / "solver.env")),
                         {"model": "claude-sonnet-5-5", "effort": "medium"})

    def test_missing_lines_are_added(self):
        self.env("critic", TOKEN)
        loop_dash.set_model(str(self.etc), "critic", "", "low")
        self.assertEqual(self.read("critic"), TOKEN + "\nLOOP_MODEL=\nLOOP_EFFORT=low\n")

    @unittest.skipIf(os.name == "nt", "POSIX の権限")
    def test_the_mode_of_the_file_is_kept(self):
        os.chmod(self.etc / "planner.env", 0o640)
        loop_dash.set_model(str(self.etc), "planner", "opus", "")
        self.assertEqual(os.stat(self.etc / "planner.env").st_mode & 0o777, 0o640)

    def test_a_bad_value_writes_nothing(self):
        before = self.read("solver")
        with self.assertRaises(loop_dash.Refused):
            loop_dash.set_model(str(self.etc), "solver", "x;id", "")
        self.assertEqual(self.read("solver"), before)
        self.assertEqual(sorted(p.name for p in self.etc.iterdir()),
                         ["critic.env", "planner.env", "solver.env"])


if __name__ == "__main__":
    unittest.main()
