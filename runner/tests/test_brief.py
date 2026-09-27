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
        with patch.object(loop, "existing_contracts", return_value=existing), \
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

    def test_a_new_project_is_not_told_about_code_it_does_not_have(self):
        self.assertNotIn("What the code already declares", self.facts([]))


if __name__ == "__main__":
    unittest.main()
