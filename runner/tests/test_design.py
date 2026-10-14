"""設計仕様書の読み方と検査、段のコマンド、下流の計画の作り方。

ランナーが読むのは、詳細設計仕様書の契約のブロックと節の区切り、受け入れ条件の
JSON だけだ。文の中身は判断しない。検査は契約の辻褄だけを見て、振る舞いは
VERIFY が確かめる。

    python3 -m unittest discover -s runner/tests
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop  # noqa: E402

CRITERIA = [
    {"case": "normal", "kind": "unit", "given": "a 3x3 board", "then": "returns 9"},
    {"case": "boundary", "kind": "unit", "given": "an empty board", "then": "returns 0"},
    {"case": "error", "kind": "unit", "given": "a width of -1", "then": "raises ValueError"},
]


def doc(*lines: dict, behaviour: str = "盤面を数える。") -> str:
    contract = "\n".join(json.dumps(line, ensure_ascii=False) for line in lines)
    return (f"# モジュール\n\n## 振る舞い\n{behaviour}\n\n"
            f"## 契約\n```ndjson contract\n{contract}\n```\n")


def ac(criteria: list[dict] = CRITERIA) -> str:
    return json.dumps({"criteria": criteria})


class Python(unittest.TestCase):
    def setUp(self) -> None:
        for p in (mock.patch.dict(loop.LANGUAGE, loop.LANGUAGES["python"], clear=True),
                  mock.patch.dict(loop.LAYOUT, loop.LAYOUT_DEFAULT),
                  mock.patch.object(loop, "head_files", return_value=set()),
                  mock.patch.object(loop, "existing_declarations", return_value={})):
            p.start()
            self.addCleanup(p.stop)


class TheContractBlock(unittest.TestCase):
    def test_provides_and_requires_are_read(self):
        provides, requires, problems = loop.parse_contract(doc(
            {"provides": "def score(board:  Board) -> int"},
            {"provides": "def main() -> None", "entry": True},
            {"requires": "class Board", "from": "game/board"}))
        self.assertEqual(problems, [])
        self.assertEqual(provides, [{"sig": "def score(board: Board) -> int", "entry": False},
                                    {"sig": "def main() -> None", "entry": True}])
        self.assertEqual(requires, [{"sig": "class Board", "from": "game/board"}])

    def test_exactly_one_block_is_required(self):
        text = doc({"provides": "def f() -> int"})
        for bad in ("# 契約が無い\n", text + text):
            with self.subTest(bad=bad[:10]):
                _, _, problems = loop.parse_contract(bad)
                self.assertTrue(problems[0].startswith("has "), problems)

    def test_a_line_that_is_not_a_contract_line_is_named(self):
        _, _, problems = loop.parse_contract(doc(
            {"provides": "def f() -> int", "why": "x"},
            {"requires": "def g() -> int"},
            {"uses": "h"}))
        self.assertEqual(len(problems), 3)
        self.assertIn("contract line 1: a provides line", problems[0])
        self.assertIn("contract line 2: a requires line", problems[1])
        self.assertIn("contract line 3 is neither", problems[2])

    def test_a_line_that_is_not_json_is_named(self):
        text = "## 契約\n```ndjson contract\n{not json}\n```\n"
        _, _, problems = loop.parse_contract(text)
        self.assertIn("contract line 1 is not JSON", problems[0])


class TheAcceptanceCriteria(unittest.TestCase):
    def test_a_complete_set_passes(self):
        criteria, problems = loop.parse_ac(ac(), "ac/m.json")
        self.assertEqual(problems, [])
        self.assertEqual(len(criteria), 3)

    def test_a_missing_or_broken_file_is_named(self):
        self.assertIn("missing", loop.parse_ac(None, "ac/m.json")[1][0])
        self.assertIn("is not JSON", loop.parse_ac("{", "ac/m.json")[1][0])
        self.assertIn('"criteria" list', loop.parse_ac("{}", "ac/m.json")[1][0])

    def test_each_criterion_has_exactly_four_strings(self):
        bad = [dict(CRITERIA[0], extra="x"), *CRITERIA[1:]]
        _, problems = loop.parse_ac(ac(bad), "ac/m.json")
        self.assertTrue(any("criterion 1 must have exactly" in p for p in problems), problems)

    def test_case_and_kind_come_from_closed_lists(self):
        bad = [dict(CRITERIA[0], case="happy", kind="e2e"), *CRITERIA[1:]]
        _, problems = loop.parse_ac(ac(bad), "ac/m.json")
        self.assertTrue(any("case 'happy'" in p for p in problems), problems)
        self.assertTrue(any("kind 'e2e'" in p for p in problems), problems)

    def test_then_must_state_a_concrete_value(self):
        bad = [dict(CRITERIA[0], then="returns the right score"), *CRITERIA[1:]]
        _, problems = loop.parse_ac(ac(bad), "ac/m.json")
        self.assertTrue(any("states no concrete value" in p for p in problems), problems)

    def test_a_shape_checked_by_reflection_is_refused(self):
        bad = [dict(CRITERIA[0], given="typeof(Board)", then="has a static method named 'Score'"),
               *CRITERIA[1:]]
        _, problems = loop.parse_ac(ac(bad), "ac/m.json")
        self.assertTrue(any("through reflection" in p for p in problems), problems)

    def test_every_case_is_present(self):
        _, problems = loop.parse_ac(ac(CRITERIA[:2]), "ac/m.json")
        self.assertIn("ac/m.json: has no criterion of case ['error']", problems)


class TheShapeOfTheDesign(Python):
    def test_a_well_formed_module_passes(self):
        modules, problems = loop.design_problems(
            {"game/board": doc({"provides": "def score() -> int", "entry": True})},
            {"game/board": ac()})
        self.assertEqual(problems, [])
        module = modules["game/board"]
        self.assertEqual(module["files"], ["src/game/board.py"])
        self.assertEqual(module["files_test"], ["tests/test_game_board.py"])

    def test_both_sections_are_required_once(self):
        text = doc({"provides": "def f() -> int"}).replace("## 振る舞い", "## 説明")
        _, problems = loop.design_problems({"m": text}, {"m": ac()})
        self.assertIn("modules/m.md: needs exactly one `## 振る舞い` section", problems)

    def test_the_prose_is_japanese(self):
        text = doc({"provides": "def f() -> int"}, behaviour="Counts the board.")
        text = text.replace("# モジュール", "# Module")
        _, problems = loop.design_problems({"m": text}, {"m": ac()})
        self.assertIn("modules/m.md: is not written in Japanese", problems)

    def test_the_basic_design_has_one_overview_and_japanese_prose(self):
        self.assertEqual(loop.basic_problems({"design": "## 概要\n放置ゲーム。\n"}), [])
        self.assertEqual(loop.basic_problems({"design": "## 概要\nAn idle game.\n"}),
                         ["basic/design.md: is not written in Japanese"])
        self.assertIn("not 2", loop.basic_problems({"a": "## 概要\n一。\n",
                                                    "b": "## 概要\n二。\n"})[0])

    def test_a_module_that_provides_nothing_is_refused(self):
        _, problems = loop.design_problems({"m": doc()}, {"m": ac()})
        self.assertIn("modules/m.md: the contract provides nothing", problems)

    def test_a_path_that_is_not_a_source_file_is_refused(self):
        _, problems = loop.design_problems({"game/my board": doc()}, {})
        self.assertTrue(problems[0].startswith("modules/game/my board.md: the path"), problems)

    def test_criteria_without_a_design_are_named(self):
        _, problems = loop.design_problems(
            {"m": doc({"provides": "def f() -> int"})}, {"m": ac(), "orphan": ac()})
        self.assertIn("ac/orphan.json: there is no modules/orphan.md for it", problems)

    def test_integration_criteria_need_something_to_integrate_with(self):
        mixed = [dict(CRITERIA[0], kind="integration"), *CRITERIA[1:]]
        modules, problems = loop.design_problems(
            {"m": doc({"provides": "def f() -> int"})}, {"m": ac(mixed)})
        self.assertIn("ac/m.json: has integration criteria, but m requires nothing from "
                      "another module", problems)
        self.assertEqual(len(modules["m"]["files_test"]), 2)

    def test_two_modules_whose_tests_share_a_name_are_refused(self):
        with mock.patch.dict(loop.LANGUAGE, loop.LANGUAGES["csharp"], clear=True):
            _, problems = loop.design_problems(
                {"A/Board": doc({"provides": "class Board"}),
                 "B/Board": doc({"provides": "class Board2"})},
                {"A/Board": ac(), "B/Board": ac()})
        self.assertTrue(any("would both have tests named BoardTests.cs" in p
                            for p in problems), problems)

    def test_the_design_hash_covers_the_criteria(self):
        text = doc({"provides": "def f() -> int"})
        one, _ = loop.design_problems({"m": text}, {"m": ac()})
        two, _ = loop.design_problems({"m": text}, {"m": ac(CRITERIA[::-1])})
        self.assertNotEqual(one["m"]["design"], two["m"]["design"])


class TheInterfaceCheck(Python):
    def check(self, docs: dict[str, str]) -> list[str]:
        modules, problems = loop.design_problems(docs, {mid: ac() for mid in docs})
        self.assertEqual(problems, [])
        return loop.interface_problems(modules)

    def test_a_design_that_fits_together_passes(self):
        self.assertEqual(self.check({
            "game/board": doc({"provides": "class Board"}),
            "game/score": doc({"requires": "class Board", "from": "game/board"},
                              {"provides": "def score(board: Board) -> int", "entry": True}),
        }), [])

    def test_a_name_the_module_does_not_provide(self):
        problems = self.check({
            "a": doc({"provides": "class A"}),
            "b": doc({"requires": "class A", "from": "a"},
                     {"requires": "def missing() -> int", "from": "a"},
                     {"provides": "def b() -> int", "entry": True})})
        self.assertIn("存在: b requires `missing` from a, which does not provide it", problems)

    def test_a_signature_that_differs(self):
        problems = self.check({
            "a": doc({"provides": "def f(x: int) -> int"}),
            "b": doc({"requires": "def f(x: str) -> int", "from": "a"},
                     {"provides": "def b() -> int", "entry": True})})
        self.assertTrue(any(p.startswith("署名の一致: b requires `def f(x: str) -> int`")
                            for p in problems), problems)

    def test_a_module_that_is_neither_designed_nor_in_the_code(self):
        problems = self.check({"b": doc({"requires": "class A", "from": "nowhere"},
                                        {"provides": "def b() -> int", "entry": True})})
        self.assertTrue(any(p.startswith("存在: b requires `A` from nowhere, which is neither")
                            for p in problems), problems)

    def test_existing_code_is_found_by_its_file(self):
        with mock.patch.object(loop, "head_files", return_value={"src/old.py"}), \
             mock.patch.object(loop, "existing_declarations", return_value={
                 "src/old.py": ["class Old -- defined in old"]}):
            problems = self.check({"b": doc({"requires": "class Old", "from": "old"},
                                            {"requires": "class Gone", "from": "old"},
                                            {"provides": "def b() -> int", "entry": True})})
        self.assertEqual(problems, ["存在: b requires `Gone` from old, whose code does not "
                                    "declare it"])

    def test_a_cycle(self):
        problems = self.check({
            "a": doc({"provides": "def a() -> int"}, {"requires": "def b() -> int", "from": "b"}),
            "b": doc({"provides": "def b() -> int"}, {"requires": "def a() -> int", "from": "a"})})
        self.assertTrue(any(p.startswith("依存の循環") and "a, b" in p for p in problems),
                        problems)

    def test_a_provides_line_nobody_requires(self):
        problems = self.check({"a": doc({"provides": "def a() -> int"})})
        self.assertTrue(any(p.startswith("未使用: a provides `a`") for p in problems), problems)

    def test_one_name_in_two_places(self):
        problems = self.check({"a": doc({"provides": "def f() -> int", "entry": True}),
                               "b": doc({"provides": "def f() -> int", "entry": True})})
        self.assertIn("居場所: `f` is provided by both a and b", problems)


class TheDownstreamPlan(Python):
    def tasks(self) -> dict:
        modules, problems = loop.design_problems({
            "game/score": doc({"requires": "class Board", "from": "game/board"},
                              {"provides": "def score(board: Board) -> int", "entry": True}),
            "game/board": doc({"provides": "class Board"}, behaviour="盤面を持つ。"),
        }, {"game/score": ac(), "game/board": ac()})
        self.assertEqual(problems, [])
        with mock.patch.object(loop, "basic_overview", return_value="放置ゲーム。"):
            return loop.build_tasks(modules, {"language": "python"})

    def test_dependencies_come_first(self):
        self.assertEqual([s["id"] for s in self.tasks()["steps"]], ["game/board", "game/score"])

    def test_a_step_carries_what_the_downstream_phases_read(self):
        board, score = self.tasks()["steps"]
        self.assertEqual((board["kind"], score["kind"]), ("unit", "integration"))
        self.assertEqual(score["depends_on"], ["game/board"])
        self.assertEqual(score["contracts"]["provides"],
                         ["def score(board: Board) -> int -- defined in game.score"])
        self.assertEqual(score["contracts"]["requires"], ["class Board -- defined in game.board"])
        self.assertEqual(score["files_write"], ["src/game/score.py"])
        self.assertEqual(score["expected_tests"], 3)
        self.assertEqual(score["acceptance"], CRITERIA)

    def test_the_context_carries_the_overview_and_the_dependency_s_behaviour(self):
        context = self.tasks()["steps"][1]["context"]
        self.assertIn("放置ゲーム。", context)
        self.assertIn("### game/board\n盤面を持つ。", context)
        self.assertNotIn("ndjson contract", context)


class Stages(Python):
    """段のコマンド。ファイルは一時ディレクトリに置き、git には触れない。"""

    def setUp(self) -> None:
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.root = root
        self.events: list[str] = []
        for name, value in {
            "PROJECT": root, "PLAN": root / "plan", "STATE": root / ".runner",
            "STAGE_FILE": root / "plan" / "stage.json", "LEDGER": root / "plan" / "ledger.jsonl",
            "INTERFACE": root / "plan" / "INTERFACE.json", "FAILURE": root / "plan" / "FAILURE.md",
            "TASKS": root / ".runner" / "tasks.json", "DOCS_BASIC": root / "docs" / "basic",
            "DOCS_MODULES": root / "docs" / "modules", "AC": root / "ac",
            "commit_paths": lambda *a: None,
            "ledger": lambda event, **f: self.events.append(event),
            "basic_overview": lambda: "",
        }.items():
            p = mock.patch.object(loop, name, value)
            p.start()
            self.addCleanup(p.stop)
        (root / "plan").mkdir()

    def stage(self, **values) -> None:
        loop.write_stage(**values)

    def design(self) -> None:
        loop.write_texts(loop.DOCS_MODULES, {
            "a": doc({"provides": "def a() -> int", "entry": True})}, ".md")
        loop.write_texts(loop.AC, {"a": ac()}, ".json")

    def digest(self) -> str:
        modules, _ = loop.current_design()
        return loop.design_digest(modules)

    def test_a_command_outside_its_stage_is_refused(self):
        self.stage(stage="upstream")
        with mock.patch("sys.stderr"):
            self.assertEqual(loop.cmd_modules_approve(), 1)
        self.assertEqual(loop.read_stage()["stage"], "upstream")

    def test_approving_the_basic_design_moves_to_the_midstream(self):
        self.stage(stage="upstream", language="python")
        loop.write_texts(loop.DOCS_BASIC, {"design": "## 概要\n放置ゲーム。\n"}, ".md")
        self.assertEqual(loop.cmd_basic_approve(), 0)
        self.assertEqual(loop.read_stage()["stage"], "midstream")

    def test_a_basic_design_without_an_overview_is_not_approved(self):
        self.stage(stage="upstream")
        loop.write_texts(loop.DOCS_BASIC, {"design": "放置ゲーム。\n"}, ".md")
        with mock.patch("sys.stderr"):
            self.assertEqual(loop.cmd_basic_approve(), 2)
        self.assertEqual(loop.read_stage()["stage"], "upstream")

    def test_a_design_the_critic_has_not_read_is_not_approved(self):
        self.design()
        self.stage(stage="midstream", language="python", critiqued="another digest")
        with mock.patch("sys.stderr"):
            self.assertEqual(loop.cmd_modules_approve(), 1)
        self.assertFalse(loop.TASKS.exists())

    def test_approving_the_module_design_writes_the_tasks_and_moves_downstream(self):
        self.design()
        self.stage(stage="midstream", language="python", critiqued=self.digest())
        self.assertEqual(loop.cmd_modules_approve(), 0)
        self.assertEqual(loop.read_stage()["stage"], "downstream")
        tasks = json.loads(loop.TASKS.read_text(encoding="utf-8"))
        self.assertEqual([s["id"] for s in tasks["steps"]], ["a"])

    def test_after_a_failure_the_human_chooses_before_the_planner_runs(self):
        self.design()
        self.stage(stage="midstream", language="python", open=[])
        with mock.patch("sys.stderr"):
            self.assertEqual(loop.cmd_modules_draft(), 1)
            self.assertEqual(loop.cmd_modules_approve(), 1)

    def test_reopen_names_the_modules_the_planner_may_change(self):
        self.design()
        self.stage(stage="downstream", language="python")
        with mock.patch.object(loop, "clean_tree"):
            self.assertEqual(loop.cmd_reopen(["a"]), 0)
        self.assertEqual((loop.read_stage()["stage"], loop.read_stage()["open"]),
                         ("midstream", ["a"]))
        with mock.patch.object(loop, "clean_tree"), mock.patch("sys.stderr"):
            self.assertEqual(loop.cmd_reopen(["nope"]), 1)

    def test_a_sent_back_draft_may_change_only_the_open_modules(self):
        out = self.root / "out"
        with mock.patch.object(loop, "PLANNER_OUT", out):
            loop.write_texts(out / "modules", {
                "a": doc({"provides": "def a() -> int", "entry": True}, behaviour="変えた。"),
                "b": doc({"provides": "def b() -> int", "entry": True}, behaviour="変えた。")},
                ".md")
            loop.write_texts(out / "ac", {"a": ac(), "b": ac()}, ".json")
            before = {"a": doc({"provides": "def a() -> int", "entry": True}),
                      "b": doc({"provides": "def b() -> int", "entry": True})}
            problems = loop.check_modules_out(["a"], before, {"a": ac(), "b": ac()})
        self.assertEqual(problems, ["差し戻し: modules/b.md changed, but only a may change"])


class TheRunSettings(unittest.TestCase):
    """上限とソルバーの段は、root の持ち物の settings.json に置く。下流の計画は
    ランナーが設計から作るので、人が書く場所はそこに無い。"""

    def read(self, text: str | None) -> dict:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            if text is not None:
                path.write_text(text, encoding="utf-8")
            with mock.patch.object(loop, "SETTINGS_FILE", path):
                return loop.read_settings()

    def test_no_file_is_no_setting(self):
        self.assertEqual(self.read(None), {})

    def test_the_known_keys_are_read(self):
        value = {"solver_tiers": ["local", "claude"], "limits": {"attempts": 8}}
        self.assertEqual(self.read(json.dumps(value)), value)

    def test_an_unknown_key_or_a_broken_file_stops_the_runner(self):
        for text in ('{"solver_teirs": ["local"]}', "{", "[]"):
            with self.subTest(text=text), self.assertRaises(SystemExit):
                self.read(text)


class WhatThePlannerLeavesBehind(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.out = Path(temp.name)
        p = mock.patch.object(loop, "PLANNER_OUT", self.out)
        p.start()
        self.addCleanup(p.stop)
        (self.out / "modules" / "game").mkdir(parents=True)
        (self.out / "modules" / "game" / "board.md").write_text("x", encoding="utf-8")

    def test_what_belongs_is_kept(self):
        self.assertEqual(loop.prune_planner_out({"modules"}), [])
        self.assertTrue((self.out / "modules" / "game" / "board.md").exists())

    def test_a_part_not_asked_for_and_a_stray_file_are_deleted(self):
        (self.out / "ac").mkdir()
        (self.out / "notes.md").write_text("x", encoding="utf-8")
        notes = loop.prune_planner_out({"modules"})
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["modules"])
        self.assertEqual(len(notes), 2)

    def test_a_file_of_the_wrong_kind_is_deleted(self):
        (self.out / "modules" / "game" / "board.json").write_text("{}", encoding="utf-8")
        notes = loop.prune_planner_out({"modules"})
        self.assertEqual(notes, ["modules/game/board.json is not a .md file; the runner "
                                 "deleted it"])
        self.assertFalse((self.out / "modules" / "game" / "board.json").exists())

    @unittest.skipUnless(os.name == "posix", "symbolic links need privileges on Windows")
    def test_a_symbolic_link_is_deleted_without_being_followed(self):
        target = self.out / "outside.md"
        (self.out / "modules" / "link.md").symlink_to(target)
        notes = loop.prune_planner_out({"modules", "outside.md"})
        self.assertIn("modules/link.md is a symbolic link; the runner deleted it", notes)


if __name__ == "__main__":
    unittest.main()
