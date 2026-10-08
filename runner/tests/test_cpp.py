"""C++ を計画の言語にする。

箱で確かめるのは標準の C++17 のロジックだけで、g++ と CMake でビルドし、GoogleTest で
走らせる。DXライブラリや Windows の API を include するファイルはビルドから外す。
失敗の形は GoogleTest の junit のレポートに合わせてある。箱の smoke-cpp が同じ形を
確かめる。

    python3 -m unittest discover -s runner/tests
"""

import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop  # noqa: E402


class Cpp(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(loop.LANGUAGE, loop.LANGUAGES["cpp"], clear=True)
        p.start()
        self.addCleanup(p.stop)

    def patch(self, **values):
        for name, value in values.items():
            p = mock.patch.object(loop, name, value)
            p.start()
            self.addCleanup(p.stop)

    def failure(self, message):
        element = ET.Element("failure", {"message": message, "type": ""})
        element.text = message
        return element


class HowAFailureIsRead(Cpp):
    def test_an_assertion_is_red(self):
        kind = loop.failure_kind(self.failure(
            "/srv/loop/project/tests/BoardTest.cpp:12\nExpected equality of these values:\n"
            "  6\n  add(2, 3)\n    Which is: 5"))
        self.assertEqual(kind, "AssertionFailure")
        self.assertTrue(loop.LANGUAGE["red_kinds"].match(kind))

    def test_the_stubs_exception_is_red(self):
        kind = loop.failure_kind(self.failure(
            'unknown file\nC++ exception with description "__stub__" thrown in the test body.'))
        self.assertEqual(kind, "StubNotImplemented")
        self.assertTrue(loop.LANGUAGE["red_kinds"].match(kind))

    def test_another_exception_is_not_red(self):
        kind = loop.failure_kind(self.failure(
            'unknown file\nC++ exception with description "bad state" thrown in the test body.'))
        self.assertEqual(kind, "std::exception")
        self.assertFalse(loop.LANGUAGE["red_kinds"].match(kind))

    def test_reading_past_an_empty_stub_container_is_a_crash_the_writer_can_fix(self):
        kind = loop.failure_kind(self.failure(
            'unknown file\nC++ exception with description "vector::_M_range_check: __n '
            '(which is 0) >= this->size() (which is 0)" thrown in the test body.'))
        self.assertEqual(kind, "std::out_of_range")
        self.assertFalse(loop.LANGUAGE["red_kinds"].match(kind))
        run_ = loop.TestRun(1, 1, 0, 0, [kind], [], "", failure_details=["Reads"])
        self.assertEqual(loop.crashed_tests(run_), "Reads")

    def test_an_exception_of_no_known_type_is_not_red(self):
        kind = loop.failure_kind(self.failure("unknown file\nUnknown C++ exception thrown in the test body."))
        self.assertEqual(kind, "UnknownException")


class TheCommand(Cpp):
    def test_a_step_s_tests_are_selected_by_suite_name(self):
        argv, env = loop.test_argv(["tests/BoardTest.cpp", "tests/rules/ScoreTest.cpp"],
                                   Path("/srv/loop/project/.runner/pytest-red-1.xml"))
        self.assertTrue(argv[0].replace("\\", "/").endswith("/srv/loop/cpp/build/out/loop_tests"))
        self.assertIn("--gtest_filter=BoardTest.*:ScoreTest.*", argv)
        self.assertIn("--gtest_also_run_disabled_tests", argv)
        self.assertTrue(any(a.startswith("--gtest_output=xml:") for a in argv))
        self.assertEqual(env["LC_ALL"], "C")

    def test_the_whole_suite_has_no_filter(self):
        argv, _ = loop.test_argv(["tests"], Path("/tmp/r.xml"))
        self.assertFalse(any(a.startswith("--gtest_filter") for a in argv))

    def test_the_suite_is_matched_against_the_file_name(self):
        self.assertEqual(loop.test_owner("BoardTest"), "BoardTest")
        self.assertEqual(loop.cpp_test_suites(["tests/BoardTest.cpp", "tests/helpers.h", "tests"]),
                         ["BoardTest"])

    def test_test_names_are_read_from_the_macros(self):
        text = "TEST(BoardTest, ScoreCountsRows) { }\nTEST_F(BoardTest , Empty)\n{ }\n"
        self.assertEqual(loop.test_names(text), ["ScoreCountsRows", "Empty"])


class AProject(Cpp):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.patch(PROJECT=self.root, SRC=self.root / "src", TESTS=self.root / "tests",
                   CPP_BUILD=self.root / "build", REQUIREMENTS=self.root / "REQUIREMENTS.md")

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


class WhatIsBuiltInTheBox(AProject):
    def test_files_that_reach_a_header_the_box_lacks_are_left_out(self):
        self.write("src/logic/Board.h", "#pragma once\n#include <vector>\nstruct Board {};\n")
        self.write("src/logic/Board.cpp", '#include "logic/Board.h"\n#include <stdexcept>\n')
        self.write("src/view/Draw.h", '#pragma once\n#include "DxLib.h"\n')
        self.write("src/view/Draw.cpp", '#include "Draw.h"\n#include "logic/Board.h"\n')
        self.write("src/main.cpp", '#include <windows.h>\n')
        self.write("src/readme.txt", "#include <nothing>\n")
        built, left_out = loop.cpp_sources()
        self.assertEqual(built, ["src/logic/Board.cpp"])
        self.assertEqual(left_out, {"src/view/Draw.cpp": "DxLib.h", "src/main.cpp": "windows.h"})

    def test_a_dxlib_header_in_the_repository_is_followed_to_what_it_includes(self):
        self.write("lib/DxLib.h", "#pragma once\n#include <windows.h>\n")
        self.write("src/Game.cpp", '#include "lib/DxLib.h"\n')
        self.assertEqual(loop.cpp_sources(), ([], {"src/Game.cpp": "windows.h"}))

    def test_a_commented_include_and_gtest_do_not_count(self):
        self.write("src/A.cpp", "// #include <windows.h>\n#include <gtest/gtest.h>\n")
        self.assertEqual(loop.cpp_sources(), (["src/A.cpp"], {}))

    def test_the_cmake_project_lists_only_what_is_built(self):
        self.write("src/logic/Board.cpp", "")
        self.write("src/main.cpp", '#include "DxLib.h"\n')
        self.write("tests/BoardTest.cpp", "")
        text = loop.cpp_cmake_lists()
        self.assertIn("set(CMAKE_CXX_STANDARD 17)", text)
        self.assertIn(f'"{(self.root / "src/logic/Board.cpp").as_posix()}"', text)
        self.assertNotIn("main.cpp", text)
        self.assertIn(f'"{(self.root / "tests/BoardTest.cpp").as_posix()}"', text)
        self.assertIn("GTest::gtest_main", text)

    def test_header_only_code_is_an_interface_library(self):
        self.write("src/logic/Board.h", "#pragma once\n")
        self.assertIn("add_library(loop_code INTERFACE)", loop.cpp_cmake_lists())

    def test_the_build_configures_once_and_then_only_builds(self):
        self.write("tests/BoardTest.cpp", "")

        def built(cmd, **_):
            if "-S" in cmd:
                (self.root / "build" / "out").mkdir(parents=True, exist_ok=True)
                (self.root / "build" / "out" / "CMakeCache.txt").write_text("", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("loop.run", side_effect=built) as run:
            self.assertIsNone(loop.cpp_build(600))
            self.assertIsNone(loop.cpp_build(600))
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(sum("-S" in c for c in commands), 1)
        self.assertEqual(sum("--build" in c for c in commands), 2)
        self.assertTrue((self.root / "build" / "CMakeLists.txt").is_file())


class WhenTheBuildFails(AProject):
    """g++ はエラーの行に絶対パスを書く。行の形は箱のパスで確かめる。"""

    OUTPUT = """[ 50%] Building CXX object CMakeFiles/loop_tests.dir/tests/BoardTest.cpp.o
/srv/loop/project/tests/BoardTest.cpp:12:9: error: 'Boad' was not declared in this scope
/srv/loop/project/tests/BoardTest.cpp:12:9: error: 'Boad' was not declared in this scope
/srv/loop/project/src/logic/Board.h:3:5: error: expected ';' after struct definition
"""

    def setUp(self):
        super().setUp()
        box = Path("/srv/loop/project")
        self.patch(PROJECT=box, SRC=box / "src", TESTS=box / "tests")

    def test_a_test_file_that_does_not_compile_is_named_as_such(self):
        run_ = loop.cpp_build_failure(self.OUTPUT)
        self.assertIn("<did not compile: tests/BoardTest.cpp>", run_.failure_kinds)
        self.assertIn("<build failed: src/logic/Board.h>", run_.failure_kinds)
        self.assertEqual(run_.errors, 2)
        self.assertEqual(run_.output.count("'Boad'"), 1)

    def test_a_missing_definition_is_a_build_failure(self):
        run_ = loop.cpp_build_failure(
            "/usr/bin/ld: BoardTest.cpp.o: in function `BoardTest_Score_Test::TestBody()':\n"
            "BoardTest.cpp:(.text+0x2a): undefined reference to `Board::score() const'\n"
            "collect2: error: ld returned 1 exit status\n")
        self.assertEqual(run_.failure_kinds, ["<build failed: undefined reference>"])
        self.assertIn("Board::score() const", run_.output)

    def test_an_output_without_errors_is_left_to_the_caller(self):
        self.assertIsNone(loop.cpp_build_failure("[100%] Built target loop_tests"))

    def test_a_failed_build_stops_before_the_tests_run(self):
        (self.root / "build" / "out").mkdir(parents=True)
        (self.root / "build" / "out" / "CMakeCache.txt").write_text("", encoding="utf-8")
        failed = SimpleNamespace(returncode=2, stdout=self.OUTPUT, stderr="")
        with mock.patch("loop.run", return_value=failed) as run, \
                mock.patch("loop.report_now"), mock.patch.object(loop, "STATE", self.root / ".runner"):
            result = loop.pytest_run("red-1", ["tests/BoardTest.cpp"])
        self.assertIn("<did not compile: tests/BoardTest.cpp>", result.failure_kinds)
        self.assertEqual(run.call_count, 1)

    def test_a_program_that_dies_leaves_no_report_and_says_so(self):
        (self.root / "build" / "out").mkdir(parents=True)
        (self.root / "build" / "out" / "CMakeCache.txt").write_text("", encoding="utf-8")
        answers = [SimpleNamespace(returncode=0, stdout="", stderr=""),
                   SimpleNamespace(returncode=-11, stdout="[ RUN      ] BoardTest.Reads\n", stderr="")]
        with mock.patch("loop.run", side_effect=answers), \
                mock.patch("loop.report_now"), mock.patch.object(loop, "STATE", self.root / ".runner"):
            result = loop.pytest_run("red-1", ["tests/BoardTest.cpp"])
        self.assertEqual(result.failure_kinds, ["<test program died: exit status -11>"])
        self.assertIn("BoardTest.Reads", result.output)


class TheContracts(Cpp):
    def test_names_are_types_member_functions_and_free_functions(self):
        for line, name in (
                ("src/logic/Board.h: struct Board { int width; int height; }", "Board"),
                ("src/logic/Board.h: enum class Cell { Empty, Wall }", "Cell"),
                ("src/logic/Board.h: int Board::score() const", "Board::score"),
                ("src/logic/Board.h: Board::Board(int width, int height)", "Board::Board"),
                ("src/logic/rules.h: std::vector<int> countLines(const Board& board)", "countLines")):
            self.assertEqual(loop.declared_name(line), name, line)

    def test_a_contract_names_its_header_as_the_module(self):
        self.assertEqual(loop.modules_of(["src/logic/Board.h", "src/logic/Board.cpp"]),
                         ["logic/Board"])

    def test_a_bare_container_has_no_shape(self):
        pattern = loop.LANGUAGE["shape_pattern"]
        bare = {name for name, bracket in pattern.findall("std::vector cells(std::map<int, int> m)")
                if bracket != "<"}
        self.assertEqual(bare, {"vector"})


class WhatThePlannerIsTold(AProject):
    def test_left_out_files_are_listed_and_dxlib_gets_its_sheet(self):
        self.write("src/main.cpp", '#include "DxLib.h"\n')
        summary, text = loop.cpp_facts()
        self.assertIn("DXLib", summary)
        self.assertIn("src/main.cpp  (includes DxLib.h)", text)
        self.assertIn("ProcessMessage", text)

    def test_requirements_that_ask_for_dxlib_get_the_sheet_before_any_code(self):
        self.write("REQUIREMENTS.md", "DXライブラリで動くブロック崩し\n")
        self.assertIn("ScreenFlip", loop.cpp_facts()[1])

    def test_other_projects_are_not_told_about_dxlib(self):
        self.write("src/logic/Board.cpp", "#include <vector>\n")
        summary, text = loop.cpp_facts()
        self.assertNotIn("DXLib", summary)
        self.assertNotIn("ProcessMessage", text)

    def test_the_stub_brief_asks_for_the_marked_exception(self):
        step = {"contracts": {"provides": ["src/logic/Board.h: int Board::score() const"],
                              "requires": []},
                "files_write": ["src/logic/Board.h", "src/logic/Board.cpp"]}
        with mock.patch("loop.render_provides", return_value="int Board::score() const"):
            brief = loop.brief_stub(step)
        self.assertIn(loop.CPP_STUB_BODY, brief)
        self.assertIn("#include <stdexcept>", brief)


if __name__ == "__main__":
    unittest.main()
