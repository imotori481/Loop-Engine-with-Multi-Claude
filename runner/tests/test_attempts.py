"""ステップが何回試行でき、各回をどのソルバーが走らせるか。

ここでは3つの上限が出会い、どれも種類が違う:

  max_attempts     計画の数。どのソルバーが計画を走らせるかを知りえない
                   プランナーが書く
  limits.attempts  計画全体での上書き。適切な回数は、誰が試行の代金を払うかで
                   決まるからだ
  solver_tiers     バックエンドの並び。次の段がステップを見る前に、各段は試行を
                   全部使う

固定しておく価値がある性質は、ステップの予算の合計が試行回数 × 段の数であること、
そして段の切り替えがちょうど境目に来ることだ。1回早く引き継ぐと、ローカルの段が
試し終えていない仕事に、枠の限られた段を使うことになる。

    python3 -m unittest discover -s runner/tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop  # noqa: E402
from loop import (  # noqa: E402
    LIMITS, POLICY, SOLVER_TIERS, TIMEOUTS, agent_command, attempt_schedule,
    attempts_for, load_settings,
)

STEP = {"id": "S1", "max_attempts": 3}


class Settings(unittest.TestCase):
    """ここのテストはどれもモジュールの状態を書くので、どれも元に戻す。"""

    def setUp(self) -> None:
        self.saved = (dict(LIMITS), dict(POLICY), list(SOLVER_TIERS), dict(TIMEOUTS))

    def tearDown(self) -> None:
        limits, policy, tiers, timeouts = self.saved
        LIMITS.clear(); LIMITS.update(limits)
        POLICY.clear(); POLICY.update(policy)
        SOLVER_TIERS[:] = tiers
        TIMEOUTS.clear(); TIMEOUTS.update(timeouts)


class HowManyAttempts(Settings):
    def test_without_an_override_the_step_decides(self) -> None:
        self.assertEqual(attempts_for(STEP), 3)

    def test_the_override_replaces_every_step_at_once(self) -> None:
        # この口の要点。ソルバーを替えるたびに、どのソルバーが走るか誰も知らない
        # うちに書かれた11ステップを編集することになってはならない。
        load_settings({"limits": {"attempts": 10}})
        self.assertEqual(attempts_for(STEP), 10)
        self.assertEqual(attempts_for({"max_attempts": 1}), 10)

    def test_zero_means_leave_the_plan_alone(self) -> None:
        load_settings({"limits": {"attempts": 0}})
        self.assertEqual(attempts_for(STEP), 3)


class WhichSolverRunsWhichAttempt(Settings):
    def test_the_default_is_claude_alone(self) -> None:
        # solver_tiers を書かない計画は Claude Code だけで回る。
        self.assertEqual(SOLVER_TIERS, ["claude"])
        self.assertEqual(attempt_schedule(STEP), ["claude"] * 3)

    def test_one_backend_is_one_attempt_each(self) -> None:
        # 既定に任せず名前を挙げる。既定はたまたま走っている箱を表すもので、
        # ここで固定したいのは、段が1つのときの予定の形だ。
        load_settings({"solver_tiers": ["codex"]})
        self.assertEqual(attempt_schedule(STEP), ["codex"] * 3)

    def test_the_cheap_backend_spends_its_whole_budget_first(self) -> None:
        # 交互にはしない。試行を使い切る前に引き継ぐと、ローカルの段が試し終えて
        # いないステップに、枠の限られた段を使うことになる。
        load_settings({"solver_tiers": ["local", "codex"]})
        self.assertEqual(attempt_schedule(STEP),
                         ["local", "local", "local", "codex", "codex", "codex"])

    def test_the_budget_multiplies_by_the_number_of_backends(self) -> None:
        load_settings({"solver_tiers": ["local", "codex"], "limits": {"attempts": 8}})
        self.assertEqual(len(attempt_schedule(STEP)), 16)
        self.assertEqual(attempt_schedule(STEP).count("local"), 8)

    def test_the_backend_reaches_the_argv(self) -> None:
        # 渡し忘れた上限と同じ形のバグ。ここで選んだバックエンドを渡さないと、
        # solver-run が自分の既定を使い、台帳は走っていないバックエンドの名前を
        # 書くことになる。
        argv = agent_command("solver", loop.SOLVER_RUN, Path("/srv/loop/brief/impl.md"),
                             TIMEOUTS["solver"], "local")
        self.assertEqual(argv[-1], "local")
        self.assertEqual(argv[-2], str(TIMEOUTS["solver"]))

    def test_no_backend_means_no_extra_argument(self) -> None:
        argv = agent_command("planner", loop.PLANNER_RUN, Path("/brief.md"), 60)
        self.assertEqual(argv[-1], "60")


class SettingsThatWouldBeWrongAreRefused(Settings):
    def test_an_unknown_retry_mode_stops_the_run(self) -> None:
        with self.assertRaises(SystemExit):
            load_settings({"policy": {"retry": "resmaple"}})

    def test_both_retry_modes_are_accepted(self) -> None:
        for mode in loop.RETRY_MODES:
            load_settings({"policy": {"retry": mode}})
            self.assertEqual(POLICY["retry"], mode)

    def test_a_backend_name_that_could_be_a_path_is_refused(self) -> None:
        # 名前は /srv/loop/bin/solver-<name> に埋め込まれる。
        for bad in (["../../bin/sh"], ["Codex"], [""], [2]):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                load_settings({"solver_tiers": bad})
        # 一部だけ適用せず、丸ごと拒む。段は元のままだ。
        self.assertEqual(SOLVER_TIERS, self.saved[2])


if __name__ == "__main__":
    unittest.main()
