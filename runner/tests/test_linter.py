"""計画のリンタ。中心は L14（契約は渡すものの形を述べる）。

L14 は実際の1つのステップのためにある。`def buy_max_affordable(...) -> tuple` は
ほかのすべての規則を通り、ソルバーに渡され、作れなかった。要素の数が署名に無く、
スタブは要素1つのタプルを返し、テストの `result, count = ...` は分解で死んだ。
RED_GATE はそれを赤いテストではなく壊れた呼び出しと正しく判定し、ソルバー
（STUB のあいだ署名しか見ない）もプランナー（P5 が緑のステップの編集を許さない）も、
後からそれを直せなかった。

    python3 -m unittest discover -s runner/tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loop import validate_plan  # noqa: E402


def step(sid, kind, provides, requires=(), depends=(), annotate=True):
    """すべての規則を満たすステップ1つ。テストがちょうど1つだけ壊せるようにする。

    `annotate` は、各署名の置き場のモジュールを書き足す（L15）。既定で有効に
    するのは、ここのテストの大半がほかのことについてのもので、そうしないと
    すべてが L15 も報告してしまうからだ。下の L15 のテストはこれを切り、置き場を
    自分で書く。
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
    """ほかのすべての規則を満たす最小の計画。リンタが報告するものは L14 だけに
    なる。"""
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
        # この規則を書くきっかけになったステップ。
        problems = self.problems(
            ["def new_game() -> GameState"],
            ["def buy_max_affordable(state: GameState) -> tuple"])
        self.assertEqual(len(problems), 1)
        self.assertIn("L14", problems[0])
        self.assertIn("tuple", problems[0])

    def test_a_bare_parameter_type_is_rejected(self) -> None:
        # 呼び出し側も、説明の無い引数は組み立てられない。
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
        # -> Purchase は渋々認める例外ではなく、勧める答えだ。
        self.assertEqual(self.problems(
            ["class Purchase(state: GameState, bought: int)",
             "def new_game() -> GameState"],
            ["def buy_max_affordable(state: GameState) -> Purchase"]), [])

    def test_prose_beside_a_signature_is_not_read_as_a_type(self) -> None:
        # provides の行は末尾に置き場の注記を持ち、計画はそこで "list" と言う
        # ことがある。数えるのは型を注釈した位置だけだ。
        self.assertEqual(self.problems(
            ["def new_game() -> GameState  -- defined in pkg.core, returns a dict of counts"],
            ["def save(state: GameState) -> dict[str, float]"]), [])


class TheContractSaysWhereItLives(unittest.TestCase):
    """L15。テストを書くためのブリーフは goal ではなく `provides` を運ぶので、
    モジュールを名指ししない契約は import のパスを推測に任せる。run 4 の S1 は
    それで、8つ後のステップのモジュールを指す `from incgame.game import ...` の
    ためにエスカレーションを1回使った。"""

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
        # まさにその誤り。別のステップのモジュールを指している。
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
    """L8。`then` だけを見て、数字ではなく数で測る。

    `given` とつなげて見ることと、数字で数えることは、2つとも同時に誤りで、
    合わさって run 4 を止めた条件を通してしまう。`then: s2 == p.state exactly` は、
    `given` に 0.0 があり、`s2` の "2" が具体的な値と数えられたので通った。"""

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
        # L13 は最初のステップを skeleton にすることを求め、それは作りの上で L9 を
        # 満たす。落ちえない規則は規則ではなく、ただの文章だ。
        source = (Path(__file__).resolve().parents[1] / "loop.py").read_text(encoding="utf-8")
        self.assertNotIn('problems.append("L9', source)


if __name__ == "__main__":
    unittest.main()
