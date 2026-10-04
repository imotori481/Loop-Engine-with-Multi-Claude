"""何がプランナーの試行を1回使わせ、何が使わせないか。

プランナーが計画に挑める回数は少なく固定で、どれもモデルへの有料の呼び出しだ。
だから「これは違反か」は整頓の問題ではない。正しい計画を捨てるかどうかを決める。

ここで固定する区別: 違反とは、`plan apply` が走るときにもまだ誤っているもの
だ。ランナーがすでに消したファイルはそうではない。プランナーに伝える価値の
ある癖で、それ以上のものではない。

リンタが何を誤りとみなすかは `test_linter.py` の対象で、ここではあえて差し替えて
ある。これらのテストは試行の算術についてのもので、差し替えないと、計画に必須の
項目が増えるたびに落ちる。

    python3 -m unittest discover -s runner/tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop  # noqa: E402


class RetryFixture(unittest.TestCase):
    """やり直しのループ。プランナーとリンタの両方を台本どおりに動かす。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name) / "out"
        self.out.mkdir()
        self.saved = (loop.PLANNER_OUT, dict(loop.LIMITS), loop.call_planner,
                      loop.ledger, loop.proposal_problems)
        loop.PLANNER_OUT = self.out
        self.events: list[tuple[str, dict]] = []
        loop.ledger = lambda event, **fields: self.events.append((event, fields))

    def tearDown(self) -> None:
        out, limits, call, ledger, problems = self.saved
        loop.PLANNER_OUT = out
        loop.LIMITS.clear(); loop.LIMITS.update(limits)
        loop.call_planner = call
        loop.ledger = ledger
        loop.proposal_problems = problems
        self.temp.cleanup()

    def write_proposal(self, extra: dict[str, str] | None = None) -> None:
        """許された3つの名前と、プランナーが残したほかのもの。"""
        for name in ("SYSTEM_SPEC.md", "CONTEXT.md", "tasks.json"):
            (self.out / name).write_text("{}", encoding="utf-8")
        for name, body in (extra or {}).items():
            (self.out / name).write_text(body, encoding="utf-8")

    def plan(self, writes: list, verdicts: list[list[str]]) -> tuple[int, list[str]]:
        """ループを走らせる。`writes[i]` は i+1 回目にプランナーがすること、
        `verdicts[i]` はその結果についてリンタが言うこと。"""
        briefs: list[str] = []
        write = iter(writes)
        verdict = iter(verdicts)

        def fake_call(brief: str) -> str:
            briefs.append(brief)
            next(write)()
            return ""

        loop.call_planner = fake_call
        loop.proposal_problems = lambda proposal: list(next(verdict))
        # ここではブリーフをフィードバックそのものにする。プランナーが何を
        # 伝えられたかを、テストで読めるようにだ。
        return loop.plan_with_retry(lambda feedback: feedback, "PLAN_TEST"), briefs


class AStrayFileIsNotAViolation(RetryFixture):
    def test_a_valid_plan_passes_even_with_a_scratch_file_beside_it(self):
        # プランナーは自分の計算を確かめるために _calc.py を書く。ランナーは何かを
        # 読む前にそれを消すので、計画を判定する時点でファイルは存在しない。ここで
        # 落とすと、ランナーがすでに取り消した状態を取り消すために試行を1回使う。
        # run 7 は3回目で、ほかは正しい計画に対してまさにそれをした。
        loop.LIMITS["revisions"] = 3
        code, briefs = self.plan(
            [lambda: self.write_proposal({"_calc.py": "print(2 * 3)\n"})], [[]])
        self.assertEqual(code, 0)
        self.assertEqual(len(briefs), 1, "a valid plan must not be retried")
        self.assertFalse((self.out / "_calc.py").exists())

    def test_the_deletion_is_still_recorded(self):
        # 違反でないことは、知る価値が無いことと同じではない。後で台帳を読む人は、
        # ファイルが消されたことを見られるべきだ。
        loop.LIMITS["revisions"] = 3
        self.plan([lambda: self.write_proposal({"_calc.py": "x = 1\n"})], [[]])
        pruned = [fields for event, fields in self.events if event == "PLAN_PRUNED"]
        self.assertEqual(pruned, [{"attempt": 1, "removed": ["_calc.py"]}])

    def test_a_directory_is_removed_the_same_way(self):
        loop.LIMITS["revisions"] = 3
        def write() -> None:
            self.write_proposal()
            (self.out / "__pycache__").mkdir()
            (self.out / "__pycache__" / "x.pyc").write_bytes(b"\x00")
        code, briefs = self.plan([write], [[]])
        self.assertEqual((code, len(briefs)), (0, 1))
        self.assertFalse((self.out / "__pycache__").exists())

    def test_the_habit_is_reported_when_the_attempt_fails_for_another_reason(self):
        # 注記は黙ることではない。どのみちやり直す試行がそれを運ぶので、
        # プランナーは、それで落とされることなく、作業用のファイルを書くのを
        # やめることを覚える。
        loop.LIMITS["revisions"] = 1
        code, briefs = self.plan(
            [lambda: self.write_proposal({"_calc.py": "x = 1\n"}),
             lambda: self.write_proposal()],
            [["L4: S2 depends on a step that does not exist"], []])
        self.assertEqual(code, 0)
        self.assertIn("_calc.py", briefs[1])
        self.assertIn("B3", briefs[1])
        self.assertIn("L4", briefs[1])

    def test_a_plan_written_under_the_wrong_name_is_still_refused(self):
        # 紛れ込んだファイルが問題になる唯一の場合。`task.json` を消すと適用する
        # ものが無くなり、ランナーは無い提案を適用せずにそう言う。
        loop.LIMITS["revisions"] = 0
        with self.assertRaises(loop.Halt):
            self.plan([lambda: (self.out / "task.json").write_text(
                "{}", encoding="utf-8")], [[]])


class AnUnusedProvideIsWiredByTheRunner(RetryFixture):
    """L11 だけで落ちた計画は、後のステップが名前を使っていれば、ランナーが
    requires を足して通す。プランナーを呼び直すのは、判断が要るときだけ。"""

    L11 = "L11: step S1 provides `total`, which no step requires"

    def write_plan(self, goal: str) -> None:
        self.write_proposal()
        tasks = {"steps": [
            {"id": "S1", "kind": "unit", "goal": "Sum a list.", "depends_on": [],
             "contracts": {"provides": ["def total(xs) -> int"], "requires": []},
             "acceptance": [{"case": "normal", "given": "total([1, 2])", "then": "3"}]},
            {"id": "S2", "kind": "integration", "goal": goal, "depends_on": ["S1"],
             "contracts": {"provides": ["def report(xs) -> str"], "requires": []},
             "acceptance": [{"case": "normal", "given": "report([1, 2])", "then": "'3'"}]},
        ]}
        (self.out / "tasks.json").write_text(json.dumps(tasks), encoding="utf-8")

    def requires_of(self, step_id: str) -> list[str]:
        tasks = json.loads((self.out / "tasks.json").read_text(encoding="utf-8"))
        return next(s for s in tasks["steps"] if s["id"] == step_id)["contracts"]["requires"]

    def test_a_name_a_dependent_step_uses_is_added_to_its_requires(self):
        loop.LIMITS["revisions"] = 3
        code, briefs = self.plan(
            [lambda: self.write_plan("Format the sum from total(xs).")], [[self.L11], []])
        self.assertEqual((code, len(briefs)), (0, 1))
        self.assertEqual(self.requires_of("S2"), ["def total(xs) -> int"])
        wired = [fields for event, fields in self.events if event == "PLAN_WIRED"]
        self.assertEqual(wired, [{"attempt": 1, "wired": [
            {"step": "S1", "name": "total", "to": ["S2"]}]}])

    def test_a_name_no_step_uses_goes_back_to_the_planner(self):
        # subtotal は total ではない。使う側が無ければ、消すか残すかはプランナーが決める。
        loop.LIMITS["revisions"] = 3
        code, briefs = self.plan(
            [lambda: self.write_plan("Format the sum from subtotal(xs)."), lambda: None],
            [[self.L11], []])
        self.assertEqual((code, len(briefs)), (0, 2))
        self.assertIn(self.L11, briefs[1])
        self.assertEqual(self.requires_of("S2"), [])

    def test_the_wiring_is_dropped_when_it_breaks_another_rule(self):
        # 足した計画が別の規則で落ちるなら、プランナーには元の違反を返し、
        # out/ の計画もプランナーが書いたままにする。
        loop.LIMITS["revisions"] = 3
        code, briefs = self.plan(
            [lambda: self.write_plan("Format the sum from total(xs)."), lambda: None],
            [[self.L11], ["L3: step S2 requires `total`, which no dependency provides"], []])
        self.assertEqual((code, len(briefs)), (0, 2))
        self.assertIn(self.L11, briefs[1])
        self.assertNotIn("L3", briefs[1])
        self.assertEqual(self.requires_of("S2"), [])
        self.assertNotIn("PLAN_WIRED", [event for event, _ in self.events])


class TheAttemptCeiling(RetryFixture):
    def test_the_last_allowed_attempt_is_revisions_plus_one(self):
        # revisions はプランナーを差し戻してよい回数なので、呼び出しの回数は
        # それより1多い。
        loop.LIMITS["revisions"] = 2
        code, briefs = self.plan(
            [self.write_proposal] * 3, [["L1: bad"], ["L1: bad"], ["L1: bad"]])
        self.assertEqual((code, len(briefs)), (2, 3))

    def test_an_escalation_ends_the_loop_without_being_judged(self):
        # エスカレーションは人間宛ての答えで、下書きではない。リンタはそれを
        # 見ず、やり直しを1回使わせることもない。
        loop.LIMITS["revisions"] = 2
        code, briefs = self.plan(
            [lambda: (self.out / loop.ESCALATE_NAME).write_text(
                "the requirements contradict each other", encoding="utf-8")],
            [])
        self.assertEqual((code, len(briefs)), (0, 1))


if __name__ == "__main__":
    unittest.main()
