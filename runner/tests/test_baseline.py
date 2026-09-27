"""最初のステップの前に、リポジトリにすでにあるテストが緑か。

VERIFY はスイート全体の緑を求める。取り込んだ時点で落ちているテストがあると、
どのステップも緑にならず、それは実装の失敗に見える。ソルバーは試行を使い切り、
エスカレーションはプランナーに届く。どちらも直せない。

    python3 -m unittest discover -s runner/tests
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop  # noqa: E402

GREEN = loop.TestRun(3, 0, 0, 0, [], ["a", "b", "c"], "")
FAILED = loop.TestRun(3, 1, 0, 0, ["AssertionError"], ["a", "b"], "",
                      failure_details=["tests.test_rules::test_score\n    assert 2 == 3"])
SKIPPED = loop.TestRun(3, 0, 0, 1, [], ["a", "b"], "",
                       skipped_names=["tests.test_rules::test_slow"])


class TheSuiteBeforeTheFirstStep(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events: list[tuple[str, dict]] = []
        for name, value in {
            "PROJECT": self.root, "TESTS": self.root / "tests",
            "ledger": lambda event, **f: self.events.append((event, f)),
        }.items():
            p = mock.patch.object(loop, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.dict(loop.LANGUAGE, loop.LANGUAGES["python"], clear=True)
        p.start()
        self.addCleanup(p.stop)

    def tests_on_disk(self, *names: str) -> None:
        for name in names:
            path = self.root / "tests" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    def check(self, result: loop.TestRun):
        with mock.patch.object(loop, "pytest_run", return_value=result) as run:
            loop.check_baseline()
        return run

    def test_a_new_project_has_nothing_to_run(self):
        run = self.check(FAILED)
        run.assert_not_called()
        self.assertEqual(self.events, [])

    def test_a_tests_directory_without_test_files_is_not_run(self):
        self.tests_on_disk("README.md")
        self.check(FAILED).assert_not_called()

    def test_a_green_suite_is_recorded_and_passes(self):
        self.tests_on_disk("test_rules.py")
        run = self.check(GREEN)
        run.assert_called_once_with("baseline", ["tests"])
        self.assertEqual(self.events, [("BASELINE", {"tests": 3, "failures": 0,
                                                     "errors": 0, "skipped": 0})])

    def test_a_failing_test_stops_the_run_and_is_named(self):
        self.tests_on_disk("test_rules.py")
        with self.assertRaises(loop.Halt) as caught:
            self.check(FAILED)
        self.assertEqual(caught.exception.phase, "PLAN_LOAD")
        self.assertIn("not green before the first step", caught.exception.reason)
        self.assertIn("1 failed", caught.exception.reason)
        self.assertIn("test_score", caught.exception.detail)

    def test_a_skipped_test_stops_the_run_too(self):
        # VERIFY はスキップを緑と数えない。放っておくと、どのステップも緑にならない。
        self.tests_on_disk("test_rules.py")
        with self.assertRaises(loop.Halt) as caught:
            self.check(SKIPPED)
        self.assertIn("1 skipped", caught.exception.reason)
        self.assertIn("tests.test_rules::test_slow", caught.exception.detail)


class WhereRunStepAsks(unittest.TestCase):
    """run_step が確かめるのは緑のステップが無いときだけで、止めてもエスカレーションしない。"""

    STEP = {"id": "S1", "files_write": ["src/a.py"], "files_test": ["tests/test_a.py"]}

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        plan = Path(self.temp.name) / "plan"
        plan.mkdir()
        (plan / "tasks.json").write_text('{"steps": []}', encoding="utf-8")
        self.escalate = mock.Mock()
        self.baseline = mock.Mock(side_effect=loop.Halt("PLAN_LOAD", "not green"))
        for name, value in {
            "PLAN": plan,
            "validate_plan": lambda tasks: [],
            "load_plan": lambda sid: (self.STEP, "context"),
            "ledger": lambda event, **f: None,
            "touched_paths": lambda: set(),
            "check_baseline": self.baseline,
            "escalate": self.escalate,
            # ここから先に進んだら、それと分かるように止める。
            "set_writable": mock.Mock(side_effect=RuntimeError("went on to TEST_WRITE")),
        }.items():
            p = mock.patch.object(loop, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_the_first_step_is_stopped_without_an_escalation(self):
        with mock.patch.object(loop, "green_steps", return_value=set()):
            with self.assertRaises(loop.Halt):
                loop.run_step("S1")
        self.baseline.assert_called_once_with()
        self.escalate.assert_not_called()

    def test_once_a_step_is_green_the_suite_is_not_checked_again(self):
        # 緑のステップの後で落ちているテストは、そのステップが壊したものだ。
        # それは VERIFY が回帰として扱う。
        with mock.patch.object(loop, "green_steps", return_value={"S0"}):
            with self.assertRaises(RuntimeError):
                loop.run_step("S1")
        self.baseline.assert_not_called()


if __name__ == "__main__":
    unittest.main()
