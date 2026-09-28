"""4つ目の役。何を伝えられ、ランナーはその答えをどう扱うか。

この機械の中で意見を持つのはクリティックだけだ。ほかはすべて、数えるか、
ハッシュを取るか、比べる。だから固定しておく価値があるのは、意見を黙って
形だけの承認に変えてしまうものだ。ブリーフに何が届くかと、読めない答えが
どうなるか。

    python3 -m unittest discover -s runner/tests
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop  # noqa: E402
from loop import (  # noqa: E402
    CRITIQUE_MODES, FINDINGS_NAME, brief_critique_coverage, brief_critique_trace,
    read_findings, render_findings,
)

REQUIREMENTS = "- 開始直後から遊べること\n- セーブを消去できること\n"
TASKS = json.dumps({"language": "python", "steps": [{"id": "S1"}]}, ensure_ascii=False)


class WhatTheCriticIsTold(unittest.TestCase):
    def test_both_modes_exist_and_are_the_whole_list(self):
        # run 7 の計画で測った。coverage は届かない機能、確かめられていない起動の
        # 経路、自分の要件に逆らって確かめる条件を見つけた。trace は初期状態が
        # 不動点であることを見つけた。指摘は重ならなかったので、どちらのモードも
        # 余計ではない。
        self.assertEqual(sorted(CRITIQUE_MODES), ["coverage", "trace"])

    def test_coverage_is_given_the_requirements_and_the_plan(self):
        brief = brief_critique_coverage(REQUIREMENTS, TASKS)
        self.assertIn("開始直後から遊べること", brief)
        self.assertIn('"id": "S1"', brief)

    def test_trace_is_not_given_the_requirements(self):
        # これがこのモードの「目的」の性質だ。run 7 で、trace は条件だけから行き
        # 詰まりを導いた。だから、欠けている部品について要件が何も言っていなくても、
        # 動かない製品を捕まえられる。run 7 の要件は初期状態に一言も触れていな
        # かった。要件を渡された trace はそれに照らして採点するようになり、この
        # モードはもう片方と同じものになってしまう。
        brief = brief_critique_trace(TASKS)
        self.assertNotIn("開始直後から遊べること", brief)
        self.assertNotIn("セーブを消去", brief)
        self.assertIn('"id": "S1"', brief)

    def test_every_brief_names_the_one_file_and_forbids_approval(self):
        for brief in (brief_critique_coverage(REQUIREMENTS, TASKS),
                      brief_critique_trace(TASKS)):
            self.assertIn(FINDINGS_NAME, brief)
            self.assertIn("empty `findings` list", brief)
            self.assertIn("cannot approve", brief)

    def test_the_tracer_is_told_a_defence_is_not_dead_code(self):
        # run 8 の計画では、壊れたセーブや知らない ID への守りのテストを、
        # 3回の批評すべてで「遊んでいても届かない状態」と指摘した。訊いた問いが
        # そう答えるよう誘っていた。外から来る入力は対象外だと伝える。
        brief = brief_critique_trace(TASKS)
        self.assertIn("the artifact's OWN logic", brief)
        self.assertIn("checking a defence, not exercising dead code", brief)

    def test_the_exemption_does_not_cover_a_read_nothing_writes(self):
        # 守りを対象外にした説明を広く取ると、読み込む処理はあるのに書き込む
        # 処理が無い、という成果物自身の欠陥まで黙る。対象外は守りだけで、
        # 読まれるものが書かれているかは確かめさせる。
        brief = brief_critique_trace(TASKS)
        self.assertIn("covers the DEFENCE, not the thing being read", brief)
        self.assertIn("step ever writes", brief)
        self.assertIn("Report that.", brief)

    def test_code_that_was_there_before_the_plan_is_not_a_read_nothing_writes(self):
        # 取り込んだリポジトリの関数を呼ぶステップは、どのステップも書かない
        # ものを読んでいるように見える。最初からあるものだと伝える。
        brief = brief_critique_trace(TASKS)
        self.assertIn('"What the code already declares" is not such a thing', brief)
        self.assertIn("exists before the first step", brief)


class TheCriticSeesOnlyWhatThePlanTouches(unittest.TestCase):
    """クリティックが既存の宣言を使うのは、既にあるものを「どのステップも書いて
    いない」と指摘しないためだけだ。取り込んだ Unity のプロジェクトでは、この一覧が
    ブリーフ16.4万字のうち11.2万字を占め、批評のたびにキャッシュ書き込みを払った。
    """

    BOARD = "Assets/Source/Logic/Board.cs"
    DECK = "Assets/Source/Logic/Deck.cs"
    MENU = "Assets/Source/Ui/Menu.cs"
    BY_FILE = {
        BOARD: [f"{BOARD}: class Board -- namespace Logic",
                f"{BOARD}: static int Board.Score(Board board) -- namespace Logic",
                f"{BOARD}: int Board.Width {{ get; }} -- namespace Logic"],
        DECK: [f"{DECK}: class Deck -- namespace Logic",
               f"{DECK}: void Deck.Shuffle() -- namespace Logic"],
        MENU: [f"{MENU}: class Menu -- namespace Ui",
               f"{MENU}: void Menu.Open() -- namespace Ui"],
    }
    PLAN = {"steps": [{
        "id": "S1",
        "contracts": {"requires": [f"{BOARD}: static int Board.Score(Board board) -- namespace Logic"],
                      "provides": [f"{DECK}: void Deck.Deal(Board board)"]},
        "files_write": [DECK],
    }]}

    def split(self, plan: dict) -> tuple[list[str], list[str]]:
        with patch.dict(loop.LANGUAGE, loop.LANGUAGES["csharp"], clear=True):
            return loop.declarations_for_plan(self.BY_FILE, plan)

    def test_a_named_member_is_kept_with_the_head_of_its_type(self):
        kept, _ = self.split(self.PLAN)
        self.assertIn(self.BY_FILE[self.BOARD][0], kept)
        self.assertIn(self.BY_FILE[self.BOARD][1], kept)
        self.assertNotIn(self.BY_FILE[self.BOARD][2], kept)

    def test_every_declaration_of_a_file_a_step_writes_is_kept(self):
        # 書き換えるステップのファイルは、provides に無い宣言も残る。スタブが
        # 替えないそれらを、クリティックは既にあるものとして読む。
        kept, _ = self.split(self.PLAN)
        for line in self.BY_FILE[self.DECK]:
            self.assertIn(line, kept)

    def test_the_rest_is_named_by_its_file(self):
        kept, dropped = self.split(self.PLAN)
        self.assertEqual(dropped, [self.BOARD, self.MENU])
        self.assertFalse(any(line.startswith(self.MENU) for line in kept))

    def test_a_plan_that_names_nothing_keeps_no_line(self):
        kept, dropped = self.split({"steps": []})
        self.assertEqual(kept, [])
        self.assertEqual(dropped, [self.BOARD, self.DECK, self.MENU])

    MODULE = "src/pkg/mod.py"
    OTHER = "src/pkg/other.py"
    PY_BY_FILE = {
        MODULE: ["def f(x: int) -> int -- defined in pkg.mod"],
        OTHER: ["def unused() -> None -- defined in pkg.other"],
    }
    PY_PLAN = json.dumps({"steps": [{
        "id": "S1",
        "contracts": {"requires": ["def f(x: int) -> int -- defined in pkg.mod"],
                      "provides": ["def g(x: int) -> int"]},
        "files_write": ["src/pkg/new.py"],
    }]})

    def brief(self, make) -> str:
        with patch.object(loop, "existing_declarations", return_value=self.PY_BY_FILE), \
             patch.dict(loop.LANGUAGE, loop.LANGUAGES["python"], clear=True), \
             patch.dict(os.environ, {"DISPLAY": ""}), \
             patch("loop.run", return_value=SimpleNamespace(
                 returncode=0, stdout="Python 3.12.3", stderr="")):
            return make()

    def test_both_critic_briefs_leave_out_what_the_plan_does_not_name(self):
        for make in (lambda: brief_critique_coverage(REQUIREMENTS, self.PY_PLAN),
                     lambda: brief_critique_trace(self.PY_PLAN)):
            brief = self.brief(make)
            self.assertIn("    def f(x: int) -> int -- defined in pkg.mod", brief)
            self.assertNotIn("def unused", brief)
            self.assertIn(f"    {self.OTHER}", brief)
            self.assertIn("not shown line by line", brief)

    def test_the_planner_still_sees_every_declaration(self):
        facts = self.brief(loop.environment_facts)
        self.assertIn("def unused", facts)
        self.assertNotIn("not shown line by line", facts)

    def test_a_plan_that_cannot_be_read_is_shown_everything(self):
        brief = self.brief(lambda: brief_critique_trace("{ not json"))
        self.assertIn("def unused", brief)


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


class TheRefineLoop(unittest.TestCase):
    """指摘は、人を挟まずにプランナーへ戻る。

    誰も回さない批評は報告で、報告は、機械が扱うために作られたちょうどその場所で、
    人間を輪の中に残す。指摘の宛先は、どのみちプランナーだ。
    """

    def test_the_brief_says_the_criteria_are_still_open(self):
        # これが `plan propose` との違いだ。何も作られておらず、何も緑でないので
        # P5 は効かず、受け入れ条件はまだ変えられる。だから批評は `plan apply` の
        # 後ではなく前に行う。
        brief = loop.brief_plan_refine("要件", "{}", "1. something is wrong")
        self.assertIn("NOTHING HAS BEEN BUILT YET", brief)
        self.assertIn("still yours to change", brief)
        self.assertIn("something is wrong", brief)

    def test_the_brief_says_the_critic_may_be_wrong(self):
        # 最初の本番の批評は、5件の指摘のうち2件を、環境が用意するファイルに
        # 使った。すべての指摘を満たせと言われたプランナーは、もうあるものを
        # 作るために計画をゆがめていただろう。
        brief = loop.brief_plan_refine("要件", "{}", "1. x")
        self.assertIn("could be wrong", brief)
        self.assertIn("already provides", brief)

    def test_deleting_the_criterion_is_named_as_the_move_not_to_make(self):
        brief = loop.brief_plan_refine("要件", "{}", "1. x")
        self.assertIn("deleting the criterion", brief)
        self.assertIn("expected_tests", brief)


if __name__ == "__main__":
    unittest.main()
