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
        self.assertIn("version 1 API", loop.LANGUAGE["layout_note"])
        self.assertNotIn("C# 9 is the ceiling", loop.LANGUAGE["layout_note"])


class ChoosingPromete(unittest.TestCase):
    def test_a_plan_can_ask_for_it(self):
        saved = dict(loop.LANGUAGE)
        self.addCleanup(lambda: (loop.LANGUAGE.clear(), loop.LANGUAGE.update(saved)))
        loop.load_settings({"language": "csharp", "framework": "promete"})
        self.assertEqual(loop.LANGUAGE["dotnet_target"], "net10.0")


if __name__ == "__main__":
    unittest.main()
