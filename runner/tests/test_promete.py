"""C# のコードを Promete に持ち込む。

コードもテストも net10.0 で、SDK は 10。コードは凍結したフィードの Promete を参照する。
Promete のコードはファイル単位の namespace と主コンストラクタで書かれるので、既存の
コードを読む側もそれを読めなければならない。Unity の組み合わせは SDK 8 のまま変えない。

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

FEED_VERSIONS = ("NUnit=3.14.0 NUnit3TestAdapter=4.6.0 Microsoft.NET.Test.Sdk=17.11.1 "
                 "JunitXml.TestLogger=4.1.0 Promete=2.1.0")

# Promete のプロジェクトでよく見る形。ファイル単位の namespace、主コンストラクタ、
# nullable の型、式本体のプロパティ。
EXISTING_CS = """using Promete;

namespace Game.Logic;

/// <summary>盤面。</summary>
public class Board(int width, int height)
{
    public int Width { get; } = width;
    public int Height { get; } = height;

    public static int Score(Board board)
    {
        return board.Width * board.Height;
    }

    public Vector Center => new(Width / 2f, Height / 2f);

    public string? Label { get; set; }

    public int Helper(int n) => n + 1;
}
"""


class Promete(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.dict(loop.LANGUAGE, loop.compose_language("csharp", "promete"),
                                  clear=True),
                  mock.patch.dict(loop.LAYOUT, loop.LAYOUT_DEFAULT),
                  mock.patch.object(loop, "UNITY_REFS", Path("/nonexistent/unity-refs"))):
            p.start()
            self.addCleanup(p.stop)


class TheProjectsTheRunnerWrites(Promete):
    def projects(self, framework: str) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as t, \
             mock.patch.dict(loop.LANGUAGE, loop.compose_language("csharp", framework),
                             clear=True):
            temp = Path(t)
            (temp / "dotnet" / "feed").mkdir(parents=True)
            (temp / "dotnet" / "feed" / ".versions").write_text(FEED_VERSIONS, encoding="utf-8")
            with mock.patch.object(loop, "DOTNET_TOOLS", temp / "dotnet"), \
                 mock.patch.object(loop, "DOTNET_BUILD", temp / "dotnet" / "build"):
                return {path.relative_to(temp).as_posix(): text
                        for path, text in loop.dotnet_projects().items()}

    def test_the_code_and_the_tests_are_net10_and_the_code_references_promete(self):
        projects = self.projects("promete")
        code = projects["dotnet/build/code/Code.csproj"]
        tests = projects["dotnet/build/tests/Tests.csproj"]
        self.assertIn("<TargetFramework>net10.0</TargetFramework>", code)
        self.assertIn("<TargetFramework>net10.0</TargetFramework>", tests)
        self.assertIn('<PackageReference Include="Promete" Version="2.1.0" />', code)
        # テストはコードを通して Promete を受け取る。2つの版が食い違う道を作らない。
        self.assertNotIn('Include="Promete"', tests)
        self.assertIn("<LangVersion>14.0</LangVersion>", code)
        # ブリーフはコードがライブラリだと伝えている（CSHARP_LIBRARY_NOTE）。
        self.assertIn("<AssemblyName>LoopCode</AssemblyName>", code)
        self.assertNotIn("OutputType", code)

    def test_imported_code_compiles_with_the_settings_promete_projects_use(self):
        code = self.projects("promete")["dotnet/build/code/Code.csproj"]
        self.assertIn("<ImplicitUsings>enable</ImplicitUsings>", code)
        self.assertIn("<Nullable>enable</Nullable>", code)

    def test_each_framework_pins_its_own_sdk(self):
        # 箱には SDK 8 と 10 があり、何もしなければ 10 が選ばれる。Unity は 8 のまま。
        self.assertIn('"version": "10.0.100"', self.projects("promete")["dotnet/build/global.json"])
        unity = self.projects("unity")
        self.assertIn('"version": "8.0.100"', unity["dotnet/build/global.json"])
        self.assertIn("<TargetFramework>net8.0</TargetFramework>",
                      unity["dotnet/build/tests/Tests.csproj"])
        self.assertNotIn("PackageReference Include=\"Promete\"",
                         unity["dotnet/build/code/Code.csproj"])


PROGRAM_CS = """using Promete;
using Game.Scenes;

var app = PrometeApp.Create()
    .Use<Keyboard>()
    .BuildWithOpenGLDesktop();
return app.Run<MainScene>();
"""


class TheEntryIsLeftOutOfTheBuild(Promete):
    """取り込んだ Promete のプロジェクトの Program.cs はトップレベル文で、ライブラリとして
    組むとコンパイルできない（CS8805）。ビルド全体が落ち、どのテストも走らない。"""

    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "src" / "Logic").mkdir(parents=True)
        (self.root / "src" / "Program.cs").write_text(PROGRAM_CS, encoding="utf-8")
        (self.root / "src" / "Logic" / "Board.cs").write_text(EXISTING_CS, encoding="utf-8")
        # 最上位にあっても文ではないもの。どれもライブラリとしてコンパイルできる。
        (self.root / "src" / "Logic" / "Events.cs").write_text(
            "namespace Game.Logic;\n\npublic delegate void Scored(int points);\n"
            "public sealed record Card(int Rank);\npublic interface IMarker;\n",
            encoding="utf-8")
        (self.root / "src" / "AssemblyInfo.cs").write_text(
            "using System.Runtime.CompilerServices;\n\n"
            "[assembly: InternalsVisibleTo(\"LoopTests\")]\n", encoding="utf-8")
        # Main を持つクラスの Program.cs も、ライブラリとしてコンパイルできる。
        (self.root / "src" / "Logic" / "Launcher.cs").write_text(
            "public class Launcher\n{\n    public static void Main(string[] args) { }\n}\n",
            encoding="utf-8")
        for p in (mock.patch.object(loop, "PROJECT", self.root),
                  mock.patch.object(loop, "SRC", self.root / "src")):
            p.start()
            self.addCleanup(p.stop)

    def test_only_the_file_of_top_level_statements_is_found(self):
        self.assertEqual(loop.csharp_entry_files(), ["src/Program.cs"])

    def test_the_csproj_removes_it(self):
        temp = self.root / "dotnet"
        (temp / "feed").mkdir(parents=True)
        (temp / "feed" / ".versions").write_text(FEED_VERSIONS, encoding="utf-8")
        with mock.patch.object(loop, "DOTNET_TOOLS", temp), \
             mock.patch.object(loop, "DOTNET_BUILD", temp / "build"):
            code = loop.dotnet_projects()[temp / "build" / "code" / "Code.csproj"]
        self.assertIn(f'<Compile Remove="{self.root / "src" / "Program.cs"}" />', code)
        self.assertNotIn("Board.cs", code)

    def test_the_planner_is_told_which_files_are_left_out(self):
        _, text = loop.promete_facts()
        self.assertIn("    src/Program.cs", text)


class DotnetRunsWhereTheGlobalJsonIs(Promete):
    def test_the_tests_run_in_the_build_directory(self):
        # global.json は作業ディレクトリから探される。PROJECT で呼ぶと読まれない。
        seen = {}

        def run(argv, cwd=loop.PROJECT, **kwargs):
            seen["cwd"] = cwd
            raise subprocess.TimeoutExpired(argv, 1)

        with tempfile.TemporaryDirectory() as t, \
             mock.patch.object(loop, "STATE", Path(t)), \
             mock.patch.object(loop, "prepare_dotnet", return_value=None), \
             mock.patch.object(loop, "report_now"), \
             mock.patch.object(loop, "run", side_effect=run):
            loop.pytest_run("red-1", ["tests/BoardTests.cs"])
        self.assertEqual(seen["cwd"], loop.DOTNET_BUILD)


class FileScopedNamespaces(Promete):
    def test_the_rest_of_the_file_is_inside_the_namespace(self):
        lines = loop.csharp_declarations(EXISTING_CS, "src/Logic/Board.cs")
        self.assertIn("src/Logic/Board.cs: class Board(int width, int height) -- namespace Game.Logic",
                      lines)
        self.assertIn("src/Logic/Board.cs: static int Board.Score(Board board) -- namespace Game.Logic",
                      lines)
        self.assertIn("src/Logic/Board.cs: string? Board.Label { get; set; } -- namespace Game.Logic",
                      lines)

    def test_a_namespace_cannot_be_both(self):
        # C# が許さない形。読めたことにして誤った名前空間を渡すより、読めないと返す。
        self.assertIsNone(loop.csharp_outline("namespace A\n{\n    namespace B;\n}\n"))

    def test_the_stub_replaces_bodies_in_a_file_scoped_file(self):
        path = "src/Logic/Board.cs"
        step = {"files_write": [path],
                "contracts": {"provides": [f"{path}: static int Board.Score(Board board)",
                                           f"{path}: Vector Board.Center {{ get; }}",
                                           f"{path}: int Board.Apply(int rule)"]}}
        text = loop.generate_stub(step, [], {path: EXISTING_CS})[path]
        self.assertIn("public static int Score(Board board)\n"
                      '    { throw new System.NotImplementedException("__stub__"); }', text)
        self.assertIn('public Vector Center => throw new System.NotImplementedException("__stub__");',
                      text)
        # 足すメンバーは、ファイル単位の namespace の字下げで型の終わりに置く。
        self.assertIn('    public int Apply(int rule) { throw new System.NotImplementedException("__stub__"); }\n'
                      "}\n", text)
        for kept in ("namespace Game.Logic;", "public int Helper(int n) => n + 1;",
                     "public int Width { get; } = width;"):
            self.assertIn(kept, text)


class WhatThePlannerIsTold(Promete):
    def test_the_headless_backend_and_its_limits_are_stated(self):
        with mock.patch.object(loop, "dotnet_projects", side_effect=OSError("no feed")), \
             mock.patch.object(loop, "existing_declarations", return_value={}), \
             mock.patch("loop.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "10.0.112", "")
            facts = loop.environment_facts()
        self.assertIn("Runtime: .NET 10 for the code and the tests; Promete 2.1.0", facts)
        self.assertIn("BuildWithHeadless()", facts)
        self.assertIn("treat as unavailable", facts)

    def test_the_layout_names_the_version_2_api(self):
        note = loop.LANGUAGE["layout_note"]
        self.assertIn("older examples show version 1", note)
        self.assertNotIn("C# 9 is the ceiling", note)

    def test_the_window_and_the_mouse_are_the_2_1_0_api(self):
        # 2.1.0 の IGameView.Size と MouseButton.IsButtonDown。App.Window は旧形式で
        # ビルドが警告を出し、ボタンを知らないとソルバーは反射で名前を当て推量する。
        note = loop.LANGUAGE["layout_note"]
        self.assertIn("Promete.Backends.IGameView", note)
        self.assertIn("view.Size = (640, 480)", note)
        self.assertNotIn("App.Window.Size", note)
        self.assertIn("mouse[MouseButtonType.Left]", note)

    def test_a_rectangle_takes_two_corners(self):
        # 幅と高さを渡すと、ボタンが大きい矩形1つに重なって描かれた。
        note = loop.LANGUAGE["layout_note"]
        self.assertIn("Shape.CreateRect(540, 300, 740, 356, color)", note)

    def test_the_entry_runs_the_scene_of_the_run(self):
        # テンプレの Program.cs のままだと、同じファイルの MainScene が起動し、
        # Mouse も登録されない。
        note = loop.LANGUAGE["layout_note"]
        self.assertIn("deletes the template's scene", note)
        self.assertIn(".Use<Mouse>()", note)

    def test_the_entry_cannot_be_tested(self):
        # ランナーはコードをライブラリとして組む。入口を確かめる条件はビルドで通らない。
        for framework in ("promete", "unity"):
            note = loop.compose_language("csharp", framework)["layout_note"]
            self.assertIn("`Assembly.EntryPoint` is always null", note)


class TheLibraryReference(Promete):
    """ソルバーはマウスのボタンの名前を知らず、反射で当て推量した。プランナーには型の
    名前を、ソルバーには置き場を渡し、メンバーは実物から引かせる。"""

    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        refs = Path(temp.name)
        (refs / "promete").mkdir()
        (refs / "promete" / "MEMBERS.txt").write_text(
            "P:Promete.Input.MouseButton.IsButtonDown  押された\n", encoding="utf-8")
        (refs / "promete" / "TYPES.txt").write_text(
            "Promete.Input.Mouse  マウス\nPromete.Input.MouseButton  ボタン\n"
            "Promete.Scene  シーン\n", encoding="utf-8")
        p = mock.patch.object(loop, "REFS", refs)
        p.start()
        self.addCleanup(p.stop)
        self.refs = refs

    def test_the_planner_gets_the_type_names_by_namespace(self):
        self.assertEqual(loop.library_types(),
                         "    Promete: Scene\n    Promete.Input: Mouse, MouseButton")
        _, text = loop.promete_facts()
        self.assertIn("    Promete.Input: Mouse, MouseButton", text)

    def test_the_solver_is_told_where_to_look(self):
        step = {"id": "S1", "depends_on": [],
                "contracts": {"provides": [], "requires": [], "invariants": []}}
        with mock.patch.object(loop, "existing_requirements", return_value=[]):
            brief = loop.solver_material(step, "CONTEXT")
        self.assertIn(f"are in {self.refs / 'promete'} (MEMBERS.txt, TYPES.txt)", brief)
        self.assertIn("Grep `MEMBERS.txt`", brief)

    def test_a_framework_without_a_reference_says_nothing(self):
        with mock.patch.dict(loop.LANGUAGE, loop.compose_language("csharp", "unity"), clear=True):
            self.assertIsNone(loop.library_refs())
            self.assertEqual(loop.library_refs_section(), "")


class ChoosingPromete(unittest.TestCase):
    def test_a_plan_can_ask_for_it(self):
        saved = dict(loop.LANGUAGE)
        self.addCleanup(lambda: (loop.LANGUAGE.clear(), loop.LANGUAGE.update(saved)))
        loop.load_settings({"language": "csharp", "framework": "promete"})
        self.assertEqual(loop.LANGUAGE["dotnet_target"], "net10.0")


if __name__ == "__main__":
    unittest.main()
