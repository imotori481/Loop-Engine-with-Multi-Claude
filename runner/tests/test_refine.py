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

DRAFT = {
    "tasks.json": '{"steps": [], "language": "python"}',
    "CONTEXT.md": "# CONTEXT\n",
    "SYSTEM_SPEC.md": "# SYSTEM_SPEC\n",
}


class RefineKeepsTheDraft(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
