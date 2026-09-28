"""C# を計画の言語にする。

コードは netstandard2.1、テストは net8.0 の NUnit 3。csproj はランナーが
/srv/loop/dotnet/build に書く。取り込んだ Unity のプロジェクトでは、Unity の
参照アセンブリでコンパイルし、エンジン本体の無い .NET でテストを走らせる。
ここの失敗の形とエンジンの制約は、箱の smoke-dotnet と probe-unity の実測に
合わせてある。

    python3 -m unittest discover -s runner/tests
"""

import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop  # noqa: E402

FEED_VERSIONS = ("NUnit=3.14.0 NUnit3TestAdapter=4.6.0 Microsoft.NET.Test.Sdk=17.11.1 "
                 "JunitXml.TestLogger=4.1.0")


class CSharp(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(loop.LANGUAGE, loop.LANGUAGES["csharp"], clear=True)
        p.start()
        self.addCleanup(p.stop)

    def failure(self, message):
        return ET.fromstring(f'<failure type="failure" message="{message}" />')


class HowAFailureIsRead(CSharp):
    def test_an_assertion_is_named_from_expected_and_but_was(self):
        # smoke-dotnet の実測: type は常に "failure" で、型を持たない。
        kind = loop.failure_kind(self.failure("  Expected: 6&#10;  But was:  5&#10;"))
        self.assertEqual(kind, "AssertionException")
        self.assertTrue(loop.LANGUAGE["red_kinds"].match(kind))

    def test_an_exception_is_named_from_the_front_of_the_message(self):
        # probe-unity の実測。エンジンに降りる呼び出しは、こう落ちる。
        kind = loop.failure_kind(self.failure(
            "System.Security.SecurityException : ECall methods must be packaged "
            "into a system module."))
        self.assertEqual(kind, "System.Security.SecurityException")
        self.assertFalse(loop.LANGUAGE["red_kinds"].match(kind))

    def test_a_wrong_exception_under_assert_throws_is_an_assertion(self):
        kind = loop.failure_kind(self.failure(
            "  Expected: &lt;System.ArgumentException&gt;&#10;  But was:  "
            "&lt;System.NotImplementedException: __stub__&gt;"))
        self.assertEqual(kind, "AssertionException")

    def test_anything_else_is_not_guessed(self):
        kind = loop.failure_kind(self.failure("the board was wrong"))
        self.assertEqual(kind, "the board was wrong")
        self.assertFalse(loop.LANGUAGE["red_kinds"].match(kind))


class TheCommand(CSharp):
    def test_a_step_s_tests_are_selected_by_class_name(self):
        argv, env = loop.test_argv(["tests/BoardTests.cs", "tests/Rules/ScoreTests.cs"],
                                   Path("/srv/loop/project/.runner/pytest-red-1.xml"))
        self.assertEqual(argv[:2], ["/usr/bin/dotnet", "test"])
        self.assertIn("--no-restore", argv)
        filter_ = argv[argv.index("--filter") + 1]
        self.assertEqual(filter_, "FullyQualifiedName~.BoardTests.|FullyQualifiedName~.ScoreTests.")
        self.assertIn("junit;LogFilePath=/srv/loop/project/.runner/pytest-red-1.xml", argv)
        self.assertEqual(env["DOTNET_CLI_UI_LANGUAGE"], "en")

    def test_the_whole_suite_has_no_filter(self):
        argv, _ = loop.test_argv(["tests"], Path("/tmp/r.xml"))
        self.assertNotIn("--filter", argv)

    def test_results_are_not_written_into_the_working_tree(self):
        # 既定の TestResults/ は作業ツリーにでき、assert_touched に引っかかる。
        argv, _ = loop.test_argv(["tests/BoardTests.cs"],
                                 Path("/srv/loop/project/.runner/pytest-verify-1.xml"))
        where = argv[argv.index("--results-directory") + 1].replace("\\", "/")
        self.assertTrue(where.startswith("/srv/loop/project/.runner/"), where)


class TheProjectsTheRunnerWrites(CSharp):
    def tools(self, temp: Path, unity: bool):
        (temp / "dotnet" / "feed").mkdir(parents=True)
        (temp / "dotnet" / "feed" / ".versions").write_text(FEED_VERSIONS, encoding="utf-8")
        refs = temp / "unity-refs"
        if unity:
            (refs / "refs").mkdir(parents=True)
            (refs / "refs" / "UnityEngine.CoreModule.dll").write_bytes(b"MZ")
            (refs / "langversion.txt").write_text("9.0\n", encoding="utf-8")
            (refs / "defines.txt").write_text("UNITY_6000_3_OR_NEWER\nENABLE_INPUT_SYSTEM\n",
                                              encoding="utf-8")
            (refs / "version.txt").write_text("6000.3.10f1\n", encoding="utf-8")
        return [mock.patch.object(loop, "DOTNET_TOOLS", temp / "dotnet"),
                mock.patch.object(loop, "DOTNET_BUILD", temp / "dotnet" / "build"),
                mock.patch.object(loop, "UNITY_REFS", refs),
                mock.patch.object(loop, "SRC", Path("/srv/loop/project/Assets/Source")),
                mock.patch.object(loop, "TESTS", Path("/srv/loop/project/Assets/Tests/Editor/Loop"))]

    def projects(self, unity: bool):
        with tempfile.TemporaryDirectory() as t:
            temp = Path(t)
            patches = self.tools(temp, unity)
            for p in patches:
                p.start()
            try:
                return {path.relative_to(temp).as_posix(): text
                        for path, text in loop.dotnet_projects().items()}
            finally:
                for p in patches:
                    p.stop()

    def test_with_unity_the_code_compiles_against_its_assemblies(self):
        projects = self.projects(unity=True)
        code = projects["dotnet/build/code/Code.csproj"]
        tests = projects["dotnet/build/tests/Tests.csproj"]
        self.assertIn("<TargetFramework>netstandard2.1</TargetFramework>", code)
        self.assertIn('<Compile Include="/srv/loop/project/Assets/Source/**/*.cs" />',
                      code.replace("\\", "/"))
        self.assertIn(";UNITY_6000_3_OR_NEWER;ENABLE_INPUT_SYSTEM</DefineConstants>", code)
        # コンパイルにだけ使う。テストは実行のために出力へ写す。
        self.assertIn("<Private>false</Private>", code)
        self.assertIn("<Private>true</Private>", tests)
        self.assertIn('<PackageReference Include="NUnit" Version="3.14.0" />', tests)
        self.assertIn('<ProjectReference Include="../code/Code.csproj" />', tests)
        self.assertIn('<Compile Include="/srv/loop/project/Assets/Tests/Editor/Loop/**/*.cs" />',
                      tests.replace("\\", "/"))

    def test_without_unity_there_are_no_references(self):
        code = self.projects(unity=False)["dotnet/build/code/Code.csproj"]
        self.assertNotIn("<Reference ", code)
        self.assertIn("<LangVersion>9.0</LangVersion>", code)

    def test_restore_runs_only_when_a_project_changes(self):
        with tempfile.TemporaryDirectory() as t:
            temp = Path(t)
            patches = self.tools(temp, unity=True)
            for p in patches:
                p.start()
            self.addCleanup(lambda: [p.stop() for p in patches])

            def restored(cmd, **_):
                assets = temp / "dotnet" / "build" / "tests" / "obj" / "project.assets.json"
                assets.parent.mkdir(parents=True, exist_ok=True)
                assets.write_text("{}", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch("loop.run", side_effect=restored) as run:
                self.assertIsNone(loop.prepare_dotnet())
                self.assertIsNone(loop.prepare_dotnet())
            self.assertEqual(run.call_count, 1)
            self.assertIn("--configfile", run.call_args.args[0])

    def test_a_failed_restore_is_reported(self):
        with tempfile.TemporaryDirectory() as t:
            patches = self.tools(Path(t), unity=False)
            for p in patches:
                p.start()
            self.addCleanup(lambda: [p.stop() for p in patches])
            with mock.patch("loop.run", return_value=SimpleNamespace(
                    returncode=1, stdout="error NU1101: Unable to find package NUnit", stderr="")):
                self.assertIn("NU1101", loop.prepare_dotnet())


class WhenTheBuildFails(CSharp):
    """C# は1つのファイルが壊れると、テストのアセンブリ全体がビルドできない。"""

    def setUp(self):
        super().setUp()
        for name, value in {"PROJECT": Path("/srv/loop/project")}.items():
            p = mock.patch.object(loop, name, value)
            p.start()
            self.addCleanup(p.stop)

    OUTPUT = """  Determining projects to restore...
/srv/loop/project/tests/BoardTests.cs(12,9): error CS0103: The name 'Boad' does not exist in the current context [/srv/loop/dotnet/build/tests/Tests.csproj]
/srv/loop/project/tests/BoardTests.cs(12,9): error CS0103: The name 'Boad' does not exist in the current context [/srv/loop/dotnet/build/tests/Tests.csproj]
/srv/loop/project/src/Logic/Board.cs(3,5): error CS1002: ; expected [/srv/loop/dotnet/build/code/Code.csproj]
"""

    def test_a_test_file_that_does_not_compile_is_named_as_such(self):
        # TEST_WRITE はこれを見て、コンパイラの言葉をソルバーに渡して書き直させる。
        run_ = loop.dotnet_build_failure(self.OUTPUT)
        self.assertIn("<did not compile: tests/BoardTests.cs>", run_.failure_kinds)
        self.assertIn("<build failed: src/Logic/Board.cs>", run_.failure_kinds)
        self.assertEqual(run_.errors, 2)
        self.assertIn("tests/BoardTests.cs: error CS0103: The name 'Boad' does not exist",
                      run_.output)
        # 同じエラーの行は1つにまとめる。
        self.assertEqual(run_.output.count("CS0103"), 1)

    def test_an_output_without_errors_is_left_to_the_report(self):
        self.assertIsNone(loop.dotnet_build_failure("Build succeeded."))


class TheStepsOwnTests(CSharp):
    def test_the_class_is_matched_against_the_file_name(self):
        # NUnit の classname は「名前空間.クラス」で、ファイルのパスを持たない。
        self.assertEqual(loop.test_owner("Logic.Tests.BoardTests"), "BoardTests")
        self.assertEqual(loop.csharp_test_classes(["tests/BoardTests.cs", "tests"]),
                         ["BoardTests"])

    def test_other_languages_keep_the_name(self):
        with mock.patch.dict(loop.LANGUAGE, loop.LANGUAGES["python"], clear=True):
            self.assertEqual(loop.test_owner("tests.test_models"), "tests.test_models")


class TheContracts(CSharp):
    def test_a_type_and_a_member_have_their_own_names(self):
        self.assertEqual(loop.declared_name(
            "src/Logic/Board.cs: class Board { public int Width; }"), "Board")
        self.assertEqual(loop.declared_name(
            "src/Logic/Board.cs: static int Board.Score(Board board)"), "Board.Score")
        self.assertEqual(loop.declared_name(
            "src/Logic/Board.cs: Board.Board(int width, int height)"), "Board.Board")

    def test_modules_are_paths_under_the_source_directory(self):
        self.assertEqual(loop.modules_of(["src/Logic/Board.cs"]), ["Logic/Board"])

    def plan(self, provides):
        step = {
            "id": "S1", "kind": "skeleton", "goal": "g", "depends_on": [],
            "contracts": {"requires": [], "provides": provides, "invariants": []},
            "acceptance": [{"case": c, "given": "g", "then": "1"}
                           for c in ("normal", "boundary", "error")],
            "files_write": ["src/Logic/Board.cs"], "files_test": ["tests/BoardTests.cs"],
            "expected_tests": 3, "max_attempts": 3, "review_gate": False,
        }
        with mock.patch.dict(loop.LAYOUT, loop.LAYOUT_DEFAULT):
            return loop.validate_plan({"language": "csharp", "steps": [step]})

    def test_a_bare_collection_is_refused_and_a_shaped_one_is_not(self):
        bare = self.plan(["src/Logic/Board.cs: static List Board.Cells(Board board)"])
        self.assertTrue(any(p.startswith("L14") and "List" in p for p in bare), bare)
        shaped = self.plan(["src/Logic/Board.cs: static List<int> Board.Cells(Board board)"])
        self.assertFalse(any(p.startswith("L14") for p in shaped), shaped)

    def test_a_folder_in_the_path_is_not_read_as_a_type(self):
        with mock.patch.dict(loop.LAYOUT, loop.LAYOUT_DEFAULT):
            problems = loop.validate_plan({"language": "csharp", "steps": [{
                "id": "S1", "kind": "skeleton", "goal": "g", "depends_on": [],
                "contracts": {"requires": [], "invariants": [],
                              "provides": ["src/List/Board.cs: static int Board.Score(Board b)"]},
                "acceptance": [{"case": c, "given": "g", "then": "1"}
                               for c in ("normal", "boundary", "error")],
                "files_write": ["src/List/Board.cs"], "files_test": ["tests/BoardTests.cs"],
                "expected_tests": 3, "max_attempts": 3, "review_gate": False}]})
        self.assertFalse(any(p.startswith("L14") for p in problems), problems)

    def test_the_path_at_the_front_says_where_it_lives(self):
        problems = self.plan(["src/Logic/Board.cs: static int Board.Score(Board board)"])
        self.assertFalse(any(p.startswith("L15") for p in problems), problems)


class TheRunnerWritesTheStub(CSharp):
    """C# のスタブは値を返さず、印の付いた例外を投げる。

    bool には誤った値が無く、文字列をキャストして押し込む手は
    InvalidCastException になる（実測）。どの型にも同じ1行で済む。
    """

    STEP = {"files_write": ["src/Logic/Board.cs", "src/Logic/Cell.cs"],
            "contracts": {"provides": [
                "src/Logic/Cell.cs: enum Cell { Empty, Wall }",
                "src/Logic/Board.cs: class Board { public int Width; public int Height { get; set; } }",
                "src/Logic/Board.cs: Board.Board(int width, int height)",
                "src/Logic/Board.cs: static int Board.Score(Board board)",
                "src/Logic/Board.cs: bool Board.IsWall(int x, int y)",
            ]}}

    def setUp(self):
        super().setUp()
        for p in (mock.patch.dict(loop.LAYOUT, loop.LAYOUT_DEFAULT),
                  mock.patch.object(loop, "UNITY_REFS", Path("/nonexistent/unity-refs"))):
            p.start()
            self.addCleanup(p.stop)

    def test_a_class_is_written_whole(self):
        files = loop.generate_stub(self.STEP, [], {})
        self.assertEqual(files["src/Logic/Board.cs"], """using System;
using System.Collections.Generic;
using System.Linq;

namespace Logic
{
    public class Board
    {
        public int Width;
        public int Height { get; set; }
        public Board(int width, int height) { throw new System.NotImplementedException("__stub__"); }
        public static int Score(Board board) { throw new System.NotImplementedException("__stub__"); }
        public bool IsWall(int x, int y) { throw new System.NotImplementedException("__stub__"); }
    }
}
""")

    def test_an_enum_keeps_its_members(self):
        files = loop.generate_stub(self.STEP, [], {})
        self.assertIn("    public enum Cell\n    {\n        Empty, Wall\n    }",
                      files["src/Logic/Cell.cs"])

    def test_a_type_from_another_folder_is_brought_in_by_its_namespace(self):
        step = {"files_write": ["src/Rules/Scorer.cs"],
                "contracts": {"provides": ["src/Rules/Scorer.cs: static int Scorer.Total(Board board)"]}}
        text = loop.generate_stub(step, ["src/Logic/Board.cs: class Board { }"], {})["src/Rules/Scorer.cs"]
        self.assertIn("using Logic;\n", text)
        self.assertIn("namespace Rules\n", text)
        self.assertIn("    public class Scorer\n", text)

    def test_unity_is_used_only_when_a_signature_names_it(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "refs").mkdir()
            with mock.patch.object(loop, "UNITY_REFS", Path(t)):
                plain = loop.generate_stub(self.STEP, [], {})["src/Logic/Board.cs"]
                step = {"files_write": ["src/Logic/Mover.cs"], "contracts": {"provides": [
                    "src/Logic/Mover.cs: static Vector2Int Mover.Step(Vector2Int from)"]}}
                unity = loop.generate_stub(step, [], {})["src/Logic/Mover.cs"]
        self.assertNotIn("UnityEngine", plain)
        self.assertIn("using UnityEngine;\n", unity)

    def test_what_it_cannot_read_goes_back_to_the_solver(self):
        for provides in (["src/Logic/Board.cs: something in prose"],
                         ["Board.Score(Board board)"]):
            with self.subTest(provides=provides):
                self.assertIsNone(loop.generate_stub(
                    {"files_write": ["src/Logic/Board.cs"],
                     "contracts": {"provides": provides}}, [], {}))

    def test_an_existing_file_without_the_type_goes_back_to_the_solver(self):
        self.assertIsNone(loop.generate_stub(
            self.STEP, [], {"src/Logic/Board.cs": "namespace Logic { }"}))

    def test_a_namespace_stated_on_a_required_line_is_used(self):
        # 既存のファイルは、フォルダと違う名前空間を持ちうる。
        step = {"files_write": ["src/Rules/Scorer.cs"],
                "contracts": {"provides": ["src/Rules/Scorer.cs: static int Scorer.Total(Board board)"]}}
        text = loop.generate_stub(
            step, ["src/Logic/Board.cs: class Board -- namespace Game.Core"], {})["src/Rules/Scorer.cs"]
        self.assertIn("using Game.Core;\n", text)
        self.assertNotIn("using Logic;", text)

    def test_a_folder_that_is_not_a_namespace_goes_back_to_the_solver(self):
        self.assertIsNone(loop.generate_stub(
            {"files_write": ["src/Game-Logic/Board.cs"],
             "contracts": {"provides": ["src/Game-Logic/Board.cs: class Board { }"]}}, [], {}))

    def test_the_marked_exception_is_red_and_an_unmarked_one_is_not(self):
        marked = loop.failure_kind(self.failure("System.NotImplementedException : __stub__"))
        self.assertEqual(marked, "StubNotImplemented")
        self.assertTrue(loop.LANGUAGE["red_kinds"].match(marked))
        unmarked = loop.failure_kind(self.failure("System.NotImplementedException : later"))
        self.assertFalse(loop.LANGUAGE["red_kinds"].match(unmarked))

    def test_the_solver_s_brief_asks_for_the_same_body(self):
        brief = loop.brief_stub(self.STEP)
        self.assertIn('throw new System.NotImplementedException("__stub__");', brief)
        self.assertIn("Do not return values", brief)
        self.assertNotIn("THERE IS NO WRONG BOOLEAN", brief)


EXISTING_CS = '''﻿using System;

namespace Game.Logic
{
    /// <summary>盤面。</summary>
    [Serializable]
    public class Board
    {
        private int width = 3;
        public int Width => width;
        public int Height { get; private set; }
        public const string Brace = "}{";
        public static readonly Board Empty = new Board(0, 0);

        public Board(int width, int height)
        {
            this.width = width; Height = height;
        }

        public static int Score(Board board)
        {
            var s = $"{board.Width}}}{{ {(board.Height > 0 ? "}" : "{")}";
            var v = @"a""}";
            var c = '{';
            // {
            /* } */
            return board.Width * board.Height;
        }

        public int Area() => Width * Height;

#if UNITY_EDITOR
        public void Gizmo() { Debug.Log("x"); }
#else
        public void Runtime() { }
#endif

        public int Helper(int n) { return n + 1; }
    }

    public interface IShape { int Area(); }
    public enum Cell { Empty, Wall }
    internal class Hidden { public void X() { } }
}
'''


class WhatTheCodeAlreadyDeclares(CSharp):
    """取り込んだ C# のファイルの public な宣言を、契約の書式で渡す。

    C# の宣言は型の中にあるので、型の行とメンバーの行に分ける。名前空間は
    フォルダと違いうるので、行の末尾に書く。
    """

    def setUp(self):
        super().setUp()
        p = mock.patch.object(loop, "UNITY_REFS", Path("/nonexistent/unity-refs"))
        p.start()
        self.addCleanup(p.stop)

    def test_public_types_and_members_are_listed_with_their_namespace(self):
        at, where = "src/Logic/Board.cs: ", " -- namespace Game.Logic"
        self.assertEqual(loop.csharp_declarations(EXISTING_CS, "src/Logic/Board.cs"), [
            at + "class Board" + where,
            at + "int Board.Width { get; }" + where,
            at + "int Board.Height { get; }" + where,
            at + "const string Board.Brace" + where,
            at + "static readonly Board Board.Empty" + where,
            at + "Board.Board(int width, int height)" + where,
            at + "static int Board.Score(Board board)" + where,
            at + "int Board.Area()" + where,
            at + "void Board.Runtime()" + where,
            at + "int Board.Helper(int n)" + where,
            at + "interface IShape { int Area(); }" + where,
            at + "enum Cell { Empty, Wall }" + where,
        ])

    def test_code_the_build_leaves_out_is_not_listed(self):
        # Unity の参照は UNITY_EDITOR を定義しない。テストからは呼べない。
        lines = loop.csharp_declarations(EXISTING_CS, "src/Logic/Board.cs")
        self.assertFalse(any("Gizmo" in line for line in lines))
        self.assertFalse(any("Hidden" in line for line in lines))

    def test_each_line_declares_the_name_requires_uses(self):
        for line, name in (("src/B.cs: int Board.Width { get; } -- namespace Game.Logic", "Board.Width"),
                           ("src/B.cs: const string Board.Brace -- namespace Game.Logic", "Board.Brace"),
                           ("src/B.cs: T Board.Get<T>() -- namespace Game.Logic", "Board.Get"),
                           ("src/B.cs: class Board -- namespace Game.Logic", "Board")):
            with self.subTest(line=line):
                self.assertEqual(loop.declared_name(line), name)

    def test_the_condition_follows_the_symbols_of_the_build(self):
        text = "#if !UNITY_EDITOR && (DEBUG || FOO)\npublic class A { }\n#elif true\npublic class B { }\n#endif\n"
        self.assertEqual(loop.csharp_declarations(text, "src/A.cs"), ["src/A.cs: class A"])

    def test_an_empty_line_in_code_the_build_leaves_out_is_passed(self):
        # 空の行では行の終わりが始まりと同じ位置になり、走査が進まなくなっていた。
        text = "#if UNITY_EDITOR\n\npublic class A { }\n\n#endif\n\npublic class B { }\n"
        self.assertEqual(loop.csharp_declarations(text, "src/A.cs"), ["src/A.cs: class B"])

    def test_a_verbatim_type_name_is_read_without_its_at(self):
        # Input System が生成するクラスは `@GameInputs` と書かれる。
        self.assertEqual(loop.csharp_declarations(
            "public partial class @Inputs: IDisposable { public void Enable() { } }", "src/I.cs"),
            ["src/I.cs: class Inputs: IDisposable", "src/I.cs: void Inputs.Enable()"])

    def test_a_file_that_cannot_be_read_gives_nothing(self):
        for text in ("#if DEBUG\npublic class A { }\n", "public class A { /* }\n",
                     "namespace A;\npublic class B { }\n"):
            with self.subTest(text=text):
                self.assertEqual(loop.csharp_declarations(text, "src/A.cs"), [])

    def test_existing_contracts_reads_the_c_sharp_files(self):
        files = {"src/Logic/Cell.cs": "namespace Logic { public enum Cell { Empty } }\n",
                 "src/Logic/Cell.cs.meta": "guid: 1\n"}

        def run(cmd, **_):
            if cmd[1] == "ls-tree":
                return SimpleNamespace(returncode=0, stderr="", stdout="\0".join(files) + "\0")
            return SimpleNamespace(returncode=0, stderr="", stdout=files[cmd[2].split(":", 1)[1]])

        with mock.patch("loop.run", side_effect=run), \
             mock.patch.dict(loop.LAYOUT, loop.LAYOUT_DEFAULT):
            self.assertEqual(loop.existing_contracts(),
                             ["src/Logic/Cell.cs: enum Cell { Empty } -- namespace Logic"])


class TheStubKeepsTheExistingCSharp(CSharp):
    """既存のファイルでは、provides のメンバーの本体だけを差し替える。"""

    PATH = "src/Logic/Board.cs"

    def setUp(self):
        super().setUp()
        for p in (mock.patch.dict(loop.LAYOUT, loop.LAYOUT_DEFAULT),
                  mock.patch.object(loop, "UNITY_REFS", Path("/nonexistent/unity-refs"))):
            p.start()
            self.addCleanup(p.stop)

    def build(self, *provides, requires=(), original=EXISTING_CS):
        step = {"files_write": [self.PATH],
                "contracts": {"provides": [f"{self.PATH}: {p}" for p in provides]}}
        files = loop.generate_stub(step, list(requires), {self.PATH: original})
        return step, files and files[self.PATH]

    def test_only_the_body_of_the_provided_method_is_replaced(self):
        _, text = self.build("static int Board.Score(Board board)")
        self.assertIn("public static int Score(Board board)\n"
                      '        { throw new System.NotImplementedException("__stub__"); }', text)
        self.assertNotIn("board.Width * board.Height", text)
        for kept in ("/// <summary>盤面。</summary>", "[Serializable]",
                     "public int Helper(int n) { return n + 1; }",
                     'public void Gizmo() { Debug.Log("x"); }', "#if UNITY_EDITOR"):
            self.assertIn(kept, text)
        self.assertTrue(text.startswith("﻿using System;\n"))

    def test_an_expression_body_becomes_the_same_throw(self):
        _, text = self.build("int Board.Area()", "int Board.Width { get; }")
        self.assertIn('public int Area() { throw new System.NotImplementedException("__stub__"); }',
                      text)
        self.assertIn('public int Width => throw new System.NotImplementedException("__stub__");',
                      text)

    def test_a_constructor_and_an_auto_property(self):
        _, text = self.build("Board.Board(int width, int height)", "int Board.Height { get; }")
        self.assertIn("public Board(int width, int height)\n"
                      '        { throw new System.NotImplementedException("__stub__"); }', text)
        # 自動プロパティは値を持つだけで、差し替える振る舞いが無い。
        self.assertIn("public int Height { get; private set; }", text)

    def test_a_member_the_type_lacks_goes_at_its_end_with_its_using(self):
        _, text = self.build("int Board.Apply(Rule rule)",
                             requires=["src/Rules/Rule.cs: class Rule -- namespace Game.Rules"])
        self.assertIn("        public int Helper(int n) { return n + 1; }\n\n"
                      '        public int Apply(Rule rule) { throw new System.NotImplementedException("__stub__"); }\n'
                      "    }\n", text)
        self.assertIn("using System;\nusing Game.Rules;\n", text)

    def test_a_using_is_not_added_when_nothing_new_names_it(self):
        _, text = self.build("int Board.Area()",
                             requires=["src/Rules/Rule.cs: class Rule -- namespace Game.Rules"])
        self.assertNotIn("using Game.Rules;", text)

    def test_a_changed_signature_goes_back_to_the_solver(self):
        _, text = self.build("static int Board.Score(Board board, int bonus)")
        self.assertIsNone(text)

    def test_a_type_line_that_does_not_match_goes_back_to_the_solver(self):
        self.assertIsNone(self.build("struct Board { }")[1])
        self.assertIsNone(self.build("enum Cell { Empty, Wall, Door }")[1])

    def test_the_result_passes_the_runner_s_own_check(self):
        step, text = self.build("static int Board.Score(Board board)", "int Board.Area()",
                                "int Board.Width { get; }", "int Board.Apply(int rule)")
        self.assertEqual(kept_the_rest(text, step), [])


def kept_the_rest(after: str, step: dict) -> list[str]:
    """EXISTING_CS を after に書き換えたときの stub_kept_the_rest。"""
    with tempfile.TemporaryDirectory() as temp, \
         mock.patch.object(loop, "UNITY_REFS", Path("/nonexistent/unity-refs")):
        target = Path(temp) / "src/Logic/Board.cs"
        target.parent.mkdir(parents=True)
        target.write_text(after, encoding="utf-8")
        with mock.patch.object(loop, "PROJECT", Path(temp)):
            return loop.stub_kept_the_rest(step, {"src/Logic/Board.cs": EXISTING_CS})


class TheRunnerChecksEachMember(CSharp):
    """C# の比べる単位は型の頭とメンバー。クラスを1つの単位にすると、1つの
    メソッドの差し替えでクラス全体が変わったことになる。"""

    STEP = {"contracts": {"provides": ["src/Logic/Board.cs: static int Board.Score(Board board)"]}}

    def check(self, after):
        return kept_the_rest(after, self.STEP)

    def test_replacing_the_provided_body_passes(self):
        after = EXISTING_CS.replace("return board.Width * board.Height;", "return -1;")
        self.assertEqual(self.check(after), [])

    def test_comments_whitespace_and_a_new_using_are_not_changes(self):
        after = ("using Game.Rules;\n"
                 + EXISTING_CS.replace("public int Helper(int n) { return n + 1; }",
                                       "// 補助\n        public int Helper(int n)\n        { return n + 1; }"))
        self.assertEqual(self.check(after), [])

    def test_another_method_of_the_same_class_is_named(self):
        after = EXISTING_CS.replace("return n + 1;", "return n + 2;")
        self.assertEqual(self.check(after), [
            "src/Logic/Board.cs: changed or removed: public int Helper(int n) { return n + 1; }"])

    def test_code_the_build_leaves_out_is_still_compared(self):
        # UNITY_EDITOR の中は、テストでは動かないが Unity のエディタでは動く。
        after = EXISTING_CS.replace('Debug.Log("x")', 'Debug.Log("y")')
        self.assertEqual(self.check(after), [
            'src/Logic/Board.cs: changed or removed: public void Gizmo() { Debug.Log("x"); }'])

    def test_a_changed_type_head_is_named(self):
        after = EXISTING_CS.replace("public class Board\n", "public class Board : IShape\n")
        self.assertEqual(len(self.check(after)), 1)
        self.assertIn("public class Board", self.check(after)[0])

    def test_a_file_that_no_longer_parses_is_reported(self):
        self.assertEqual(self.check("namespace A { public class B {"),
                         ["src/Logic/Board.cs: no longer parses"])


class WhatThePlannerIsTold(CSharp):
    def facts(self, unity: bool, project: Path):
        refs = project.parent / "unity-refs"
        if unity:
            (refs / "refs").mkdir(parents=True)
            (refs / "version.txt").write_text("6000.3.10f1\n", encoding="utf-8")
        with mock.patch.object(loop, "UNITY_REFS", refs), \
             mock.patch.object(loop, "PROJECT", project), \
             mock.patch.object(loop, "SRC", project / "Assets" / "Source"), \
             mock.patch.object(loop, "TESTS", project / "Assets" / "Tests"), \
             mock.patch.dict(loop.LAYOUT, {"src": "Assets/Source", "tests": "Assets/Tests"}), \
             mock.patch.object(loop, "existing_declarations", return_value={}), \
             mock.patch.object(loop, "dotnet_projects", side_effect=OSError("no feed")), \
             mock.patch.dict(os.environ, {"DISPLAY": ""}), \
             mock.patch("loop.run", return_value=SimpleNamespace(
                 returncode=0, stdout="8.0.131", stderr="")):
            return loop.environment_facts()

    def unity_project(self, temp: Path) -> Path:
        project = temp / "project"
        for rel in ("Assets/Source/Logic/Board.cs", "Assets/Source/Logic/Board.cs.meta",
                    "Assets/Resources/Sprite/cell.png", "Assets/Resources/Sprite/cell.png.meta",
                    "Assets/Tests/BoardTests.cs", "ProjectSettings/ProjectVersion.txt",
                    ".gitignore"):
            (project / rel).parent.mkdir(parents=True, exist_ok=True)
            (project / rel).write_text("", encoding="utf-8")
        return project

    def test_with_unity_the_limits_of_the_tests_are_stated(self):
        with tempfile.TemporaryDirectory() as t:
            facts = self.facts(True, self.unity_project(Path(t)))
        self.assertIn("Unity 6000.3.10f1", facts)
        self.assertIn("SecurityException", facts)
        self.assertIn("a MonoBehaviour (even with `new`)", facts)
        self.assertIn("Put the behaviour a criterion checks in a plain class", facts)

    def test_without_unity_there_is_no_engine_to_warn_about(self):
        with tempfile.TemporaryDirectory() as t:
            facts = self.facts(False, self.unity_project(Path(t)))
        self.assertNotIn("SecurityException", facts)
        self.assertIn("User interface: none", facts)

    def test_only_the_root_and_the_fence_are_listed(self):
        # Unity の Assets/ を全部並べると、ブリーフが数万トークンになる。
        with tempfile.TemporaryDirectory() as t:
            facts = self.facts(True, self.unity_project(Path(t)))
        self.assertIn("  Assets/Source/Logic/Board.cs\n", facts)
        self.assertIn("  Assets/Tests/BoardTests.cs\n", facts)
        self.assertIn("  ProjectSettings/\n", facts)
        self.assertNotIn("cell.png", facts)
        self.assertNotIn(".meta", facts)
        self.assertNotIn("ProjectVersion.txt", facts)

    def test_the_layout_note_is_c_sharp_s(self):
        with mock.patch.dict(loop.LAYOUT, {"src": "Assets/Source", "tests": "Assets/Tests"}), \
             mock.patch.object(loop, "environment_facts", return_value=""):
            brief = loop.brief_plan_bootstrap("requirements")
        self.assertIn("C# 9 is the ceiling", brief)
        self.assertIn("Assets/Source/Logic/Board.cs: static int Board.Score(Board board)", brief)
        self.assertIn("class Board { public int Width; public int Height; }", brief)
        self.assertNotIn("{SRC}", brief)


if __name__ == "__main__":
    unittest.main()
