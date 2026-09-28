"""計画を別の言語で書くと何が変わり、何が変わらないか。

変える必要のあったものの一覧は短く、それ自体が分かったことだ。関門の算術、
書き込みの柵、台帳、エスカレーションの規則、L14 の語彙以外のすべてのリンタ規則は、
もともと言語に依存していなかった。Python の形をしていたのは表現だけだ。

ここのテストは、変わった4つと、変わらなかったが「誤っていた」1つだ。
parse_junit は最初の <testsuite> を読み、pytest はそれを1つしか書かないので、
suite ではなくレポート全体を読んでいるのかを示すものが何も無かった。

    python3 -m unittest discover -s runner/tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop  # noqa: E402
from loop import (  # noqa: E402
    LANGUAGE, LANGUAGES, failure_kind, load_settings, modules_of, parse_junit,
    test_argv, validate_plan,
)


class Language(unittest.TestCase):
    """ここのテストはどれもモジュールの状態を書くので、どれも元に戻す。"""

    def setUp(self) -> None:
        self.saved = dict(LANGUAGE)

    def tearDown(self) -> None:
        LANGUAGE.clear()
        LANGUAGE.update(self.saved)

    def speak(self, name: str) -> None:
        load_settings({"language": name})


class WhichLanguage(Language):
    def test_a_plan_that_says_nothing_is_python(self):
        # これができる前に書かれた計画はすべて Python を前提にしている。計画は何を
        # 確かめたかの記録で、読み直して意味が変わってはならない。
        load_settings({})
        self.assertEqual(LANGUAGE["source_suffix"], ".py")

    def test_an_unknown_language_is_refused_rather_than_defaulted(self):
        # 黙って既定に落とすと、TypeScript の計画を pytest で、誤った拡張子に対して
        # 確かめ、どの関門も読んでもいないファイルについて自信をもって報告する。
        with self.assertRaises(SystemExit):
            self.speak("js")

    def test_the_three_are_the_whole_list(self):
        self.assertEqual(sorted(LANGUAGES), ["csharp", "python", "typescript"])


class TheCommandThatProducesAVerdict(Language):
    def test_python_runs_pytest_from_the_frozen_venv(self):
        self.speak("python")
        argv, env = test_argv(["tests/test_engine.py"], Path("/tmp/r.xml"))
        self.assertIn(".venv/bin/pytest", argv[0].replace("\\", "/"))
        self.assertIn("--junitxml", argv)
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")

    def test_typescript_runs_the_frozen_vitest_once_and_not_in_watch_mode(self):
        # vitest の既定は対話的だ。永遠に止まったランナーは、終わらないステップと
        # 見分けがつかない。
        self.speak("typescript")
        argv, _ = test_argv(["tests/engine.test.ts"], Path("/tmp/r.xml"))
        self.assertIn("node_modules/.bin/vitest", argv[0].replace("\\", "/"))
        self.assertEqual(argv[1], "run")
        self.assertIn("--reporter=junit", argv)

    def test_neither_reaches_the_test_runner_through_a_fetcher(self):
        # npx は取ってくることも厭わない。どちらも、プロビジョニングが凍結した
        # 木への絶対パスだ。
        for name in ("python", "typescript", "csharp"):
            self.speak(name)
            argv, _ = test_argv([], Path("/tmp/r.xml"))
            # pathlib ではなく POSIX の形で確かめる。ランナーはサンドボックスで
            # しか走らず、Windows の pathlib は /srv/... をドライブ文字が無いので
            # 相対パスと呼ぶ。
            self.assertTrue(argv[0].replace("\\", "/").startswith("/"), argv[0])
            self.assertNotIn("npx", argv[0])


class WhereAThingLives(Language):
    def test_python_spells_it_with_dots(self):
        self.speak("python")
        self.assertEqual(
            modules_of(["src/incgame/engine.py", "src/incgame/__init__.py"]),
            ["incgame.engine", "incgame"])

    def test_typescript_spells_it_with_slashes(self):
        self.speak("typescript")
        self.assertEqual(
            modules_of(["src/idlegame/engine.ts", "src/idlegame/index.ts"]),
            ["idlegame/engine", "idlegame"])

    def test_the_other_language_s_files_are_not_modules(self):
        # .py を挙げた TypeScript の計画はモジュールを書いていない。書いたと言うと、
        # 何も満たせない契約を L15 が通してしまう。
        self.speak("typescript")
        self.assertEqual(modules_of(["src/idlegame/engine.py"]), [])


class ContractsMustStateAShape(Language):
    """L14 を、各言語自身の型の構文で確かめる。規則そのものは変わらない。"""

    def plan(self, provides: str) -> dict:
        return {
            "steps": [{
                "id": "S1", "kind": "skeleton", "goal": "g", "depends_on": [],
                "acceptance": [
                    {"case": "normal", "given": "g", "then": "x == 1"},
                    {"case": "boundary", "given": "g", "then": "x == 0"},
                    {"case": "error", "given": "g", "then": "raises ValueError"},
                ],
                "contracts": {"provides": [provides], "requires": [],
                              "invariants": ["one"]},
                "files_write": ["src/pkg/models" + LANGUAGE["source_suffix"]],
                "files_test": ["tests/models_test" + LANGUAGE["source_suffix"]],
                "expected_tests": 3, "max_attempts": 2, "review_gate": False,
            }],
        }

    def l14(self, provides: str) -> list[str]:
        return [p for p in validate_plan(self.plan(provides)) if p.startswith("L14")]

    def test_a_bare_python_container_is_refused(self):
        self.speak("python")
        self.assertTrue(self.l14("src/pkg/models.py: def catalog() -> dict"))

    def test_a_parameterised_python_container_passes(self):
        self.speak("python")
        self.assertFalse(self.l14("src/pkg/models.py: def catalog() -> dict[str, Spec]"))

    def test_a_bare_typescript_container_is_refused(self):
        self.speak("typescript")
        self.assertTrue(self.l14("src/pkg/models.ts: function catalog(): Record"))

    def test_a_parameterised_typescript_container_passes(self):
        self.speak("typescript")
        self.assertFalse(
            self.l14("src/pkg/models.ts: function catalog(): Record<string, Spec>"))

    def test_each_language_only_knows_its_own_shapeless_names(self):
        # `dict` は TypeScript の型ではなく、`Record` は Python の型ではない。
        # 語彙を1つにすると、両方で正しい契約を拒むことになる。
        self.speak("typescript")
        self.assertFalse(self.l14("src/pkg/models.ts: function f(): dict"))
        self.speak("python")
        self.assertFalse(self.l14("src/pkg/models.py: def f() -> Record"))


class ContractsMustSayWhereAThingLives(Language):
    """L15 と、行がモジュールを名指ししているかを決める境界。"""

    def plan(self, provides: str, source: str) -> dict:
        return {
            "steps": [{
                "id": "S1", "kind": "skeleton", "goal": "g", "depends_on": [],
                "acceptance": [
                    {"case": "normal", "given": "g", "then": "x == 1"},
                    {"case": "boundary", "given": "g", "then": "x == 0"},
                    {"case": "error", "given": "g", "then": "raises ValueError"},
                ],
                "contracts": {"provides": [provides], "requires": [],
                              "invariants": ["one"]},
                "files_write": [source],
                "files_test": ["tests/models_test" + LANGUAGE["source_suffix"]],
                "expected_tests": 3, "max_attempts": 2, "review_gate": False,
            }],
        }

    def l15(self, provides: str, source: str) -> list[str]:
        return [p for p in validate_plan(self.plan(provides, source))
                if p.startswith("L15")]

    def test_a_typescript_path_names_its_own_module(self):
        # モジュール名の後のドットは拡張子で、区切り文字ではない。パスを書いた
        # プランナーに名前を繰り返させると、誤っているのは計画ではなく規則なのに
        # 試行を1回使う。最初の TypeScript の bootstrap の1回目で、まさにそれが
        # 起きた。
        self.speak("typescript")
        self.assertFalse(self.l15(
            "src/idlegame/model.ts :: interface GameState { resource: number }",
            "src/idlegame/model.ts"))

    def test_a_typescript_signature_with_no_module_at_all_is_refused(self):
        self.speak("typescript")
        self.assertTrue(self.l15("interface GameState { resource: number }",
                                 "src/idlegame/model.ts"))

    def test_a_longer_name_does_not_count_as_the_shorter_one(self):
        self.speak("typescript")
        self.assertTrue(self.l15("src/idlegame/models.ts :: interface X",
                                 "src/idlegame/model.ts"))

    def test_python_still_treats_the_dot_as_a_separator(self):
        # incgame.engine.sub の中に現れる incgame.engine は別のモジュールを指す
        # ので、Python の境界にはドットを残す。
        self.speak("python")
        self.assertFalse(self.l15(
            "src/incgame/engine.py: def f() -> int -- defined in incgame.engine",
            "src/incgame/engine.py"))
        self.assertTrue(self.l15("def f() -> int", "src/incgame/engine.py"))


class StampingTheLanguage(Language):
    """計画に言語を書き込むのはランナーで、プランナーは決して書かない。

    この機械がどの言語を持つかは箱の性質で、プロジェクトがどれを使うかは誰かに
    計画を頼む前に決まる。solver_tiers と同じ理屈だ。選べるプランナーは、入って
    いないツールチェーンを選ぶことがある。
    """

    def setUp(self) -> None:
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name)
        self.saved_out = loop.PLANNER_OUT
        loop.PLANNER_OUT = self.out

    def tearDown(self) -> None:
        loop.PLANNER_OUT = self.saved_out
        self.temp.cleanup()
        super().tearDown()

    def test_the_value_lands_in_the_proposal(self):
        (self.out / "tasks.json").write_text('{"version": 1, "steps": []}',
                                             encoding="utf-8")
        loop.stamp_language("typescript")
        written = json.loads((self.out / "tasks.json").read_text(encoding="utf-8"))
        self.assertEqual(written["language"], "typescript")
        self.assertEqual(written["steps"], [])

    def test_an_escalation_leaves_nothing_to_stamp(self):
        # プランナーは計画を書かず ESCALATE.md だけで答えることがある。書き込みで
        # 計画をこしらえてはならない。
        loop.stamp_language("typescript")
        self.assertFalse((self.out / "tasks.json").exists())

    def test_unreadable_json_is_left_alone_rather_than_replaced(self):
        (self.out / "tasks.json").write_text("{ not json", encoding="utf-8")
        loop.stamp_language("typescript")
        self.assertEqual((self.out / "tasks.json").read_text(encoding="utf-8"),
                         "{ not json")


class ReadingTheReport(unittest.TestCase):
    def report(self, body: str) -> loop.TestRun:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "report.xml"
            path.write_text(body, encoding="utf-8")
            return parse_junit(path)

    def test_every_suite_is_counted_not_the_first(self):
        # pytest は実行全体で <testsuite> を1つ書くので、「最初の suite」と
        # 「レポート」は同じもので、見分けられなかった。vitest はファイルごとに
        # 1つ書き、VERIFY は tests/ 全体を渡す。最初の suite だけを読むと、
        # 1ファイルだけを数えて残りを緑と呼ぶ。失敗を少なく数える関門は、関門が
        # 無いより悪い。
        run = self.report(
            '<testsuites tests="4" failures="2">'
            '<testsuite name="a.test.ts" tests="2" failures="0" errors="0" skipped="0">'
            '<testcase classname="a.test.ts" name="one"/>'
            '<testcase classname="a.test.ts" name="two"/>'
            '</testsuite>'
            '<testsuite name="b.test.ts" tests="2" failures="2" errors="0" skipped="0">'
            '<testcase classname="b.test.ts" name="three">'
            '<failure message="expected 0 to be greater than 0" type="AssertionError"/>'
            '</testcase>'
            '<testcase classname="b.test.ts" name="four">'
            '<failure message="nope" type="TypeError"/>'
            '</testcase>'
            '</testsuite></testsuites>')
        self.assertEqual((run.tests, run.failures), (4, 2))
        self.assertEqual(run.failure_kinds, ["AssertionError", "TypeError"])
        self.assertEqual(run.failed_files, ["b.test.ts"])

    def test_a_declared_type_is_believed_over_the_body(self):
        # vitest は設定し、pytest は設定しない。R5 はこの値で決まるので、読み
        # 誤ると、どの赤を赤と数えるかが変わる。
        element = ET.fromstring('<failure message="m" type="TypeError">'
                                'tests/x.py:3: AssertionError</failure>')
        self.assertEqual(failure_kind(element), "TypeError")

    def test_without_a_declared_type_the_body_still_decides(self):
        element = ET.fromstring('<failure message="assert nan == 5.0">'
                                'tests/x.py:16: AssertionError</failure>')
        self.assertEqual(failure_kind(element), "AssertionError")

    def test_a_report_with_no_suite_at_all_is_an_error_not_an_absence(self):
        run = self.report('<testsuites tests="0"></testsuites>')
        self.assertEqual(run.errors, 1)


class AFileThatNeverCompiled(unittest.TestCase):
    """vitest は壊れたファイルを、ファイル名を持つ失敗テスト1件として報告する。

    何かが見分けない限り、それは落ちるテストを1つだけ持つファイルと区別が
    つかない。pytest には相当する場合が無い（壊れたモジュールは収集の <error>
    になる）ので、これは2つ目の言語が来て初めて現れた。
    """

    def report(self, body: str) -> loop.TestRun:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "r.xml"
            path.write_text(body, encoding="utf-8")
            return loop.parse_junit(path)

    def test_a_transform_failure_counts_as_an_error_not_a_test(self):
        run = self.report(
            '<testsuites tests="1" failures="1">'
            '<testsuite name="tests/e.test.ts" tests="1" failures="1" errors="0" skipped="0">'
            '<testcase classname="tests/e.test.ts" name="tests/e.test.ts">'
            '<failure message="Transform failed with 1 error: PARSE_ERROR"/>'
            '</testcase></testsuite></testsuites>')
        self.assertEqual((run.tests, run.failures, run.errors), (0, 0, 1))
        self.assertIn("tests/e.test.ts", run.failed_files)
        self.assertTrue(any("did not compile" in k for k in run.failure_kinds))

    def test_a_real_failing_test_is_untouched(self):
        # 目印は name == classname だ。本物のテストは describe/it の名前を持つので、
        # これに巻き込まれてはならない。
        run = self.report(
            '<testsuites tests="1" failures="1">'
            '<testsuite name="tests/e.test.ts" tests="1" failures="1" errors="0" skipped="0">'
            '<testcase classname="tests/e.test.ts" name="engine &gt; adds one">'
            '<failure message="expected 0 to be 1" type="AssertionError"/>'
            '</testcase></testsuite></testsuites>')
        self.assertEqual((run.tests, run.failures, run.errors), (1, 1, 0))
        self.assertEqual(run.failure_kinds, ["AssertionError"])


class TheTestWritingBrief(Language):
    def test_typescript_is_warned_about_apostrophes_in_names(self):
        # 条件は文章で、文章にはアポストロフィがある。それを単一引用符のテスト名に
        # 写すと文字列が終わり、ファイル全体がコンパイルできなくなる。run 8 の S1 は
        # それでテスト12件すべてを失った。
        self.speak("typescript")
        self.assertIn("DOUBLE quotes", loop.naming_note())

    def test_python_is_not_told_about_a_trap_it_does_not_have(self):
        self.speak("python")
        self.assertEqual(loop.naming_note(), "")


class TheStubBrief(unittest.TestCase):
    def test_a_boolean_has_no_wrong_value_and_the_brief_says_so(self):
        # 値は2つで、正しい実装はどちらもどこかの入力に対して返すので、「正しい
        # 型の間違った値」が存在しない。ブリーフがそれを認めるまで、run 8 の S1 は
        # canAfford で R4 に2回続けて拒まれた。
        step = {"files_write": ["src/pkg/engine.ts"],
                "contracts": {"provides": [
                    "src/pkg/engine.ts: function canAfford(s: S, c: C, id: string): boolean"]}}
        brief = loop.brief_stub(step)
        self.assertIn("THERE IS NO WRONG BOOLEAN", brief)
        self.assertIn('return "__stub__" as unknown as boolean', brief)
        # キャストそのものは要点ではない。このブリーフの最初の版に対しては
        # `false as unknown as boolean` が返ってきた。それはやはり false だ。
        self.assertIn("casting alone does nothing", brief)
        # そして、R5 が求めるアサーションの失敗を、なお求めていなければならない
        self.assertIn("assertion failure", brief)


class RewritingTestsThatDoNotCompile(unittest.TestCase):
    """TEST_WRITE をやり直す唯一の理由と、それが唯一である理由。

    構文エラーはソルバーの誤りで、ほかの誰にも直せない。プランナーは直せず、
    エスカレーションすれば、それを言われるために28分の呼び出しを使う。run 8 の
    S1 はそれを2回やった。落ちるだけのテストや、スタブに対して通るテストは、
    「頼んだ」内容の問題で、もう一度頼んでも同じ誤解を引き直すために払うだけだ。
    """

    def test_the_second_brief_carries_the_compiler_output(self):
        # 前もって与えた警告より価値がある。ブリーフはすでに、単一引用符の
        # テスト名にアポストロフィを入れるなと言っていたが、ソルバーはそれでも
        # そうした。誤りの前に読んだ規則はブリーフのほかのすべてと競り合い、
        # エラーは誤りの後に、それだけで届く。
        section = loop.compile_failure_section(
            "<did not compile: tests/engine.test.ts>" + chr(10) * 2
            + "PARSE_ERROR at 58:92")
        self.assertIn("DID NOT COMPILE", section)
        self.assertIn("58:92", section)
        self.assertIn("check every other line", section)

    def test_a_first_attempt_carries_no_such_section(self):
        self.assertEqual(loop.compile_failure_section(""), "")

    def test_the_colour_codes_are_stripped(self):
        # vitest は変換エラーに色を付け、そのエスケープのせいで、引用した先の
        # どこでも文が読めなくなる。
        coloured = chr(27) + "[31m" + "PARSE_ERROR" + chr(27) + "[0m"
        self.assertEqual(loop.ANSI.sub("", coloured), "PARSE_ERROR")

    def test_the_codes_that_lost_their_escape_in_the_xml_are_stripped_too(self):
        # JUnit の XML に ESC は書けないので、vitest は ESC だけを落とす。
        # Claude で回した run 8 の S1 では、ソルバーがこの切れ端を6回渡された。
        coloured = "[38;5;249ma[0m[38;5;249ms[0m"
        self.assertEqual(loop.ANSI.sub("", coloured), "as")
        # 数字の無い `[m` は、コードの一部かもしれないので残す。
        self.assertEqual(loop.ANSI.sub("", "arr[m]"), "arr[m]")


class TellingTheSolverWhatFailed(unittest.TestCase):
    """ソルバーに渡すのはアサーションで、ソルバーが開けないファイルのパスではない。

    `last_failure` をランナーの標準出力から取ると、pytest が失敗をそこに出す
    からこそ動く。vitest の junit の報告はレポートのパスしか出さず、レポートは
    0700 runner の .runner の下にある。だから TypeScript では、ソルバーはファイルに
    失敗があることだけを伝えられ、どれが、なぜかは伝えられなかった。そして2つの
    別のバックエンドが、同じ誤りを6回続けた。
    """

    def report(self, body):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "r.xml"
            path.write_text(body, encoding="utf-8")
            return loop.parse_junit(path)

    def test_the_assertion_comes_back_with_the_test_name(self):
        run = self.report(
            '<testsuites tests="1" failures="1">'
            '<testsuite name="tests/e.test.ts" tests="1" failures="1" errors="0" skipped="0">'
            '<testcase classname="tests/e.test.ts" name="engine &gt; tick with dt 0">'
            '<failure message="expected +0 to be 7" type="AssertionError"/>'
            '</testcase></testsuite></testsuites>')
        self.assertEqual(len(run.failure_details), 1)
        self.assertIn("tick with dt 0", run.failure_details[0])
        self.assertIn("expected +0 to be 7", run.failure_details[0])

    def test_a_passing_run_carries_no_details(self):
        run = self.report(
            '<testsuites tests="1" failures="0">'
            '<testsuite name="tests/e.test.ts" tests="1" failures="0" errors="0" skipped="0">'
            '<testcase classname="tests/e.test.ts" name="engine &gt; ok"/>'
            '</testsuite></testsuites>')
        self.assertEqual(run.failure_details, [])


class TheRunnerWritesTheStub(Language):
    """8回の run のあいだ、判断の要らないものをモデルに頼んでいた。

    スタブの仕事は1つ（形は正しく、値は間違っている）で、contracts.provides が
    すでに署名、すべての型の形、それぞれの置き場を運んでいる。L14 と L15 は
    それを保証するためにある。頼んだ結果、run 8 の S1 は、条件に正しく答える
    スタブのせいで RED_GATE で4回拒まれた。
    """

    S1 = {
        "files_write": ["src/idlegame/model.ts", "src/idlegame/engine.ts"],
        "contracts": {"provides": [
            "src/idlegame/model.ts: interface GameState { resource: number; generators: Record<string, number>; lastUpdate: number }",
            "src/idlegame/model.ts: type Catalog = Record<string, GeneratorDef>",
            "src/idlegame/model.ts: interface GeneratorDef { id: string; name: string; baseCost: number; costMultiplier: number; rate: number }",
            "src/idlegame/engine.ts: function createGame(): GameState",
            "src/idlegame/engine.ts: function canAfford(s: GameState, c: Catalog, id: string): boolean",
            "src/idlegame/engine.ts: function generatorCost(d: GeneratorDef, owned: number): number",
        ]},
    }

    def build(self):
        self.speak("typescript")
        return loop.generate_stub(self.S1, [])

    def test_every_declaration_is_exported(self):
        # ソルバーが忘れ続けたもの。システムプロンプトは Python を書いていると
        # 伝えていた。Python では宣言は書いたまま import できる。
        files = self.build()
        for text in files.values():
            for line in text.splitlines():
                if line.startswith(("interface", "type", "function", "const")):
                    self.fail("not exported: " + line)

    def test_a_boolean_returns_something_that_is_neither(self):
        # 間違った値を持たない唯一の型。`toBe(true)` と `toBe(false)` の両方が
        # 落ちなければならない。そうでないと、条件の1つがスタブに対して通り、
        # ステップが止まる。
        engine = self.build()["src/idlegame/engine.ts"]
        self.assertIn('return "__stub__" as unknown as boolean;', engine)

    def test_a_number_stays_a_number(self):
        # ここで文字列を返すと、アサーションの失敗が TypeError に変わり、
        # RED_GATE はそれをそのまま拒む（R5）。
        self.assertIn("return -999999;", self.build()["src/idlegame/engine.ts"])

    def test_a_named_type_is_built_from_its_own_fields(self):
        engine = self.build()["src/idlegame/engine.ts"]
        self.assertIn("resource: -999999", engine)
        self.assertIn("lastUpdate: -999999", engine)

    def test_nothing_returns_a_default_value(self):
        # このすべてのきっかけになった拒否。`{resource: 0, generators: {},
        # lastUpdate: 0}` は正しい初期状態で、createGame についての条件がスタブに
        # 対して通ってしまう。
        engine = self.build()["src/idlegame/engine.ts"]
        for wrong in ("return 0;", "return true;", "return false;", 'return "";',
                      "return {};", "return [];", "return null;"):
            self.assertNotIn(wrong, engine)

    def test_a_type_from_an_earlier_step_is_expanded_not_cast(self):
        # S2 は `const CATALOG: Catalog` を提供し、Catalog は S1 の
        # Record<string, GeneratorDef> だ。S1 の行が無いとその名前は分からず、番兵の
        # 値はキャストに落ち、CATALOG['cursor'].baseCost を読む最初のテストは
        # アサーションの失敗ではなく TypeError を受け取る。RED_GATE はそれをそのまま
        # 拒む。テストは確かめる前に値を分解するので、番兵の値は「形」を運ばなければ
        # ならない。
        self.speak("typescript")
        stub = loop.generate_stub(
            {"files_write": ["src/idlegame/catalog.ts"],
             "contracts": {"provides": ["src/idlegame/catalog.ts: const CATALOG: Catalog"]}},
            ["src/idlegame/model.ts: interface GeneratorDef { id: string; baseCost: number }",
             "src/idlegame/model.ts: type Catalog = Record<string, GeneratorDef>"])
        text = stub["src/idlegame/catalog.ts"]
        self.assertIn("baseCost: -999999", text)
        self.assertNotIn("as unknown as Catalog", text)

    def test_a_keyed_container_holds_the_keys_the_criteria_look_up(self):
        # cursor が無いと、`CATALOG.cursor.rate` は比較の前に例外を投げ、
        # RED_GATE は例外を投げた呼び出しを R5 で拒む。契約はどのキーかを言えない
        # （Record<string, GeneratorDef> は型であって名簿ではない）が、条件は
        # キーを挙げ、条件はランナーが読んでよいものだ。モデルには何も見せない
        # ので、そこから決め打ちされるものも無い。ランナーが名前を取り、番兵の値で
        # 埋める。
        self.speak("typescript")
        stub = loop.generate_stub(
            {"files_write": ["src/idlegame/catalog.ts"],
             "contracts": {"provides": ["src/idlegame/catalog.ts: const CATALOG: Catalog"]},
             "acceptance": [{"given": "CATALOG.cursor.rate",
                             "then": "deep-equals exactly ['cursor','farm']"}]},
            ["src/idlegame/model.ts: interface GeneratorDef { rate: number }",
             "src/idlegame/model.ts: type Catalog = Record<string, GeneratorDef>"])
        text = stub["src/idlegame/catalog.ts"]
        self.assertIn('"cursor": { rate: -999999 }', text)
        self.assertIn('"farm":', text)
        # そして __stub__ は残す。そうでないと、コンテナのキーについての
        # アサーションが「一致」する。スタブに対して通るテストはステップを止める。
        self.assertIn('"__stub__":', text)

    def test_one_unreadable_inherited_line_does_not_lose_the_others(self):
        # 既存のコードの行には、クラスやアロー関数の const のように、この形に
        # 合わないものがある。まとめて読むと、その1行で Catalog の中身が消え、
        # 番兵の値はキャストに落ちる。
        self.speak("typescript")
        stub = loop.generate_stub(
            {"files_write": ["src/idlegame/catalog.ts"],
             "contracts": {"provides": ["src/idlegame/catalog.ts: const CATALOG: Catalog"]}},
            ["src/legacy/engine.ts: class Engine { tick(dt: number): void }",
             "src/idlegame/model.ts: interface GeneratorDef { id: string; baseCost: number }",
             "src/idlegame/model.ts: type Catalog = Record<string, GeneratorDef>"])
        text = stub["src/idlegame/catalog.ts"]
        self.assertIn("baseCost: -999999", text)
        self.assertNotIn("as unknown as Catalog", text)

    def test_what_it_cannot_parse_goes_back_to_the_solver(self):
        # None は恥ずべき失敗ではない。コンパイルできない生成スタブは、置き換える
        # 問題より悪い。
        self.speak("typescript")
        self.assertIsNone(loop.generate_stub(
            {"files_write": ["src/a.ts"],
             "contracts": {"provides": ["src/a.ts: something in prose"]}}, []))

    def test_a_method_in_an_interface_becomes_a_callable_not_a_field(self):
        # Claude で回した run 8 の S1。メソッドの引数 `listener` をフィールドと
        # 取り違え、`): () => void` を型の位置に残した。スタブがコンパイルせず、
        # ソルバーはテストしか書き直せないので、6回続けて落ちた。
        self.speak("typescript")
        stub = loop.generate_stub(
            {"files_write": ["src/idle/store.ts"],
             "contracts": {"provides": [
                 "src/idle/store.ts: interface Store { getState(): GameState; "
                 "setState(state: GameState): void; "
                 "subscribe(listener: (state: GameState) => void): () => void }",
                 "src/idle/store.ts: function createStore(initial: GameState): Store"]}},
            ["src/idle/state.ts: interface GameState { resource: number }"])
        text = stub["src/idle/store.ts"]
        self.assertNotIn("listener:", text.split("function createStore")[1])
        self.assertIn("subscribe: ((..._args: unknown[]) => (((..._args: unknown[]) => (undefined))))",
                      text)
        self.assertIn("getState: ((..._args: unknown[]) => ({ resource: -999999 }))", text)
        self.assertIn("setState: ((..._args: unknown[]) => (undefined))", text)

    def test_a_function_type_is_split_at_its_own_parenthesis(self):
        self.assertEqual(loop.split_function_type("(l: (s: S) => void) => () => void"),
                         ("l: (s: S) => void", "() => void"))
        self.assertIsNone(loop.split_function_type("(A | B)[]"))

    def test_python_is_left_alone(self):
        self.speak("python")
        self.assertIsNone(loop.generate_stub(
            {"files_write": ["src/pkg/a.py"],
             "contracts": {"provides": ["src/pkg/a.py: def f() -> int"]}}, []))


EXISTING_TS = '''import { Board } from "./board";

/** 盤面の点数。 */
export function score(board: Board): number {
  return board.cells.length;
}

function helper(n: number): number {
  return n + 1;
}

export const LIMIT = 10;
'''


class TheStubKeepsTheExistingCode(Language):
    """既存のファイルでは、provides の名前だけをスタブに差し替える。

    スタブはファイルを丸ごと書き直していた。既存のファイルを files_write に
    入れると、ステップに関係の無い関数まで消える。
    """

    STEP = {"files_write": ["src/game/rules.ts"],
            "contracts": {"provides": [
                "src/game/rules.ts: function score(board: Board): number",
                "src/game/rules.ts: function bonus(board: Board): number"]}}

    def build(self, original=EXISTING_TS):
        self.speak("typescript")
        return loop.generate_stub(
            self.STEP, ["src/game/board.ts: interface Board { cells: number[] }"],
            {"src/game/rules.ts": original})["src/game/rules.ts"]

    def test_only_the_provided_declaration_is_replaced(self):
        text = self.build()
        self.assertIn("function helper(n: number): number {\n  return n + 1;\n}", text)
        self.assertIn("export const LIMIT = 10;", text)
        self.assertIn("/** 盤面の点数。 */", text)
        self.assertNotIn("board.cells.length", text)
        self.assertIn("export function score(board: Board): number {\n  return -999999;\n}",
                      text)

    def test_a_name_the_file_lacks_goes_at_the_end(self):
        text = self.build()
        self.assertTrue(text.rstrip().endswith(
            "export function bonus(board: Board): number {\n  return -999999;\n}"))

    def test_a_name_already_imported_is_not_imported_again(self):
        # 同じ名前を2度 import すると、esbuild はファイルごと拒む。
        self.assertEqual(self.build().count("import { Board }"), 1)

    def test_a_name_not_yet_imported_is_imported(self):
        text = self.build(EXISTING_TS.replace('import { Board } from "./board";\n', ""))
        self.assertTrue(text.startswith('import { Board } from "./board";'))

    def test_the_result_passes_the_runner_s_own_check(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "src/game/rules.ts"
            path.parent.mkdir(parents=True)
            path.write_text(self.build(), encoding="utf-8")
            with patch.object(loop, "PROJECT", Path(temp)):
                self.assertEqual(loop.stub_kept_the_rest(
                    self.STEP, {"src/game/rules.ts": EXISTING_TS}), [])

    def test_one_statement_declaring_two_names_goes_back_to_the_solver(self):
        self.speak("typescript")
        step = {"files_write": ["src/m.ts"],
                "contracts": {"provides": ["src/m.ts: const A: number",
                                           "src/m.ts: const B: number"]}}
        self.assertIsNone(loop.generate_stub(
            step, [], {"src/m.ts": "export const A = 1, B = 2;\n"}))

    def test_a_new_file_is_written_whole_as_before(self):
        self.speak("typescript")
        text = loop.generate_stub(
            {"files_write": ["src/m.ts"],
             "contracts": {"provides": ["src/m.ts: function f(): number"]}},
            [], {})["src/m.ts"]
        self.assertEqual(text, "export function f(): number {\n  return -999999;\n}\n")

    def test_the_spans_point_at_the_statements(self):
        text = "// head\nexport const A = 1;\n\nexport function f() {\n  return 1;\n}\n"
        spans = loop.ts_statement_spans(text)
        self.assertEqual([text[a:b] for a, b, _ in spans],
                         ["export const A = 1;", "export function f() {\n  return 1;\n}"])


PYTHON_BEFORE = '''"""ルール。"""
from game.board import Board

LIMIT = 10


def score(board: Board) -> int:
    return len(board.cells)


def helper(n: int) -> int:
    return n + 1
'''


class TheRunnerChecksWhatTheStubLeftAlone(unittest.TestCase):
    """ソルバーが書いたスタブでも、provides の外が HEAD と同じかを確かめる。

    消えた関数は、それを使う既存のテストを VERIFY で落とす。そのときには、どの
    位相の誰が消したのかがもう分からない。
    """

    STEP = {"contracts": {"provides": ["def score(board: Board) -> int -- defined in game.rules"]}}

    def check(self, after, path="src/game/rules.py", before=PYTHON_BEFORE, step=None):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / path
            target.parent.mkdir(parents=True)
            target.write_text(after, encoding="utf-8")
            with patch.object(loop, "PROJECT", Path(temp)):
                return loop.stub_kept_the_rest(step or self.STEP, {path: before})

    def test_replacing_only_the_provided_body_passes(self):
        after = PYTHON_BEFORE.replace("return len(board.cells)", "return -999999")
        self.assertEqual(self.check(after), [])

    def test_whitespace_comments_and_a_new_import_are_not_changes(self):
        after = ("from game.extra import Extra\n"
                 + PYTHON_BEFORE.replace("def helper(n: int) -> int:",
                                         "# 補助\ndef helper(n: int)  ->  int:"))
        self.assertEqual(self.check(after), [])

    def test_a_changed_function_outside_the_step_is_named(self):
        after = PYTHON_BEFORE.replace("return n + 1", "return n + 2")
        self.assertEqual(self.check(after),
                         ["src/game/rules.py: changed or removed: def helper(n: int) -> int:"])

    def test_a_removed_function_is_named(self):
        after = PYTHON_BEFORE.split("def helper")[0]
        self.assertEqual(len(self.check(after)), 1)
        self.assertIn("def helper", self.check(after)[0])

    def test_a_file_that_no_longer_parses_is_reported(self):
        self.assertEqual(self.check("def score(:\n"), ["src/game/rules.py: no longer parses"])

    def test_a_name_that_shared_a_statement_with_the_provided_one_is_not_lost(self):
        # `const A = 1, C = 2` の A だけを差し替えると C が消える。文を A のものと
        # して飛ばすと、それを見逃す。
        step = {"contracts": {"provides": ["src/m.ts: const A: number"]}}
        problems = self.check("export const A: number = -999999;\n", "src/m.ts",
                              "export const A = 1, C = 2;\n", step)
        self.assertEqual(problems, ["src/m.ts: changed or removed: export const A = 1, C = 2"])

    def test_typescript_is_checked_the_same_way(self):
        step = {"contracts": {"provides": ["src/game/rules.ts: function score(board: Board): number"]}}
        after = EXISTING_TS.replace("return n + 1;", "return n;")
        problems = self.check(after, "src/game/rules.ts", EXISTING_TS, step)
        self.assertEqual(len(problems), 1)
        self.assertIn("function helper(n: number): number", problems[0])


class TheSolverIsToldTheFileExists(unittest.TestCase):
    STEP = {"files_write": ["src/game/rules.py"],
            "contracts": {"provides": ["def score(board: Board) -> int -- defined in game.rules"]}}

    def test_an_existing_file_is_named_with_what_to_leave_alone(self):
        brief = loop.brief_stub(self.STEP, ["src/game/rules.py"])
        self.assertIn("# These files already exist\nsrc/game/rules.py", brief)
        self.assertIn("Leave every other line exactly", brief)
        self.assertIn("stops the step", brief)

    def test_a_step_with_only_new_files_is_not_told(self):
        self.assertNotIn("already exist", loop.brief_stub(self.STEP))


class TheExistingCodeReachesTheSolver(unittest.TestCase):
    """requires が名指しした既存の宣言は、依存先の契約と一緒にソルバーへ届く。

    L3 が認めても、ブリーフに行が無ければ、テストを書くソルバーは import する
    モジュールも署名も知らない。行は HEAD から読んだものを渡す。
    """

    EXISTING = ["def score(board: Board) -> int -- defined in legacy.rules",
                "def unused() -> None -- defined in legacy.rules"]

    def step(self, requires):
        return {"id": "S2", "depends_on": ["S1"],
                "contracts": {"requires": requires, "provides": []}}

    def run_with(self, requires, call):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            (state / "contracts").mkdir()
            (state / "contracts" / "S1.json").write_text(
                '{"provides": ["def new_game() -> Game -- defined in pkg.game"]}',
                encoding="utf-8")
            with patch.object(loop, "STATE", state), \
                 patch("loop.existing_contracts", return_value=self.EXISTING) as found:
                return call(self.step(requires)), found

    def test_the_brief_names_the_existing_declaration_it_requires(self):
        text, _ = self.run_with(["def new_game() -> Game", "def score(board: Board) -> int"],
                                loop.dep_contracts)
        self.assertIn("From S1:", text)
        self.assertIn("From the code already in the repository:\n"
                      "def score(board: Board) -> int -- defined in legacy.rules", text)
        # 求めていない宣言は渡さない。
        self.assertNotIn("unused", text)

    def test_the_stub_gets_the_same_lines(self):
        lines, _ = self.run_with(["def score(board: Board) -> int"], loop.dep_contract_lines)
        self.assertEqual(lines, ["def new_game() -> Game -- defined in pkg.game",
                                 "def score(board: Board) -> int -- defined in legacy.rules"])

    def test_git_is_not_asked_when_the_dependencies_suffice(self):
        text, found = self.run_with(["def new_game() -> Game"], loop.dep_contracts)
        self.assertNotIn("already in the repository", text)
        found.assert_not_called()


class APhaseHasToProduceWhatItWasAskedFor(unittest.TestCase):
    """許可リストの内側に収まっているかと、頼んだものがそろっているかは別の問いだ。

    assert_touched は、許可リストの外に何かが書かれたかを訊く。run 8 の S2 では、
    TEST_WRITE が何も書かずに戻り、solver が書いてはいけない場所に書いていな
    かったので、位相は ok=True と記録された。その後 RED_GATE が空のレポートを
    読んでエラーと呼んだ。原因から2位相離れた場所だった。
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.saved = loop.PROJECT
        loop.PROJECT = Path(self.temp.name)

    def tearDown(self) -> None:
        loop.PROJECT = self.saved
        self.temp.cleanup()

    def test_a_missing_file_halts(self):
        with self.assertRaises(loop.Halt):
            loop.assert_written("TEST_WRITE", ["tests/catalog.test.ts"])

    def test_an_empty_file_halts(self):
        path = loop.PROJECT / "tests" / "catalog.test.ts"
        path.parent.mkdir(parents=True)
        path.write_text("", encoding="utf-8")
        with self.assertRaises(loop.Halt):
            loop.assert_written("TEST_WRITE", ["tests/catalog.test.ts"])

    def test_a_written_file_passes(self):
        path = loop.PROJECT / "tests" / "catalog.test.ts"
        path.parent.mkdir(parents=True)
        path.write_text("it(\"x\", () => {});", encoding="utf-8")
        loop.assert_written("TEST_WRITE", ["tests/catalog.test.ts"])


if __name__ == "__main__":
    unittest.main()
