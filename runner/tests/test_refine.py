"""plan refine が、改訂に失敗しても改訂前の提案を失わないこと。

改訂を頼む前に out/ は空になる。プランナーがエスカレーションしたとき、または
改訂がリンタを通らなかったとき、リンタを通っていた改訂前の提案が消えていた。

    python3 -m unittest discover -s runner/tests
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop  # noqa: E402

RUN_CRITIQUE = loop.run_critique

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

        patches = [
            mock.patch.object(loop, "PLANNER_OUT", self.out),
            mock.patch.object(loop, "REFINE_ESCALATION", self.escalation),
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
        def escalate(brief_for, tag):
            loop.clear_proposal()
            (self.out / loop.ESCALATE_NAME).write_text("人間に訊く", encoding="utf-8")
            return 0

        with mock.patch.object(loop, "plan_with_retry", escalate):
            self.assertEqual(loop.cmd_plan_refine(["trace"]), 3)
        self.assertEqual(self.contents(), DRAFT)
        # 提案は戻したので、エスカレーションの本文は控えにしか残らない。
        self.assertEqual(self.escalation.read_text(encoding="utf-8"), "人間に訊く")

    def test_a_failed_revision_puts_the_draft_back(self) -> None:
        def fail(brief_for, tag):
            loop.clear_proposal()
            (self.out / "tasks.json").write_text("{}", encoding="utf-8")
            return 4

        with mock.patch.object(loop, "plan_with_retry", fail):
            self.assertEqual(loop.cmd_plan_refine(["trace"]), 4)
        self.assertEqual(self.contents(), DRAFT)


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


if __name__ == "__main__":
    unittest.main()
