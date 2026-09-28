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

    def test_the_dev_server_is_named_as_the_environment_s(self):
        # run 8 の S10 は、Vite が index.html を変換した結果を確かめる条件を持ち、
        # どの実装でも通らずに時間切れまで考えた。
        facts = self.facts("typescript")
        self.assertIn("development server (Vite, for example) is also the", facts)
        self.assertIn("Do not write criteria that start a development server", facts)
        self.assertNotIn("development server", self.facts("python"))

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


PYTHON_MODULE = '''
"""モジュールの説明。"""
from dataclasses import dataclass

LIMIT: int = 10
NAMES = ("a", "b")
_cache = {}


@dataclass(frozen=True)
class GameState(Base):
    resource: float
    _secret: int = 0

    def buy(self, name: str, count: int = 1) -> "GameState":
        return self

    def _helper(self):
        pass


def advance(state: GameState, dt: float) -> GameState:
    return state


async def load(path: str) -> dict[str, int]:
    return {}


def _private() -> None:
    pass
'''

TS_MODULE = '''
import { Other } from "./other.ts";

// export function commented(): void { }
/* export const hidden = 1; */
export interface Generator {
  id: string;   // { not a brace that counts }
  cost: number;
}

export type Id = "a" | "b";

export const CATALOG: Record<Id, Generator> = {
  a: { id: "a", cost: 1 },
  b: { id: "b", cost: 2 },
};

export const double = (n: number): number => n * 2;

export function split(
  text: string,
  sep = "}",
): { head: string; rest: string[] } {
  const quote = /'/g;
  return { head: text.replace(quote, ""), rest: [] };
}

function internal(): void {}

export class Engine extends Base {
  private secret = 1;
  #hidden = 2;
  readonly speed: number = 3;
  constructor(public name: string) {
    super();
  }
  tick(dt: number): void {
    if (dt > 0) { this.speed; }
  }
  protected guard(): boolean { return true; }
}

export { Other } from "./other.ts";
'''


class WhatTheCodeAlreadyDeclares(unittest.TestCase):
    """取り込んだリポジトリの宣言を、署名だけでプランナーに渡す。

    プランナーはコードを読めない（BOOTSTRAP 1-1）。署名が無ければ、既存の関数も
    型も知らずに計画を書く。本体を渡すと、条件が要件ではなく実装を述べる。
    """

    def test_python_gives_public_signatures_with_their_module(self):
        lines = loop.python_declarations(PYTHON_MODULE, "incgame.engine")
        where = " -- defined in incgame.engine"
        self.assertEqual(lines, [
            "LIMIT: int" + where,
            "NAMES" + where,
            "@dataclass(frozen=True) class GameState(Base): resource: float; "
            "def buy(self, name: str, count: int=1) -> 'GameState'" + where,
            "def advance(state: GameState, dt: float) -> GameState" + where,
            "async def load(path: str) -> dict[str, int]" + where,
        ])

    def test_python_class_lines_declare_the_class_name(self):
        # 次の作業で、L3 はこの行を declared_name で読む。
        line = loop.python_declarations(PYTHON_MODULE, "m")[2]
        self.assertEqual(loop.declared_name(line), "GameState")

    def test_python_that_does_not_parse_gives_nothing(self):
        self.assertEqual(loop.python_declarations("def broken(:\n", "m"), [])

    def test_typescript_gives_exports_without_bodies(self):
        lines = loop.ts_declarations(TS_MODULE, "src/idle/engine.ts")
        at = "src/idle/engine.ts: "
        self.assertEqual(lines, [
            at + "interface Generator { id: string; cost: number; }",
            at + 'type Id = "a" | "b"',
            at + "const CATALOG: Record<Id, Generator>",
            at + "const double = (n: number): number =>",
            at + 'function split( text: string, sep = "}", ): '
                 "{ head: string; rest: string[] }",
            at + "class Engine extends Base { readonly speed: number; "
                 "constructor(public name: string); tick(dt: number): void }",
        ])

    def test_typescript_functions_match_the_contract_form(self):
        # スタブを書く generate_stub は、この形の行だけを読める。
        line = loop.ts_declarations(
            "export function add(a: number, b: number): number {\n  return a + b;\n}\n",
            "src/m.ts")[0]
        match = loop.TS_DECLARATION.match(line)
        self.assertIsNotNone(match)
        self.assertEqual(match.group("name"), "add")

    def test_typescript_without_semicolons_still_splits(self):
        lines = loop.ts_declarations(
            "export const A = 1\nexport const B: string = 'x'\n", "src/m.ts")
        self.assertEqual(lines, ["src/m.ts: const A", "src/m.ts: const B: string"])

    def git(self, files: dict[str, str], listed: str | None = None):
        def run(cmd, **_):
            if cmd[1] == "ls-tree":
                return SimpleNamespace(returncode=0, stderr="",
                                       stdout=listed if listed is not None
                                       else "\0".join(files) + "\0")
            path = cmd[2].split(":", 1)[1]
            return SimpleNamespace(returncode=0, stdout=files[path], stderr="")
        return patch("loop.run", side_effect=run)

    def test_only_source_files_of_the_plan_s_language_are_read(self):
        files = {"src/incgame/engine.py": "def f(x: int) -> int:\n    return x\n",
                 "src/idle/engine.ts": "export const X = 1;\n",
                 "src/incgame/data.json": "{}"}
        with self.git(files), \
             patch.dict(loop.LANGUAGE, loop.LANGUAGES["python"], clear=True):
            self.assertEqual(loop.existing_contracts(),
                             ["def f(x: int) -> int -- defined in incgame.engine"])
        with self.git(files), \
             patch.dict(loop.LANGUAGE, loop.LANGUAGES["typescript"], clear=True):
            self.assertEqual(loop.existing_contracts(),
                             ["src/idle/engine.ts: const X"])

    def test_no_commit_means_no_declarations(self):
        with patch("loop.run", return_value=SimpleNamespace(
                returncode=128, stdout="", stderr="fatal: not a valid object name HEAD")):
            self.assertEqual(loop.existing_contracts(), [])

    def test_no_working_tree_means_no_declarations(self):
        with patch("loop.run", side_effect=FileNotFoundError):
            self.assertEqual(loop.existing_contracts(), [])

    def facts(self, existing: list[str]) -> str:
        by_file = {"src/pkg/mod.py": existing} if existing else {}
        with patch.object(loop, "existing_declarations", return_value=by_file), \
             patch.dict(os.environ, {"DISPLAY": ""}), \
             patch("loop.run") as run:
            run.return_value = SimpleNamespace(
                returncode=0, stdout="Python 3.12.3", stderr="")
            return loop.environment_facts()

    def test_the_planner_is_given_the_signatures_and_told_why_not_the_bodies(self):
        facts = self.facts(["def f(x: int) -> int -- defined in pkg.mod"])
        self.assertIn("# What the code already declares", facts)
        self.assertIn("    def f(x: int) -> int -- defined in pkg.mod", facts)
        self.assertIn("bodies are left out on purpose", facts)
        # requires にどう書くかと、書き換えるステップがあるときの依存も伝える。
        self.assertIn("puts that line in `contracts.requires`", facts)
        self.assertIn("depends on that step instead", facts)

    def test_the_planner_is_told_criteria_about_unchanged_code_stop_at_r4(self):
        # スタブが替えるのは provides の名前だけで、ほかの宣言は本物のまま動く。
        # 書き換えない関数で満たされる条件は、スタブに対して通って R4 で止まる。
        facts = self.facts(["def f(x: int) -> int -- defined in pkg.mod"])
        self.assertIn("keeps its real, working code", facts)
        self.assertIn("RED_GATE stops the step (R4)", facts)
        self.assertIn("calls a name this step provides", facts)
        # 変えない振る舞いは、条件ではなく既存のテストが守る。
        self.assertIn("Do not write criteria to show that the rest still works", facts)
        self.assertIn("The tests already in\nthe repository run on every step", facts)

    def test_a_new_project_is_not_told_about_code_it_does_not_have(self):
        self.assertNotIn("What the code already declares", self.facts([]))


class TheSolverIsShownTheFilesItWillEdit(unittest.TestCase):
    """既存のファイルを書き換える IMPL は、ファイルを Read するたびにそれまでの
    文脈を送り直す。取り込んだ Unity のプロジェクトの回で、ソルバーの消費の63%が
    そのターンだった。今の中身をブリーフに載せる。
    """

    STEP = {"id": "S1", "goal": "g", "files_write": ["src/a.py", "src/b.py", "src/new.py"],
            "contracts": {"provides": [], "requires": [], "invariants": []}}

    def section(self, files: dict[str, str], cap: int = 60_000) -> str:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            for rel, text in files.items():
                (project / rel).parent.mkdir(parents=True, exist_ok=True)
                (project / rel).write_text(text, encoding="utf-8")
            with patch.object(loop, "PROJECT", project), \
                 patch.object(loop, "IMPL_FILE_CHARS", cap):
                return loop.current_files_section(self.STEP)

    def test_existing_files_are_shown_and_new_ones_are_not(self):
        section = self.section({"src/a.py": "def a(): pass\n",
                                "src/b.py": "def b(): pass\n"})
        self.assertIn("--- src/a.py ---\ndef a(): pass", section)
        self.assertIn("--- src/b.py ---\ndef b(): pass", section)
        self.assertNotIn("src/new.py", section)

    def test_the_solver_reads_one_line_instead_of_the_whole_file(self):
        # Claude Code の Edit は、同じ呼び出しで Read していないファイルを拒む。
        # 1行だけの Read でも通る。
        section = self.section({"src/a.py": "x = 1\n"})
        self.assertIn("you do not need to read them again", section)
        self.assertIn("(offset 1, limit 1)", section)

    def test_files_over_the_cap_are_named_but_not_shown(self):
        section = self.section({"src/a.py": "a" * 30, "src/b.py": "b" * 30}, cap=40)
        self.assertIn("a" * 30, section)
        self.assertNotIn("b" * 30, section)
        self.assertIn("read these yourself:\nsrc/b.py", section)

    def test_a_step_that_only_creates_files_gets_no_section(self):
        self.assertEqual(self.section({}), "")

    def test_the_files_come_after_the_tests_and_before_the_failure(self):
        # 失敗の文と今の中身は試行ごとに変わる。変わらない部分を先に置けば、
        # 次の試行はそこをプロンプトキャッシュから読む。
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "src").mkdir()
            (project / "src/a.py").write_text("CURRENT\n", encoding="utf-8")
            with patch.object(loop, "PROJECT", project), \
                 patch("loop.dep_contracts", return_value=""):
                brief = loop.brief_impl(self.STEP, "CONTEXT", "TESTS", "FAILURE")
        self.assertLess(brief.index("TESTS"), brief.index("CURRENT"))
        self.assertLess(brief.index("CURRENT"), brief.index("FAILURE"))


class TheSolverBriefsShareTheirOpening(unittest.TestCase):
    """TEST_WRITE と IMPL は、CONTEXT.md、契約、不変条件、署名を共有する。先頭を
    そろえれば、後の呼び出しはそこをプロンプトキャッシュから読む。
    """

    STEP = {"id": "S1", "goal": "GOAL-TEXT", "expected_tests": 1,
            "files_write": ["src/a.py"], "files_test": ["tests/test_a.py"],
            "acceptance": [{"case": "c", "given": "g", "then": "t"}],
            "contracts": {"provides": ["def f(x: int) -> int"], "requires": [],
                          "invariants": ["f is pure"]}}

    def briefs(self) -> tuple[str, str, str]:
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(loop, "PROJECT", Path(temp)), \
             patch("loop.dep_contracts", return_value="DEPS"):
            return (loop.solver_material(self.STEP, "CONTEXT"),
                    loop.brief_test_write(self.STEP, "CONTEXT"),
                    loop.brief_impl(self.STEP, "CONTEXT", "TESTS", "FAILURE"))

    def test_both_phases_open_with_the_same_material(self):
        shared, test_write, impl = self.briefs()
        self.assertTrue(test_write.startswith(shared))
        self.assertTrue(impl.startswith(shared))
        for part in ("CONTEXT", "DEPS", "f is pure", "def f(x: int) -> int"):
            self.assertIn(part, shared)

    def test_a_replaced_test_file_is_rewritten_from_scratch(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(loop, "PROJECT", Path(temp)), \
             patch("loop.dep_contracts", return_value="DEPS"):
            plain = loop.brief_test_write(self.STEP, "CONTEXT")
            replacing = loop.brief_test_write(self.STEP, "CONTEXT",
                                              replaced=["tests/test_a.py"])
        self.assertNotIn("you replace them", plain)
        self.assertIn("you replace them\ntests/test_a.py", replacing)
        self.assertIn("Do not keep, adapt or\ncopy the old tests", replacing)
        self.assertIn("(offset 1,\nlimit 1)", replacing)

    def test_the_goal_stays_out_of_the_tests_brief(self):
        # テストは受け入れ条件から作る。goal を共通の部分に入れると、TEST_WRITE
        # にも届く。
        shared, test_write, impl = self.briefs()
        self.assertNotIn("GOAL-TEXT", shared)
        self.assertNotIn("GOAL-TEXT", test_write)
        self.assertIn("GOAL-TEXT", impl)


class TestsAlreadyInTheRepository(unittest.TestCase):
    """要件が振る舞いを変えると、それを確かめる既存のテストはどんな実装でも落ちる。
    そのファイルは、振る舞いを変えるステップが files_test に挙げて差し替える。
    """

    FILES = {
        "tests/test_board.py": "import pytest\n\ndef test_score_counts_rows():\n"
                               "    pass\n\nasync def test_async_load():\n    pass\n",
        "tests/__init__.py": "",
    }

    def run_git(self, argv, **_):
        if argv[:2] == ["git", "ls-tree"]:
            return SimpleNamespace(returncode=0, stdout="\0".join(self.FILES), stderr="")
        if argv[:2] == ["git", "show"]:
            path = argv[2].split(":", 1)[1]
            return SimpleNamespace(returncode=0, stdout=self.FILES[path], stderr="")
        raise AssertionError(argv)

    def text(self, plan):
        with patch("loop.run", side_effect=self.run_git), \
             patch.dict(loop.LANGUAGE, loop.LANGUAGES["python"], clear=True):
            return loop.existing_tests_text(plan)

    def test_the_planner_sees_each_file_with_its_test_names(self):
        text = self.text(None)
        self.assertIn("    tests/test_board.py\n        test_score_counts_rows\n"
                      "        test_async_load", text)
        self.assertNotIn("__init__", text)
        self.assertIn("REPLACES the whole file", text)
        self.assertIn("(L17)", text)

    def test_the_critic_sees_only_the_files_the_plan_replaces(self):
        keeps = {"steps": [{"files_test": ["tests/test_new.py"]}]}
        replaces = {"steps": [{"files_test": ["tests/test_board.py"]}]}
        self.assertEqual(self.text(keeps), "")
        self.assertIn("Test files this plan replaces", self.text(replaces))

    def test_the_revision_is_not_offered_the_tests_of_a_green_step(self):
        tasks = ('{"steps": [{"id": "S1", "files_test": ["tests/test_board.py"]}]}')
        with patch("loop.run", side_effect=self.run_git), \
             patch.dict(loop.LANGUAGE, loop.LANGUAGES["python"], clear=True):
            self.assertEqual(loop.preplan_tests_section(tasks, ["S1"]), "")
            offered = loop.preplan_tests_section(tasks, [])
        self.assertIn("tests/test_board.py", offered)
        self.assertIn("do not replace the file", offered)


class TestNamesByLanguage(unittest.TestCase):
    def names(self, language, text):
        with patch.dict(loop.LANGUAGE, loop.LANGUAGES[language], clear=True):
            return loop.test_names(text)

    def test_typescript_names_come_from_it_and_test(self):
        self.assertEqual(self.names("typescript", (
            "describe('board', () => {\n"
            "  it('counts rows', () => {});\n"
            "  test.each([1])(\"doubles %i\", () => {});\n"
            "  it(`keeps the player's score`, () => {});\n"
            "});\n")), ["counts rows", "doubles %i", "keeps the player's score"])

    def test_csharp_names_come_from_test_attributes_once_each(self):
        self.assertEqual(self.names("csharp", (
            "public class BoardTests {\n"
            "    [Test]\n    public void CountsRows() {}\n"
            "    [TestCase(1)]\n    [TestCase(2)]\n    public void Doubles(int n) {}\n"
            "    [UnityTest]\n    public IEnumerator Loads() { yield break; }\n"
            "    public void Helper() {}\n"
            "}\n")), ["CountsRows", "Doubles", "Loads"])


if __name__ == "__main__":
    unittest.main()
