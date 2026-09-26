"""利用上限と一時的な混雑を、失敗と区別すること。

3役は同じサブスクリプションの枠を使う。上限に当たったのを Halt にすると
エスカレーションになり、エスカレーションはプランナーを呼んで、同じ枠をさらに
使う。だから見分けて、待ってからやり直す。

    python3 -m unittest discover -s runner/tests
"""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
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


class UsageIsRecorded(unittest.TestCase):
    """--output-format json の出力から消費量を台帳に残し、結果の文だけを返す。"""

    def setUp(self) -> None:
        self.events: list[tuple[str, dict]] = []
        p = mock.patch.object(loop, "ledger",
                              lambda event, **k: self.events.append((event, k)))
        p.start()
        self.addCleanup(p.stop)

    def payload(self, **extra) -> str:
        data = {"type": "result", "subtype": "success", "is_error": False,
                "result": "Created src/smoke.txt", "num_turns": 3,
                "duration_ms": 4200, "total_cost_usd": 0.01,
                "usage": {"input_tokens": 10, "output_tokens": 20},
                "modelUsage": {"claude-sonnet-5": {"outputTokens": 20}}}
        data.update(extra)
        return json.dumps(data)

    def test_the_caller_gets_the_result_text_and_the_ledger_gets_the_usage(self):
        result = loop.run_agent("solver", "IMPL", lambda: proc(0, self.payload()))
        self.assertEqual(result.stdout.strip(), "Created src/smoke.txt")
        (event, fields), = self.events
        self.assertEqual(event, "USAGE")
        self.assertEqual(fields["who"], "solver")
        self.assertEqual(fields["models"], ["claude-sonnet-5"])
        self.assertEqual(fields["usage"], {"input_tokens": 10, "output_tokens": 20})
        self.assertEqual(fields["turns"], 3)

    def test_a_quota_message_inside_the_json_is_still_recognised(self):
        with mock.patch.object(loop.time, "sleep", lambda s: None), \
             mock.patch.dict(loop.QUOTA, {"wait_seconds": 1, "waits": 1}):
            calls = iter([
                proc(1, self.payload(is_error=True, result="You've hit your session limit")),
                proc(0, self.payload()),
            ])
            result = loop.run_agent("planner", "PLAN_PROPOSE", lambda: next(calls))
        self.assertEqual(result.returncode, 0)
        self.assertEqual([e for e, _ in self.events], ["USAGE", "QUOTA_WAIT", "USAGE"])

    def test_the_screen_gets_one_short_line_and_the_ledger_gets_everything(self):
        # setUp の差し替えを外し、本物の ledger で台帳と画面を比べる。
        mock.patch.stopall()
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.object(loop, "LEDGER", Path(temp) / "ledger.jsonl"):
            screen = io.StringIO()
            with contextlib.redirect_stdout(screen):
                loop.record_usage("solver", "IMPL", json.loads(self.payload(
                    usage={"input_tokens": 4, "cache_read_input_tokens": 100,
                           "cache_creation_input_tokens": 6, "output_tokens": 20,
                           "iterations": [{"input_tokens": 2}]})))
            record = json.loads(loop.LEDGER.read_text(encoding="utf-8"))
        line = screen.getvalue().strip()
        self.assertEqual(line, "[USAGE] who=solver phase=IMPL model=claude-sonnet-5 "
                               "in=110 out=20 sec=4 usd=0.01")
        self.assertNotIn("iterations", line)
        self.assertIn("iterations", record["usage"])

    def test_output_that_is_not_json_is_passed_through(self):
        result = loop.run_agent("solver", "IMPL", lambda: proc(0, "plain text"))
        self.assertEqual(result.stdout, "plain text")
        self.assertEqual(self.events, [])


if __name__ == "__main__":
    unittest.main()
