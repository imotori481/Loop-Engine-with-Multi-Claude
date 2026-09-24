"""How many attempts a step gets, and which solver runs each one.

Three ceilings meet here and they are not the same kind of thing:

  max_attempts   the plan's number, written by a planner that cannot know which
                 solver will run its plan
  limits.attempts  a plan-wide override, because the right number depends on who
                 is paying for an attempt
  solver_tiers   the backends, in order, each getting a full set of attempts
                 before the next one sees the step

The property worth pinning down is that a step's total budget is attempts x
tiers, and that the tier change lands exactly on the boundary -- a handover one
attempt early spends the rationed backend on work the local one had not finished
trying.

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
    """Every test here writes module state, so every test here puts it back."""

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
        # The point of the knob: switching solver must not mean editing eleven
        # steps that were written before anyone knew which solver would run.
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
        # Named rather than left to the default: the default describes the box
        # this happens to run on, and what is being pinned here is the shape of
        # the schedule for a single tier.
        load_settings({"solver_tiers": ["codex"]})
        self.assertEqual(attempt_schedule(STEP), ["codex"] * 3)

    def test_the_cheap_backend_spends_its_whole_budget_first(self) -> None:
        # Not interleaved. A handover before the attempts are gone would spend
        # the rationed backend on a step the local one had not finished trying.
        load_settings({"solver_tiers": ["local", "codex"]})
        self.assertEqual(attempt_schedule(STEP),
                         ["local", "local", "local", "codex", "codex", "codex"])

    def test_the_budget_multiplies_by_the_number_of_backends(self) -> None:
        load_settings({"solver_tiers": ["local", "codex"], "limits": {"attempts": 8}})
        self.assertEqual(len(attempt_schedule(STEP)), 16)
        self.assertEqual(attempt_schedule(STEP).count("local"), 8)

    def test_the_backend_reaches_the_argv(self) -> None:
        # Same shape of bug as the unpassed timeout: a backend chosen here and
        # not handed over leaves solver-run applying its own default, and the
        # ledger would then name a backend that did not run.
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
        # The name is interpolated into /srv/loop/bin/solver-<name>.
        for bad in (["../../bin/sh"], ["Codex"], [""], [2]):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                load_settings({"solver_tiers": bad})
        # Refused outright, not partially applied: the tiers are what they were.
        self.assertEqual(SOLVER_TIERS, self.saved[2])


if __name__ == "__main__":
    unittest.main()
