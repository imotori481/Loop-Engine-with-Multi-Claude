"""L14 -- a contract states the shape of what it hands over.

The rule exists because of one real step. `def buy_max_affordable(...) -> tuple`
passed every other rule, went out to the solver, and could not be built: the
arity is not in the signature, the stub returned a one-element tuple, and the
test's `result, count = ...` died unpacking it. RED_GATE called that a broken
call rather than a red test, correctly, and neither the solver (which sees only
the signature during STUB) nor the planner (P5 will not let it edit a green
step) could repair it afterwards.

    python3 -m unittest discover -s runner/tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loop import validate_plan  # noqa: E402


def step(sid, kind, provides, requires=(), depends=(), annotate=True):
    """One step that satisfies every rule, so a test can break exactly one.

    `annotate` appends the module each signature lives in (L15). It is on by
    default because most of these tests are about something else and would
    otherwise all report L15 as well; the L15 tests below turn it off and say
    where things live themselves.
    """
    module = f"pkg.{sid.lower()}"
    if annotate:
        provides = [f"{line}  -- defined in {module}" for line in provides]
    return {
        "id": sid,
        "kind": kind,
        "goal": f"build {sid}",
        "depends_on": list(depends),
        "contracts": {"requires": list(requires), "provides": list(provides),
                      "invariants": []},
        "acceptance": [
            {"case": "normal", "given": "a new game", "then": "resources == 0.0"},
            {"case": "boundary", "given": "resources == 0.0",
             "then": "buy() returns False and resource stays 0.0"},
            {"case": "error", "given": "an unknown id", "then": "raises KeyError"},
        ],
        "files_write": [f"src/pkg/{sid.lower()}.py"],
        "files_test": [f"tests/test_{sid.lower()}.py"],
        "expected_tests": 3,
        "max_attempts": 3,
        "review_gate": False,
    }


def plan(*provides):
    """The smallest plan that satisfies every other rule, so anything the
    linter reports is L14 and nothing else."""
    first, second = provides
    return {
        "version": 1,
        "steps": [
            step("S1", "skeleton", first),
            step("S2", "integration", second, requires=first, depends=["S1"]),
        ],
    }


class TheShapeMustBeStated(unittest.TestCase):
    def problems(self, *provides):
        return validate_plan(plan(*provides))

    def test_a_plan_whose_contracts_state_their_contents_is_valid(self) -> None:
        self.assertEqual(self.problems(
            ["class GameState(resources: float, owned: dict[str, int])",
             "def new_game() -> GameState"],
            ["def save(state: GameState) -> dict[str, float]",
             "def catalog() -> list[str]"]), [])

    def test_a_bare_return_type_is_rejected(self) -> None:
        # The step this rule was written for.
        problems = self.problems(
            ["def new_game() -> GameState"],
            ["def buy_max_affordable(state: GameState) -> tuple"])
        self.assertEqual(len(problems), 1)
        self.assertIn("L14", problems[0])
        self.assertIn("tuple", problems[0])

    def test_a_bare_parameter_type_is_rejected(self) -> None:
        # A caller cannot build an argument it has no description of, either.
        problems = self.problems(
            ["def new_game() -> GameState"],
            ["def production_rate(catalog: dict) -> float"])
        self.assertEqual(len(problems), 1)
        self.assertIn("L14", problems[0])

    def test_a_bare_field_on_a_declared_class_is_rejected(self) -> None:
        problems = self.problems(
            ["class GameState(resources: float, owned: dict)"],
            ["def save(state: GameState) -> dict[str, float]"])
        self.assertEqual(len(problems), 1)
        self.assertIn("owned", problems[0])

    def test_every_bare_type_in_one_signature_is_named(self) -> None:
        problems = self.problems(
            ["def new_game() -> GameState"],
            ["def build(generators: list) -> dict"])
        self.assertEqual(len(problems), 1)
        self.assertIn("dict, list", problems[0])

    def test_a_named_type_is_never_a_bare_container(self) -> None:
        # -> Purchase is the preferred answer, not a grudging exception.
        self.assertEqual(self.problems(
            ["class Purchase(state: GameState, bought: int)",
             "def new_game() -> GameState"],
            ["def buy_max_affordable(state: GameState) -> Purchase"]), [])

    def test_prose_beside_a_signature_is_not_read_as_a_type(self) -> None:
        # provides lines carry a trailing note about where the symbol lives, and
        # a plan may well say "list" in it. Only annotated positions count.
        self.assertEqual(self.problems(
            ["def new_game() -> GameState  -- defined in pkg.core, returns a dict of counts"],
            ["def save(state: GameState) -> dict[str, float]"]), [])


class TheContractSaysWhereItLives(unittest.TestCase):
    """L15. The brief for writing tests carries `provides` and not the goal, so a
    contract that does not name its module leaves the import path to a guess --
    which cost step S1 of run 4 an escalation on `from incgame.game import ...`,
    a module belonging to a step eight places later."""

    def test_a_contract_without_a_module_is_rejected(self) -> None:
        first = step("S1", "skeleton", ["def new_game() -> GameState"])
        first["files_write"] = ["src/incgame/engine.py"]
        second = step("S2", "integration", ["def save(s: GameState) -> dict[str, float]  -- in incgame.io"],
                      requires=["def new_game() -> GameState"], depends=["S1"])
        second["files_write"] = ["src/incgame/io.py"]
        problems = validate_plan({"version": 1, "steps": [first, second]})
        self.assertEqual(len(problems), 1)
        self.assertIn("L15", problems[0])
        self.assertIn("incgame.engine", problems[0])

    def test_naming_the_module_satisfies_it(self) -> None:
        first = step("S1", "skeleton", ["def new_game() -> GameState  # defined in incgame.engine"])
        first["files_write"] = ["src/incgame/engine.py"]
        second = step("S2", "integration", ["def save(s: GameState) -> dict[str, float]  -- in incgame.io"],
                      requires=["def new_game() -> GameState  # defined in incgame.engine"],
                      depends=["S1"])
        second["files_write"] = ["src/incgame/io.py"]
        self.assertEqual(validate_plan({"version": 1, "steps": [first, second]}), [])

    def test_naming_a_module_the_step_does_not_write_is_rejected(self) -> None:
        # The exact mistake: pointing at a module that belongs to another step.
        first = step("S1", "skeleton", ["def new_game() -> GameState  # defined in incgame.game"])
        first["files_write"] = ["src/incgame/engine.py"]
        second = step("S2", "integration", ["def save(s: GameState) -> dict[str, float]  -- in incgame.io"],
                      requires=["def new_game() -> GameState  # defined in incgame.game"],
                      depends=["S1"])
        second["files_write"] = ["src/incgame/io.py"]
        problems = validate_plan({"version": 1, "steps": [first, second]})
        self.assertEqual(len(problems), 1)
        self.assertIn("L15", problems[0])

    def test_the_package_itself_counts_for_a_package_level_symbol(self) -> None:
        first = step("S1", "skeleton", ["def new_game() -> GameState  # defined in incgame"])
        first["files_write"] = ["src/incgame/__init__.py"]
        second = step("S2", "integration", ["def save(s: GameState) -> dict[str, float]  -- in incgame.io"],
                      requires=["def new_game() -> GameState  # defined in incgame"],
                      depends=["S1"])
        second["files_write"] = ["src/incgame/io.py"]
        self.assertEqual(validate_plan({"version": 1, "steps": [first, second]}), [])


class TheExpectedResultIsAValue(unittest.TestCase):
    """L8, measured on the `then` alone and against a NUMBER rather than a digit.

    Both halves were wrong at once, and together they let through the criterion
    that stopped run 4: `then: s2 == p.state exactly` passed because `given`
    contained 0.0 and because the "2" in `s2` counted as a concrete value."""

    def plan_with(self, then: str):
        first = step("S1", "skeleton", ["def new_game() -> GameState  # in incgame.engine"])
        first["files_write"] = ["src/incgame/engine.py"]
        first["acceptance"][1]["given"] = "state = new_game(0.0); p = buy(state, 'cursor')"
        first["acceptance"][1]["then"] = then
        second = step("S2", "integration", ["def save(s: GameState) -> dict[str, float]  -- in incgame.io"],
                      requires=["def new_game() -> GameState  # in incgame.engine"], depends=["S1"])
        second["files_write"] = ["src/incgame/io.py"]
        return validate_plan({"version": 1, "steps": [first, second]})

    def test_a_result_stated_as_a_comparison_between_two_calls_is_rejected(self) -> None:
        problems = self.plan_with("s2 == p.state exactly (zero elapsed time changes nothing)")
        self.assertEqual(len(problems), 1)
        self.assertIn("L8", problems[0])

    def test_the_same_result_written_out_as_values_is_accepted(self) -> None:
        self.assertEqual(self.plan_with(
            "p.state.resource == 0.0, p.state.generators == {'cursor': 1}"), [])

    def test_a_concrete_value_in_the_given_does_not_excuse_the_then(self) -> None:
        problems = self.plan_with("the state is unchanged")
        self.assertEqual(len(problems), 1)
        self.assertIn("L8", problems[0])

    def test_an_exception_type_is_a_concrete_result(self) -> None:
        self.assertEqual(self.plan_with("raises InsufficientFundsError"), [])

    def test_an_empty_collection_or_a_python_constant_is_a_concrete_result(self) -> None:
        # 境界のケースの答えとして一番よく出る形。数えないと、プランナーは
        # len(...) == 0 のように言い換えるか、書き直しで呼び出しを1回使う。
        for then in ("returns exactly []", "returns exactly {}", "returns exactly ()",
                     "returns None", "returns True", "returns False"):
            with self.subTest(then=then):
                self.assertEqual(self.plan_with(then), [])

    def test_words_that_only_describe_emptiness_are_still_rejected(self) -> None:
        for then in ("returns an empty list", "the result is true for every input"):
            with self.subTest(then=then):
                problems = self.plan_with(then)
                self.assertEqual(len(problems), 1)
                self.assertIn("L8", problems[0])


class RulesThatAreGone(unittest.TestCase):
    def test_l9_is_retired(self) -> None:
        # L13 requires the first step to be a skeleton, which satisfies L9 by
        # construction. A rule that cannot fail is not a rule; it is text.
        source = (Path(__file__).resolve().parents[1] / "loop.py").read_text(encoding="utf-8")
        self.assertNotIn('problems.append("L9', source)


if __name__ == "__main__":
    unittest.main()
