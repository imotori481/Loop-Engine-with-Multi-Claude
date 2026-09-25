"""What changes when the plan is written in the other language, and what does not.

The list of things that had to change is short, and that is the finding: the
gates' arithmetic, the write fence, the ledger, the escalation rules and every
linter rule but L14's vocabulary were language-independent already. Only their
Python-shaped expression was not.

The tests here are the four that did change, plus the one that did not change
but was WRONG -- parse_junit read the first <testsuite> and pytest only ever
writes one, so nothing had ever shown that it was reading a suite rather than a
report.

    python3 -m unittest discover -s runner/tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop  # noqa: E402
from loop import (  # noqa: E402
    LANGUAGE, LANGUAGES, failure_kind, load_settings, modules_of, parse_junit,
    test_argv, validate_plan,
)


class Language(unittest.TestCase):
    """Every test here writes module state, so every test here puts it back."""

    def setUp(self) -> None:
        self.saved = dict(LANGUAGE)

    def tearDown(self) -> None:
        LANGUAGE.clear()
        LANGUAGE.update(self.saved)

    def speak(self, name: str) -> None:
        load_settings({"language": name})


class WhichLanguage(Language):
    def test_a_plan_that_says_nothing_is_python(self):
        # Every plan written before this existed assumed it, and a plan is a
        # record of what was checked -- re-reading one must not change what it
        # meant.
        load_settings({})
        self.assertEqual(LANGUAGE["source_suffix"], ".py")

    def test_an_unknown_language_is_refused_rather_than_defaulted(self):
        # Silently falling back would check a TypeScript plan with pytest,
        # against the wrong suffixes, and every gate would report confidently
        # about files it never read.
        with self.assertRaises(SystemExit):
            self.speak("js")

    def test_the_two_are_the_whole_list(self):
        self.assertEqual(sorted(LANGUAGES), ["python", "typescript"])


class TheCommandThatProducesAVerdict(Language):
    def test_python_runs_pytest_from_the_frozen_venv(self):
        self.speak("python")
        argv, env = test_argv(["tests/test_engine.py"], Path("/tmp/r.xml"))
        self.assertIn(".venv/bin/pytest", argv[0].replace("\\", "/"))
        self.assertIn("--junitxml", argv)
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")

    def test_typescript_runs_the_frozen_vitest_once_and_not_in_watch_mode(self):
        # vitest's default is interactive. A runner that blocks forever is
        # indistinguishable from a step that never finishes.
        self.speak("typescript")
        argv, _ = test_argv(["tests/engine.test.ts"], Path("/tmp/r.xml"))
        self.assertIn("node_modules/.bin/vitest", argv[0].replace("\\", "/"))
        self.assertEqual(argv[1], "run")
        self.assertIn("--reporter=junit", argv)

    def test_neither_reaches_the_test_runner_through_a_fetcher(self):
        # npx would be willing to download one. Both are absolute paths into a
        # tree provisioning froze.
        for name in ("python", "typescript"):
            self.speak(name)
            argv, _ = test_argv([], Path("/tmp/r.xml"))
            # Spelled against POSIX rather than pathlib: the runner only ever
            # runs on the sandbox, and on Windows pathlib calls /srv/... relative
            # because it carries no drive letter.
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
        # A TypeScript plan that lists a .py has not written a module, and
        # saying it did would let L15 pass a contract nothing can satisfy.
        self.speak("typescript")
        self.assertEqual(modules_of(["src/idlegame/engine.py"]), [])


class ContractsMustStateAShape(Language):
    """L14, in each language's own type syntax. The rule does not change."""

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
        # `dict` is not a TypeScript type and `Record` is not a Python one.
        # Sharing one vocabulary would reject valid contracts in both.
        self.speak("typescript")
        self.assertFalse(self.l14("src/pkg/models.ts: function f(): dict"))
        self.speak("python")
        self.assertFalse(self.l14("src/pkg/models.py: def f() -> Record"))


class ContractsMustSayWhereAThingLives(Language):
    """L15, and the boundary that decides whether a line names a module."""

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
        # The dot after the module name is a file extension, not a separator.
        # Requiring the planner to repeat the name after writing the path
        # spends an attempt on a rule that is wrong rather than a plan that is
        # -- which is what happened on attempt 1 of the first TypeScript
        # bootstrap.
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
        # incgame.engine appearing inside incgame.engine.sub names a different
        # module, so the dot stays in Python's boundary.
        self.speak("python")
        self.assertFalse(self.l15(
            "src/incgame/engine.py: def f() -> int -- defined in incgame.engine",
            "src/incgame/engine.py"))
        self.assertTrue(self.l15("def f() -> int", "src/incgame/engine.py"))


class StampingTheLanguage(Language):
    """The runner writes the language into the plan; the planner never does.

    Which languages this machine has is a property of the box, and which one a
    project uses is decided before anyone is asked for a plan -- the same
    argument as solver_tiers. A planner that could choose would sometimes
    choose the toolchain that is not installed.
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
        # The planner may answer with ESCALATE.md and no plan at all. Stamping
        # must not invent one.
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
        # pytest writes one <testsuite> for a whole run, so "the first suite"
        # and "the report" were the same thing and nothing could tell them
        # apart. vitest writes one per FILE, and VERIFY hands over all of
        # tests/ -- so the old reading would have counted one file and called
        # the rest green. A gate that under-counts failures is worse than none.
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
        # vitest sets it; pytest never does. R5 turns on this value, so reading
        # it wrong changes which reds count as red.
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
    """vitest reports a broken file as one failing test named after the file.

    Which is indistinguishable from a file holding one failing test, unless
    something looks. pytest has no equivalent case -- a broken module is a
    collection <error> -- so this could only appear once a second language did.
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
        # The signature is name == classname. A real test has a describe/it
        # name, so it must not be swept up by this.
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
        # The criteria are prose and prose has apostrophes. Copying one into a
        # single-quoted test name ends the string, and the whole file stops
        # compiling. Run 8's S1 lost all twelve tests to it.
        self.speak("typescript")
        self.assertIn("DOUBLE quotes", loop.naming_note())

    def test_python_is_not_told_about_a_trap_it_does_not_have(self):
        self.speak("python")
        self.assertEqual(loop.naming_note(), "")


class TheStubBrief(unittest.TestCase):
    def test_a_boolean_has_no_wrong_value_and_the_brief_says_so(self):
        # Two values, and a correct implementation returns each of them for
        # some input, so "a wrong value of the right type" does not exist.
        # Run 8's S1 was rejected twice in a row by R4 on canAfford before the
        # brief admitted this.
        step = {"files_write": ["src/pkg/engine.ts"],
                "contracts": {"provides": [
                    "src/pkg/engine.ts: function canAfford(s: S, c: C, id: string): boolean"]}}
        brief = loop.brief_stub(step)
        self.assertIn("THERE IS NO WRONG BOOLEAN", brief)
        self.assertIn('return "__stub__" as unknown as boolean', brief)
        # The cast alone is not the point: the first version of this brief came
        # back as `false as unknown as boolean`, which is still false.
        self.assertIn("casting alone does nothing", brief)
        # and it must still insist on the assertion failure R5 requires
        self.assertIn("assertion failure", brief)


class RewritingTestsThatDoNotCompile(unittest.TestCase):
    """The one thing TEST_WRITE is retried for, and the reason it is the one.

    A syntax error is the solver's mistake and nobody else can repair it. The
    planner cannot fix one, and escalating it spends a twenty-eight minute call
    to be told so -- run 8's S1 did that twice. A test that merely fails, or
    passes against the stub, is about what was ASKED for, and asking again
    would pay to sample the same misunderstanding.
    """

    def test_the_second_brief_carries_the_compiler_output(self):
        # Worth more than the warning that preceded it: the brief had already
        # said not to put an apostrophe in a single-quoted test name, and the
        # solver did it anyway. A rule read before the mistake competes with
        # everything else in the brief; the error arrives after it, alone.
        section = loop.compile_failure_section(
            "<did not compile: tests/engine.test.ts>" + chr(10) * 2
            + "PARSE_ERROR at 58:92")
        self.assertIn("DID NOT COMPILE", section)
        self.assertIn("58:92", section)
        self.assertIn("check every other line", section)

    def test_a_first_attempt_carries_no_such_section(self):
        self.assertEqual(loop.compile_failure_section(""), "")

    def test_the_colour_codes_are_stripped(self):
        # vitest colours its transform errors, and the escapes make the message
        # unreadable everywhere it is quoted back.
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
    """The assertion, not the path of a file the solver cannot open.

    `last_failure` came from the runner's stdout, which works only because
    pytest prints failures there. vitest's junit reporter prints the path of
    the report and nothing else, and the report lives under .runner at 0700
    runner. So under TypeScript the solver was told a file had failures, never
    which or why -- and two different backends then made the same mistake six
    times in a row.
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
    """Eight runs asked a model for something that needs no judgement.

    The stub has one job -- right shape, wrong value -- and contracts.provides
    already carries the signatures, the shape of every type and the file each
    thing lives in. L14 and L15 exist to make sure of it. Asking cost run 8's
    S1 four rejections at RED_GATE on stubs that answered a criterion
    correctly.
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
        # What the solver kept forgetting, under a system prompt that told it
        # it was writing Python -- where a declaration is importable as written.
        files = self.build()
        for text in files.values():
            for line in text.splitlines():
                if line.startswith(("interface", "type", "function", "const")):
                    self.fail("not exported: " + line)

    def test_a_boolean_returns_something_that_is_neither(self):
        # The one type with no wrong value. `toBe(true)` and `toBe(false)` must
        # both fail, or one criterion passes against the stub and the step stops.
        engine = self.build()["src/idlegame/engine.ts"]
        self.assertIn('return "__stub__" as unknown as boolean;', engine)

    def test_a_number_stays_a_number(self):
        # A string here turns an assertion failure into a TypeError, which
        # RED_GATE rejects outright (R5).
        self.assertIn("return -999999;", self.build()["src/idlegame/engine.ts"])

    def test_a_named_type_is_built_from_its_own_fields(self):
        engine = self.build()["src/idlegame/engine.ts"]
        self.assertIn("resource: -999999", engine)
        self.assertIn("lastUpdate: -999999", engine)

    def test_nothing_returns_a_default_value(self):
        # The rejection that prompted all of this: `{resource: 0, generators:
        # {}, lastUpdate: 0}` is the correct initial state, and a criterion
        # about createGame then passes against the stub.
        engine = self.build()["src/idlegame/engine.ts"]
        for wrong in ("return 0;", "return true;", "return false;", 'return "";',
                      "return {};", "return [];", "return null;"):
            self.assertNotIn(wrong, engine)

    def test_a_type_from_an_earlier_step_is_expanded_not_cast(self):
        # S2 provides `const CATALOG: Catalog`, and Catalog is S1's
        # Record<string, GeneratorDef>. Without S1's line the name is unknown,
        # the sentinel falls back to a cast, and the first test that reads
        # CATALOG['cursor'].baseCost gets a TypeError rather than a failed
        # assertion -- which RED_GATE rejects outright. The sentinel has to
        # carry the SHAPE, because the test takes it apart before it asserts.
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
        # `CATALOG.cursor.rate` throws before it compares if cursor is not
        # there, and RED_GATE rejects a thrown call as R5. The contract cannot
        # say which keys -- Record<string, GeneratorDef> is a type, not a
        # census -- but the criteria name them, and they are the runner's to
        # read. Nothing is shown to a model, so nothing can be hardcoded from
        # it: the runner takes the names and fills them with sentinels.
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
        # and __stub__ stays, or an assertion about the container's keys would
        # MATCH -- a test that passes against the stub stops the step.
        self.assertIn('"__stub__":', text)

    def test_what_it_cannot_parse_goes_back_to_the_solver(self):
        # None is not a failure to be ashamed of. A generated stub that does not
        # compile would be worse than the problem it replaces.
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


class APhaseHasToProduceWhatItWasAskedFor(unittest.TestCase):
    """Subset was checked; superset was not, and they are different questions.

    assert_touched asks whether anything outside the allowlist was written.
    Run 8's S2 had TEST_WRITE return having written nothing at all, and the
    phase recorded ok=True because the solver had not written anywhere it
    should not. RED_GATE then read an empty report and called it an error, two
    phases away from the cause.
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
