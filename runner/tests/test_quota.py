"""利用上限と一時的な混雑を、失敗と区別すること。

3役は同じサブスクリプションの枠を使う。上限に当たったのを Halt にすると
エスカレーションになり、エスカレーションはプランナーを呼んで、同じ枠をさらに
使う。だから見分けて、待ってからやり直す。

    python3 -m unittest discover -s runner/tests
"""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop  # noqa: E402


def proc(returncode: int, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout, "")


class WhatCountsAsQuota(unittest.TestCase):
    def test_the_documented_messages_are_recognised(self):
        cases = {
            "You've hit your session limit": "usage_limit",
            "You’ve hit your weekly limit · resets Mon": "usage_limit",
            "You've hit your Opus limit": "usage_limit",
            "API Error: Request rejected (429)": "rate_limit",
            "Server is temporarily limiting requests": "rate_limit",
            "API Error: Repeated 529 Overloaded errors": "rate_limit",
        }
        for out, kind in cases.items():
            with self.subTest(out=out):
                self.assertEqual(loop.quota_problem(out), kind)

    def test_an_ordinary_failure_is_not_quota(self):
        for out in ("Traceback (most recent call last):", "Not logged in", ""):
            with self.subTest(out=out):
                self.assertIsNone(loop.quota_problem(out))


class WaitingForQuota(unittest.TestCase):
    def setUp(self) -> None:
        self.events: list[str] = []
        for p in (mock.patch.object(loop, "ledger",
                                    lambda event, **k: self.events.append(event)),
                  mock.patch.object(loop.time, "sleep", lambda s: None),
                  mock.patch.dict(loop.QUOTA, {"wait_seconds": 1, "waits": 2})):
            p.start()
            self.addCleanup(p.stop)

    def test_a_call_that_recovers_is_returned_as_if_nothing_happened(self):
        calls = iter([proc(1, "You've hit your session limit"), proc(0, "done")])
        result = loop.run_agent("solver", "IMPL", lambda: next(calls))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.events, ["QUOTA_WAIT"])

    def test_waiting_runs_out_without_becoming_a_halt(self):
        # Halt ではないこと自体が要点。Halt はエスカレーションになる。
        invoke = mock.Mock(return_value=proc(1, "Request rejected (429)"))
        with self.assertRaises(loop.QuotaExhausted) as raised:
            loop.run_agent("planner", "PLAN_PROPOSE", invoke)
        self.assertNotIsInstance(raised.exception, loop.Halt)
        self.assertEqual(invoke.call_count, 3)   # 最初の1回 + 待ってから2回
        self.assertEqual(self.events, ["QUOTA_WAIT", "QUOTA_WAIT", "QUOTA_EXHAUSTED"])

    def test_any_other_failure_is_handed_back_untouched(self):
        invoke = mock.Mock(return_value=proc(1, "Traceback"))
        self.assertEqual(loop.run_agent("critic", "CRITIQUE", invoke).returncode, 1)
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(self.events, [])


if __name__ == "__main__":
    unittest.main()
