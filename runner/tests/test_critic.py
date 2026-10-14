"""4つ目の役。何を伝えられ、ランナーはその答えをどう扱うか。

この機械の中で意見を持つのはクリティックだけだ。ほかはすべて、数えるか、
ハッシュを取るか、比べる。だから固定しておく価値があるのは、意見を黙って
形だけの承認に変えてしまうものだ。ブリーフに何が届くかと、読めない答えが
どうなるか。

    python3 -m unittest discover -s runner/tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop  # noqa: E402
from loop import FINDINGS_NAME, brief_critique, read_findings, render_findings  # noqa: E402

SPEC = "- 開始直後から遊べること\n- セーブを消去できること\n"
BASIC = "--- basic/design.md ---\n## 概要\n放置ゲームを作る。\n"
MODULES = {"game/state": {
    "doc": "# 状態\n\n## 振る舞い\n資源を数える。\n",
    "criteria": [{"case": "normal", "kind": "unit", "given": "g", "then": "UNSEEN_THEN"}],
}}


class WhatTheCriticIsTold(unittest.TestCase):
    def brief(self) -> str:
        return brief_critique(SPEC, BASIC, MODULES)

    def test_it_is_given_the_spec_the_basic_design_and_every_module(self):
        brief = self.brief()
        self.assertIn("開始直後から遊べること", brief)
        self.assertIn("放置ゲームを作る。", brief)
        self.assertIn("--- modules/game/state.md ---\n# 状態", brief)

    def test_it_is_not_given_the_acceptance_criteria(self):
        # 受け入れ条件はソルバーの側の材料だ。クリティックが読むのは設計だけで、
        # 条件を見せると、条件の書き方の批評に流れる。
        self.assertNotIn("UNSEEN_THEN", self.brief())

    def test_it_looks_for_coverage_reach_and_boundaries(self):
        brief = self.brief()
        for what in ("**Coverage.**", "**Reach.**", "**Boundaries.**"):
            self.assertIn(what, brief)

    def test_the_brief_names_the_one_file_and_forbids_approval(self):
        brief = self.brief()
        self.assertIn(FINDINGS_NAME, brief)
        self.assertIn("empty `findings` list", brief)
        self.assertIn("cannot approve", brief)


class ReadingTheAnswer(unittest.TestCase):
    """読めない批評は何も言っておらず、決して問題なしと読んではならない。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name)
        self.saved_out = loop.CRITIC_OUT
        self.saved_ledger = loop.ledger
        loop.CRITIC_OUT = self.out
        self.events: list[tuple[str, dict]] = []
        loop.ledger = lambda event, **fields: self.events.append((event, fields))

    def tearDown(self) -> None:
        loop.CRITIC_OUT = self.saved_out
        loop.ledger = self.saved_ledger
        self.temp.cleanup()

    def write(self, body: str) -> None:
        (self.out / FINDINGS_NAME).write_text(body, encoding="utf-8")

    def test_findings_come_back_as_a_list(self):
        self.write(json.dumps({"findings": [
            {"title": "the initial state is a fixed point",
             "evidence": "S1 pins new_game() to resource=0.0",
             "machine_would_notice": False}]}))
        findings = read_findings()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["title"], "the initial state is a fixed point")

    def test_an_empty_list_is_an_answer(self):
        self.write(json.dumps({"findings": []}))
        self.assertEqual(read_findings(), [])

    def test_no_file_at_all_halts_rather_than_reading_as_clean(self):
        # 「クリティックは何も見つけなかった」と「クリティックは走らなかった」は
        # 同じに見えてはならない。parse_junit が壊れたレポートを失敗ゼロと読まない
        # のと同じ理由だ。
        with self.assertRaises(loop.Halt):
            read_findings()

    def test_unreadable_json_halts(self):
        self.write("{ not json")
        with self.assertRaises(loop.Halt):
            read_findings()

    def test_a_file_without_a_findings_list_halts(self):
        self.write(json.dumps({"verdict": "looks good to me"}))
        with self.assertRaises(loop.Halt):
            read_findings()

    def test_a_stray_file_is_deleted_and_recorded(self):
        # プランナーの B3 と同じ形。ディレクトリはランナーの所有なので、ランナーが
        # 消す。B3 と違い、試行を使わせることはありえない。ここには使うための
        # やり直しのループが無い。
        self.write(json.dumps({"findings": []}))
        (self.out / "notes.md").write_text("scratch", encoding="utf-8")
        read_findings()
        self.assertFalse((self.out / "notes.md").exists())
        self.assertEqual([e for e, _ in self.events], ["CRITIQUE_PRUNED"])


class Reporting(unittest.TestCase):
    def test_a_finding_no_gate_would_catch_is_marked_as_such(self):
        text = render_findings({"trace": [
            {"title": "nothing is reachable", "evidence": "0.0 < 15.0",
             "machine_would_notice": False}]})
        self.assertIn("NO GATE WOULD CATCH THIS", text)

    def test_a_mode_that_found_nothing_says_so(self):
        self.assertIn("(nothing found)", render_findings({"coverage": []}))


if __name__ == "__main__":
    unittest.main()
