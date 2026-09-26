"""効く上限は、渡した上限だ。

solver-run と planner-run は、どちらも `timeout --kill-after=30 "${2:-900}"` を
掛ける。TIMEOUTS で上げても2つ目の引数として渡さなければ何も変わらない。
loop.py が 1800 と言っているのに、run 5 の計画づくりは 900 秒で2回殺された。

    python3 -m unittest discover -s runner/tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loop import (  # noqa: E402
    BACKSTOP_MARGIN, PLANNER_RUN, SOLVER_RUN, TIMEOUTS, agent_command,
)

# solver-run と planner-run の中の既定値。2つ目の引数が無いときに使われる。
SCRIPT_DEFAULT = 900


class TheLimitIsHandedOver(unittest.TestCase):
    def test_the_planner_call_carries_its_limit(self) -> None:
        argv = agent_command("planner", PLANNER_RUN, Path("/srv/loop/planner/brief/plan.md"),
                             TIMEOUTS["planner"])
        self.assertEqual(argv[:3], ["sudo", "-u", "planner"])
        self.assertEqual(argv[-1], str(TIMEOUTS["planner"]))

    def test_the_solver_call_carries_its_limit(self) -> None:
        argv = agent_command("solver", SOLVER_RUN, Path("/srv/loop/brief/impl.md"),
                             TIMEOUTS["solver"])
        self.assertEqual(argv[:3], ["sudo", "-u", "solver"])
        self.assertEqual(argv[-1], str(TIMEOUTS["solver"]))

    def test_a_configured_limit_above_the_script_default_would_have_no_effect_unpassed(self) -> None:
        # 数ではなくバグの形を守る。設定した上限がスクリプト自身の既定を超える
        # 限り、argv から抜けると黙って下がる。
        raised = [k for k in ("planner", "solver") if TIMEOUTS[k] > SCRIPT_DEFAULT]
        for key in raised:
            argv = agent_command(key, Path(f"/srv/loop/bin/{key}-run"),
                                 Path("/brief.md"), TIMEOUTS[key])
            self.assertIn(str(TIMEOUTS[key]), argv,
                          f"{key}'s limit is above the script default and must be passed")

    def test_the_runners_own_timeout_sits_above_the_agents(self) -> None:
        # 備えが先に発動すると、ランナーは、スクリプトがいまにも止めようとして
        # いたプロセスについて「まだ動いている」と報告する。しかも、自分では
        # 信号を送れないエージェントについてそう言う。
        self.assertGreater(BACKSTOP_MARGIN, 30, "must clear the scripts' --kill-after=30")


if __name__ == "__main__":
    unittest.main()
