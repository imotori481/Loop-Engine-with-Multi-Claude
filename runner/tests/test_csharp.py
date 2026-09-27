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
             mock.patch.object(loop, "existing_contracts", return_value=[]), \
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
