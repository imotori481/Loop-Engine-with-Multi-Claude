"""書き込みの柵の場所をプロジェクトごとに決める。

Unity のプロジェクトはコードを Assets/ の下に置く。柵が src/ と tests/ 固定だと、
どのモジュールも Unity の外に書かれる。場所は /srv/loop/layout.json（root 所有）から読み、
規則は layout_problems にしか書かない。loop-project.sh とプロビジョニングも、
これを import して同じ規則で確かめる。

    python3 -m unittest discover -s runner/tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop  # noqa: E402

UNITY = {"src": "Assets/Source", "tests": "Assets/Tests/Editor/Loop"}


class WhatALayoutMayBe(unittest.TestCase):
    def test_the_default_and_a_unity_layout_are_accepted(self):
        self.assertEqual(loop.layout_problems(loop.LAYOUT_DEFAULT), [])
        self.assertEqual(loop.layout_problems(UNITY), [])

    def test_a_path_that_leaves_the_project_is_refused(self):
        for bad in ("/srv/loop", "../elsewhere", "Assets/../plan", "Assets//Source",
                    "Assets/", "./src"):
            with self.subTest(src=bad):
                self.assertNotEqual(loop.layout_problems({"src": bad, "tests": "tests"}), [])

    def test_a_place_the_runner_owns_is_refused(self):
        for bad in ("plan", ".git/x", ".runner", ".venv/lib", "node_modules/x", "docs/src", "ac"):
            with self.subTest(src=bad):
                self.assertNotEqual(loop.layout_problems({"src": bad, "tests": "tests"}), [])

    def test_characters_bash_could_misquote_are_refused(self):
        for bad in ("TextMesh Pro", "src;rm", "src$x", "ソース"):
            with self.subTest(src=bad):
                self.assertNotEqual(loop.layout_problems({"src": bad, "tests": "tests"}), [])

    def test_one_directory_inside_the_other_is_refused(self):
        # テストを凍結すると、それを含むコードの柵まで閉じる。逆なら、コードを
        # 開けたときに凍結したテストまで開く。
        self.assertNotEqual(
            loop.layout_problems({"src": "Assets", "tests": "Assets/Tests"}), [])
        self.assertNotEqual(
            loop.layout_problems({"src": "Assets/Source/Logic", "tests": "Assets/Source"}), [])
        self.assertEqual(
            loop.layout_problems({"src": "Assets/Source", "tests": "Assets/SourceTests"}), [])

    def test_the_shape_is_exactly_two_keys(self):
        self.assertNotEqual(loop.layout_problems({"src": "src"}), [])
        self.assertNotEqual(loop.layout_problems({"src": "a", "tests": "b", "x": "c"}), [])
        self.assertNotEqual(loop.layout_problems(["src", "tests"]), [])


class ReadingTheLayout(unittest.TestCase):
    def read(self, text):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "layout.json"
            if text is not None:
                path.write_text(text, encoding="utf-8")
            with mock.patch.object(loop, "LAYOUT_FILE", path):
                return loop.read_layout()

    def test_no_file_means_src_and_tests(self):
        self.assertEqual(self.read(None), {"src": "src", "tests": "tests"})

    def test_a_file_is_used_as_written(self):
        self.assertEqual(self.read(json.dumps(UNITY)), UNITY)

    def test_a_broken_file_stops_the_runner_rather_than_falling_back(self):
        # 既定に戻ると、柵が黙って別の場所に開く。
        with self.assertRaises(SystemExit):
            self.read("{not json")
        with self.assertRaises(SystemExit):
            self.read(json.dumps({"src": "../x", "tests": "tests"}))


class TheRulesFollowTheLayout(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(loop.LAYOUT, UNITY)
        p.start()
        self.addCleanup(p.stop)

    def test_in_layout_is_by_whole_directory_names(self):
        self.assertTrue(loop.in_layout("Assets/Source/Gameplay/Board.cs", "src"))
        self.assertFalse(loop.in_layout("Assets/SourceX/Board.cs", "src"))
        self.assertFalse(loop.in_layout("Assets/Source", "src"))
        self.assertFalse(loop.in_layout("Assets/Source/../Plugins/x.cs", "src"))
        self.assertFalse(loop.in_layout("src/a.py", "src"))

    def test_a_module_s_files_are_inside_the_layout(self):
        with mock.patch.dict(loop.LANGUAGE, loop.LANGUAGES["python"], clear=True):
            self.assertEqual(loop.module_files("game/board"), ["Assets/Source/game/board.py"])
            self.assertEqual(loop.module_test_files("game/board", True), [
                "Assets/Tests/Editor/Loop/test_game_board.py",
                "Assets/Tests/Editor/Loop/test_game_board_integration.py"])

    def test_modules_are_named_from_inside_the_source_directory(self):
        with mock.patch.dict(loop.LANGUAGE, loop.LANGUAGES["python"], clear=True):
            self.assertEqual(loop.modules_of(["Assets/Source/game/board.py"]), ["game.board"])

    def brief(self) -> str:
        with mock.patch.dict(loop.LANGUAGE, loop.LANGUAGES["typescript"], clear=True), \
             mock.patch.object(loop, "environment_facts", return_value=""):
            return loop.brief_modules("仕様", "基本", None, "")

    def test_the_planner_is_told_where_the_code_goes(self):
        brief = self.brief()
        self.assertIn("`Assets/Source/logic/board.ts`\n  is `modules/logic/board.md`", brief)
        # テストから見たコードへの相対パスも、柵の場所から出す。
        self.assertIn('from "../../../Source/idlegame/models.ts"', brief)
        self.assertIn('import { start } from "/Assets/Source/main.ts"', brief)
        self.assertNotIn("{SRC}", brief)
        self.assertNotIn("{TESTS}", brief)

    def test_the_default_brief_reads_as_it_did(self):
        with mock.patch.dict(loop.LAYOUT, loop.LAYOUT_DEFAULT):
            brief = self.brief()
        self.assertIn('from "../src/idlegame/models.ts"', brief)
        self.assertIn("`src/logic/board.ts`", brief)


if __name__ == "__main__":
    unittest.main()
