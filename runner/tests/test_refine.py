"""plan refine が、上限の後に残った指摘と、改訂が済まずに止まった回の指摘を
人に直させ、直した指摘で1回だけ改訂させること。
改訂に失敗しても改訂前の提案を失わないこと。

改訂を頼む前に out/ は空になる。プランナーがエスカレーションしたとき、または
改訂がリンタを通らなかったとき、リンタを通っていた改訂前の提案が消えていた。

    python3 -m unittest discover -s runner/tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop  # noqa: E402

RUN_CRITIQUE = loop.run_critique
RENDER_FINDINGS = loop.render_findings

DRAFT = {
    "tasks.json": '{"steps": [], "language": "python"}',
    "CONTEXT.md": "# CONTEXT\n",
    "SYSTEM_SPEC.md": "# SYSTEM_SPEC\n",
}


class PendingDraft(unittest.TestCase):
    """out/ に改訂前の提案がある状態。テストは持たない。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.out = root / "out"
        self.out.mkdir()
        for name, text in DRAFT.items():
            (self.out / name).write_text(text, encoding="utf-8")
        self.escalation = root / "refine-escalation.md"
        self.state = root / "refine.json"
        self.critique = root / "CRITIQUE.json"

        patches = [
            mock.patch.object(loop, "PLANNER_OUT", self.out),
            mock.patch.object(loop, "REFINE_ESCALATION", self.escalation),
            mock.patch.object(loop, "REFINE_STATE", self.state),
            mock.patch.object(loop, "CRITIQUE_FOR_HUMAN", self.critique),
            mock.patch.object(loop, "REQUIREMENTS", root / "missing.md"),
            mock.patch.object(loop, "load_settings", lambda _: None),
            mock.patch.object(loop, "run_critique",
                              lambda modes, tasks: {"trace": [{"title": "x"}]}),
            mock.patch.object(loop, "render_findings", lambda by_mode: "1. x"),
            mock.patch.object(loop, "ledger", lambda *a, **k: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.temp.cleanup)

    def contents(self) -> dict[str, str]:
        return {p.name: p.read_text(encoding="utf-8") for p in self.out.iterdir()}


class RefineKeepsTheDraft(PendingDraft):
    def test_an_escalation_puts_the_draft_back(self) -> None:
        def escalate(brief_for, tag, keep=None):
            loop.clear_proposal()
            (self.out / loop.ESCALATE_NAME).write_text("人間に訊く", encoding="utf-8")
            return 0

        with mock.patch.object(loop, "plan_with_retry", escalate):
            self.assertEqual(loop.cmd_plan_refine(["trace"]), 3)
        self.assertEqual(self.contents(), DRAFT)
        # 提案は戻したので、エスカレーションの本文は控えにしか残らない。
        self.assertEqual(self.escalation.read_text(encoding="utf-8"), "人間に訊く")

    def test_a_failed_revision_puts_the_draft_back(self) -> None:
        def fail(brief_for, tag, keep=None):
            loop.clear_proposal()
            (self.out / "tasks.json").write_text("{}", encoding="utf-8")
            return 4

        with mock.patch.object(loop, "plan_with_retry", fail):
            self.assertEqual(loop.cmd_plan_refine(["trace"]), 4)
        self.assertEqual(self.contents(), DRAFT)


FINDINGS = [{"title": "条件が要件に逆らう", "evidence": "S2 の条件", "machine_would_notice": False},
            {"title": "画面から届かない", "evidence": "S3", "machine_would_notice": True}]


class TheHumanRewritesWhatIsLeftAfterTheCap(PendingDraft):
    """上限まで自動で回し、残った指摘だけを人が直す。直した指摘で1回だけ改訂する。"""

    def setUp(self) -> None:
        super().setUp()
        self.critiques = 0
        for p in (mock.patch.object(loop, "render_findings", RENDER_FINDINGS),
                  mock.patch.object(loop, "run_critique", self.critic),
                  mock.patch.dict(loop.LIMITS, {"critiques": 1})):
            p.start()
            self.addCleanup(p.stop)
        self.briefs: list[str] = []

    def critic(self, modes, tasks):
        self.critiques += 1
        return {"trace": [dict(f) for f in FINDINGS]}

    def planner(self, brief_for, tag, keep=None):
        self.briefs.append(brief_for(""))
        (self.out / "tasks.json").write_text(
            json.dumps({"steps": [f"revised {len(self.briefs)}"]}), encoding="utf-8")
        return 0

    def reach_the_cap(self) -> None:
        with mock.patch.object(loop, "plan_with_retry", self.planner):
            self.assertEqual(loop.cmd_plan_refine(["trace"]), 4)
        self.briefs.clear()
        self.critiques = 0

    def shown(self) -> dict:
        return json.loads(self.critique.read_text(encoding="utf-8"))

    def rewrite(self, mode: str, index: int, **fields) -> None:
        value = self.shown()
        value["modes"][mode][index].update(fields)
        self.critique.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def resume(self) -> int:
        with mock.patch.object(loop, "plan_with_retry", self.planner):
            return loop.cmd_plan_refine(["trace"], resume=True)

    def test_rounds_before_the_cap_revise_without_waiting(self) -> None:
        with mock.patch.object(loop, "plan_with_retry", self.planner):
            self.assertEqual(loop.cmd_plan_refine(["trace"]), 4)
        # 1回目の指摘で自動で改訂し、2回目の指摘で止まる。
        self.assertEqual(self.critiques, 2)
        self.assertEqual(len(self.briefs), 1)
        self.assertNotIn("REWRITTEN BY THE HUMAN\n", self.briefs[0].split("# What the critic found")[1])
        shown = self.shown()
        self.assertTrue(shown["waiting"])
        self.assertEqual(shown["round"], 2)
        self.assertEqual(shown["modes"]["trace"][0]["title"], "条件が要件に逆らう")

    def test_the_rewritten_findings_are_revised_once_without_another_critique(self) -> None:
        self.reach_the_cap()
        self.rewrite("trace", 0, title="条件は正しい。S2 の説明だけ直す", evidence="要件の3行目")
        self.assertEqual(self.resume(), 0)

        self.assertEqual(self.critiques, 0)
        self.assertEqual(len(self.briefs), 1)
        brief = self.briefs[0]
        self.assertIn("1. 条件は正しい。S2 の説明だけ直す\n   REWRITTEN BY THE HUMAN", brief)
        self.assertIn("要件の3行目", brief)
        self.assertIn("2. 画面から届かない\n   S3", brief)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.shown()["waiting"])

    def test_nothing_rewritten_leaves_the_proposal_as_it_is(self) -> None:
        self.reach_the_cap()
        before = self.contents()
        self.assertEqual(self.resume(), 0)
        self.assertEqual(self.briefs, [])
        self.assertEqual(self.contents(), before)
        self.assertFalse(self.state.exists())

    def test_a_failed_revision_after_the_rewrite_puts_the_draft_back(self) -> None:
        self.reach_the_cap()
        before = self.contents()
        self.rewrite("trace", 1, title="画面の遷移を足す")

        def fail(brief_for, tag, keep=None):
            loop.clear_proposal()
            return 2

        with mock.patch.object(loop, "plan_with_retry", fail):
            self.assertEqual(loop.cmd_plan_refine(["trace"], resume=True), 2)
        self.assertEqual(self.contents(), before)
        # 次の `loop continue` は、同じ指摘で改訂し直さずに下書きを適用する。
        self.assertFalse(self.state.exists())

    def test_a_finding_cannot_be_removed(self) -> None:
        self.reach_the_cap()
        value = self.shown()
        value["modes"]["trace"].pop()
        self.critique.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(loop.Halt) as caught:
            self.resume()
        self.assertIn("different number of findings", caught.exception.reason)
        # 直してから流し直せるよう、控えは残す。
        self.assertTrue(self.state.exists())
        self.assertEqual(self.briefs, [])

    def test_an_empty_title_is_refused(self) -> None:
        self.reach_the_cap()
        self.rewrite("trace", 1, title="  ")
        with self.assertRaises(loop.Halt):
            self.resume()

    def test_a_proposal_changed_after_the_critique_is_refused(self) -> None:
        self.reach_the_cap()
        (self.out / "tasks.json").write_text('{"steps": ["by hand"]}', encoding="utf-8")
        with self.assertRaises(loop.Halt) as caught:
            self.resume()
        self.assertIn("changed after it was critiqued", caught.exception.reason)

    def test_resuming_with_nothing_waiting_does_nothing(self) -> None:
        self.assertEqual(self.resume(), 0)
        self.assertEqual(self.briefs, [])
        self.assertEqual(self.critiques, 0)

    def test_a_clean_critique_does_not_wait(self) -> None:
        with mock.patch.object(loop, "run_critique", lambda modes, tasks: {"trace": []}):
            self.assertEqual(loop.cmd_plan_refine(["trace"]), 0)
        self.assertFalse(self.shown()["waiting"])
        self.assertFalse(self.state.exists())

    def test_an_escalation_before_the_cap_waits_for_the_rewrite(self) -> None:
        def escalate(brief_for, tag, keep=None):
            loop.clear_proposal()
            (self.out / loop.ESCALATE_NAME).write_text("人間に訊く", encoding="utf-8")
            return 0

        with mock.patch.object(loop, "plan_with_retry", escalate):
            self.assertEqual(loop.cmd_plan_refine(["trace"]), 3)
        shown = self.shown()
        self.assertTrue(shown["waiting"])
        self.assertEqual(shown["round"], 1)

        self.critiques = 0
        self.rewrite("trace", 0, title="範囲は既存のファイルの分割まで")
        self.assertEqual(self.resume(), 0)
        self.assertEqual(self.critiques, 0)
        self.assertEqual(len(self.briefs), 1)
        self.assertIn("1. 範囲は既存のファイルの分割まで\n   REWRITTEN BY THE HUMAN", self.briefs[0])
        self.assertFalse(self.shown()["waiting"])

    def test_a_revision_that_went_through_does_not_wait(self) -> None:
        with mock.patch.dict(loop.LIMITS, {"critiques": 2}), \
                mock.patch.object(loop, "run_critique",
                                  lambda modes, tasks: {"trace": []} if self.briefs else
                                  {"trace": [dict(f) for f in FINDINGS]}), \
                mock.patch.object(loop, "plan_with_retry", self.planner):
            self.assertEqual(loop.cmd_plan_refine(["trace"]), 0)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.shown()["waiting"])

    def test_a_failed_revision_before_the_cap_waits_for_the_rewrite(self) -> None:
        def fail(brief_for, tag, keep=None):
            loop.clear_proposal()
            return 2

        with mock.patch.object(loop, "plan_with_retry", fail):
            self.assertEqual(loop.cmd_plan_refine(["trace"]), 2)
        self.assertTrue(self.shown()["waiting"])
        self.assertEqual(self.contents(), DRAFT)

    def stop_mid_revision(self, partial: dict[str, str]) -> None:
        """改訂の途中で走行を止められた。改訂の後の処理は1つも走らない。"""
        def stopped(brief_for, tag, keep=None):
            loop.clear_proposal()
            for name, text in partial.items():
                (self.out / name).write_text(text, encoding="utf-8")
            raise KeyboardInterrupt

        with mock.patch.object(loop, "plan_with_retry", stopped), \
                self.assertRaises(KeyboardInterrupt):
            loop.cmd_plan_refine(["trace"])
        shown = self.shown()
        self.assertTrue(shown["waiting"])
        self.assertEqual(shown["round"], 1)

    def test_a_run_stopped_mid_revision_waits_and_resumes_from_the_draft(self) -> None:
        self.stop_mid_revision({"tasks.json": '{"steps": ["half written"]}'})
        self.critiques = 0
        self.rewrite("trace", 0, title="S2 の条件は要件どおり")
        self.assertEqual(self.resume(), 0)
        self.assertEqual(self.critiques, 0)
        self.assertEqual(len(self.briefs), 1)
        # 改訂は書きかけではなく、批評した提案に対して頼む。
        self.assertIn(DRAFT["tasks.json"], self.briefs[0])
        self.assertIn("1. S2 の条件は要件どおり\n   REWRITTEN BY THE HUMAN", self.briefs[0])
        self.assertEqual(self.contents()["CONTEXT.md"], DRAFT["CONTEXT.md"])
        self.assertFalse(self.state.exists())

    def test_a_run_stopped_with_out_empty_resumes_without_a_rewrite(self) -> None:
        self.stop_mid_revision({})
        self.assertEqual(self.contents(), {})
        self.assertEqual(self.resume(), 0)
        self.assertEqual(self.briefs, [])
        self.assertEqual(self.contents(), DRAFT)
        self.assertFalse(self.shown()["waiting"])

    def test_applying_the_plan_closes_the_waiting_critique(self) -> None:
        self.reach_the_cap()
        loop.settle_critique()
        shown = self.shown()
        self.assertFalse(shown["waiting"])
        self.assertEqual(shown["modes"]["trace"][0]["title"], "条件が要件に逆らう")


class NoCritiqueIsNotClean(PendingDraft):
    """要件が無いと coverage は走らない。それを指摘ゼロと呼ばないこと。"""

    def setUp(self) -> None:
        super().setUp()
        # setUp が差し替えた run_critique を本物に戻す。critic は呼ばれては
        # ならないので、呼ばれたら落ちるようにしておく。
        for p in (mock.patch.object(loop, "run_critique", RUN_CRITIQUE),
                  mock.patch.object(loop, "call_critic",
                                    mock.Mock(side_effect=AssertionError("critic ran")))):
            p.start()
            self.addCleanup(p.stop)

    def test_refine_stops_instead_of_calling_it_clean(self) -> None:
        planner = mock.Mock()
        with mock.patch.object(loop, "plan_with_retry", planner):
            self.assertEqual(loop.cmd_plan_refine(["coverage"]), 1)
        planner.assert_not_called()
        self.assertEqual(self.contents(), DRAFT)

    def test_critique_stops_instead_of_calling_it_clean(self) -> None:
        self.assertEqual(loop.cmd_critique(["coverage"]), 1)


class ARevisionKeepsWhatItDidNotRewrite(unittest.TestCase):
    """改訂のブリーフは CONTEXT.md と SYSTEM_SPEC.md を「変えるときだけ書け」と言う。

    out/ は呼ぶ前に空にされ、未適用の計画は B1 で3ファイルを要求される。
    補わないと、ブリーフに従ったプランナーが落ちる。run 8 で2回起きた。
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        self.events: list[str] = []
        # B1 と同じ判定だけを残す。3ファイル揃っていれば通る。
        b1 = lambda proposal: [] if {"tasks.json", "CONTEXT.md", "SYSTEM_SPEC.md"} \
            <= set(proposal) else ["B1: a first plan must include all three files"]
        for p in (mock.patch.object(loop, "PLANNER_OUT", self.out),
                  mock.patch.object(loop, "proposal_problems", b1),
                  mock.patch.object(loop, "ledger",
                                    lambda event, **k: self.events.append(event))):
            p.start()
            self.addCleanup(p.stop)

    def planner_writes(self, files: dict[str, str]):
        def call(brief):
            for name, text in files.items():
                (self.out / name).write_text(text, encoding="utf-8")
            return ""
        return mock.patch.object(loop, "call_planner", call)

    def test_the_files_it_left_alone_are_carried_from_the_draft(self) -> None:
        keep = {"CONTEXT.md": DRAFT["CONTEXT.md"], "SYSTEM_SPEC.md": DRAFT["SYSTEM_SPEC.md"]}
        with self.planner_writes({"tasks.json": '{"steps": ["revised"]}'}):
            self.assertEqual(loop.plan_with_retry(lambda f: "", "T", keep=keep), 0)
        self.assertEqual((self.out / "CONTEXT.md").read_text(encoding="utf-8"),
                         DRAFT["CONTEXT.md"])
        self.assertEqual((self.out / "tasks.json").read_text(encoding="utf-8"),
                         '{"steps": ["revised"]}')
        self.assertIn("PLAN_CARRIED", self.events)

    def test_what_it_did_rewrite_is_not_overwritten(self) -> None:
        keep = {"CONTEXT.md": "old", "SYSTEM_SPEC.md": "old"}
        with self.planner_writes({"tasks.json": "{}", "CONTEXT.md": "new"}):
            loop.plan_with_retry(lambda f: "", "T", keep=keep)
        self.assertEqual((self.out / "CONTEXT.md").read_text(encoding="utf-8"), "new")
        self.assertEqual((self.out / "SYSTEM_SPEC.md").read_text(encoding="utf-8"), "old")

    def test_an_escalation_is_left_on_its_own(self) -> None:
        keep = {"CONTEXT.md": "old", "SYSTEM_SPEC.md": "old"}
        with self.planner_writes({loop.ESCALATE_NAME: "人間に訊く"}):
            self.assertEqual(loop.plan_with_retry(lambda f: "", "T", keep=keep), 0)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), [loop.ESCALATE_NAME])


if __name__ == "__main__":
    unittest.main()
