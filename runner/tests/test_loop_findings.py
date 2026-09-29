"""`loop findings` が、人を待っている指摘の title と evidence だけを書き換えること。

`loop now` が、その写しをダッシュボードに渡すこと。

    python3 -m unittest discover -s runner/tests
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "provision"))

import loop_findings  # noqa: E402
import loop_now  # noqa: E402

SHOWN = {"round": 1, "waiting": True, "at": "2026-09-29T12:00:00+0900", "modes": {
    "coverage": [{"title": "条件が要件に逆らう", "evidence": "S2", "machine_would_notice": False}],
    "trace": [],
}}


class RewriteAFinding(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "human" / "in" / "CRITIQUE.json"
        self.path.parent.mkdir(parents=True)
        self.write(SHOWN)

    def write(self, value: dict) -> None:
        self.path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def stored(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def set(self, **request) -> tuple[int, str]:
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = loop_findings.set_finding(str(self.path), json.dumps(request))
        return code, err.getvalue()

    def test_title_and_evidence_are_rewritten_and_the_rest_is_kept(self):
        code, _ = self.set(mode="coverage", index=0, title=" 条件は正しい ", evidence="要件の3行目")
        self.assertEqual(code, 0)
        finding = self.stored()["modes"]["coverage"][0]
        self.assertEqual(finding, {"title": "条件は正しい", "evidence": "要件の3行目",
                                   "machine_would_notice": False})
        self.assertTrue(self.stored()["waiting"])

    def test_nothing_is_rewritten_while_the_runner_is_not_waiting(self):
        self.write({**SHOWN, "waiting": False})
        code, err = self.set(mode="coverage", index=0, title="x", evidence="y")
        self.assertEqual(code, 1)
        self.assertIn("待っていない", err)
        self.assertEqual(self.stored()["modes"]["coverage"][0]["title"], "条件が要件に逆らう")

    def test_a_finding_that_is_not_there_is_refused(self):
        self.assertEqual(self.set(mode="trace", index=0, title="x", evidence="")[0], 1)
        self.assertEqual(self.set(mode="coverage", index=1, title="x", evidence="")[0], 1)
        self.assertEqual(self.set(mode="coverage", index=True, title="x", evidence="")[0], 1)
        self.assertEqual(self.set(mode="other", index=0, title="x", evidence="")[0], 1)

    def test_an_empty_or_long_field_is_refused(self):
        self.assertEqual(self.set(mode="coverage", index=0, title=" ", evidence="")[0], 1)
        self.assertEqual(self.set(mode="coverage", index=0, title="x",
                                  evidence="y" * (loop_findings.MAX_CHARS + 1))[0], 1)

    def test_a_shorter_rewrite_leaves_no_tail_of_the_old_file(self):
        self.write({**SHOWN, "modes": {"coverage": [{**SHOWN["modes"]["coverage"][0],
                                                     "evidence": "長い" * 500}], "trace": []}})
        self.assertEqual(self.set(mode="coverage", index=0, title="x", evidence="y")[0], 0)
        self.assertEqual(self.stored()["modes"]["coverage"][0]["evidence"], "y")

    def test_show_prints_the_findings_with_their_index(self):
        out = io.StringIO()
        with redirect_stdout(out):
            loop_findings.show(str(self.path))
        text = out.getvalue()
        self.assertIn("直せる", text)
        self.assertIn("[0] 条件が要件に逆らう", text)
        self.assertIn("どの関門も気づかない", text)
        self.assertIn("（指摘なし）", text)

    def test_loop_now_hands_the_findings_to_the_dashboard(self):
        logs = self.root / "logs"
        logs.mkdir()
        self.assertEqual(loop_now.findings(str(logs)), SHOWN)
        self.path.unlink()
        self.assertIsNone(loop_now.findings(str(logs)))


if __name__ == "__main__":
    unittest.main()
