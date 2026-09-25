"""レポートを読む部分を、直接確かめる。

関門の算術は parse_junit にある。RED_GATE も VERIFY も、それに問いを投げる
だけだ。ここを確かめないと何が起きるかは、実際の走行で2度現れた。R5 が
誤った属性を読んで正直なアサーションを拒み、`green` がスキップを無視して、
走らなくなったテストを通ったテストと数えた。

ランナーと同じく標準ライブラリだけを使う。venv は要らない:

    python3 -m unittest discover -s runner/tests
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loop import parse_junit  # noqa: E402


def report(cases: str, **counts: int) -> str:
    attrs = " ".join(f'{k}="{v}"' for k, v in counts.items())
    return f'<testsuites><testsuite name="pytest" {attrs}>{cases}</testsuite></testsuites>'


PASS = '<testcase classname="tests.test_models" name="test_rate"/>'
FAIL = ('<testcase classname="tests.test_models" name="test_tick">'
        '<failure message="assert nan == 5.0">tests/test_models.py:16: AssertionError'
        '</failure></testcase>')
ERROR = ('<testcase classname="tests.test_models" name="test_import">'
         '<error message="collection failure">tests/test_models.py:1: ImportError'
         '</error></testcase>')
SKIP = ('<testcase classname="tests.test_models" name="test_save">'
        '<skipped type="pytest.skip" message="not implemented"/></testcase>')


class ReadingAReport(unittest.TestCase):
    def parse(self, xml: str):
        path = Path(self.tmp.name) / "report.xml"
        path.write_text(xml, encoding="utf-8")
        return parse_junit(path, output="(pytest output)")

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_every_test_passed(self) -> None:
        run = self.parse(report(PASS * 2, tests=2, failures=0, errors=0, skipped=0))
        self.assertTrue(run.green)
        self.assertEqual(run.tests, 2)
        self.assertEqual(run.passed_names, ["test_rate", "test_rate"])
        self.assertEqual(run.failed_files, [])

    def test_a_failure_is_not_green(self) -> None:
        run = self.parse(report(PASS + FAIL, tests=2, failures=1, errors=0, skipped=0))
        self.assertFalse(run.green)
        self.assertEqual(run.failures, 1)
        self.assertEqual(run.failed_files, ["tests.test_models"])
        # R5 はクラスを message ではなく本文の最後の行から読む。説明が複数行に
        # わたると pytest は "AssertionError: " の接頭辞を落とし、属性について
        # のアサーションはたいていそうなる。
        self.assertEqual(run.failure_kinds, ["AssertionError"])

    def test_an_error_is_not_green(self) -> None:
        run = self.parse(report(PASS + ERROR, tests=2, failures=0, errors=1, skipped=0))
        self.assertFalse(run.green)
        self.assertEqual(run.errors, 1)
        self.assertEqual(run.failed_files, ["tests.test_models"])
        self.assertEqual(run.passed_names, ["test_rate"])

    def test_a_skip_is_not_green(self) -> None:
        # このファイルを書いた理由の穴。pytest はこの走行を成功と呼ぶ。失敗も
        # エラーも無く、終了コードは 0 だ。2件のうち1件は走っておらず、スイートは
        # 主張の半分しか証明していない。
        run = self.parse(report(PASS + SKIP, tests=2, failures=0, errors=0, skipped=1))
        self.assertFalse(run.green)
        self.assertEqual(run.skipped, 1)
        self.assertEqual(run.skipped_names, ["tests.test_models::test_save"])
        # スキップされたテストは通ったテストではなく、そう数えてはならない。
        # RED_GATE の R4 は、スタブに対してすでに通るものがあるかを訊く。
        self.assertEqual(run.passed_names, ["test_rate"])

    def test_an_empty_suite_is_not_green(self) -> None:
        # 何も走らなかったので、何も落ちなかった。RED_GATE は R1 で expected_tests
        # と比べて捕まえる。VERIFY には期待する件数が無いので、これに頼る。
        run = self.parse(report("", tests=0, failures=0, errors=0, skipped=0))
        self.assertFalse(run.green)
        self.assertEqual(run.tests, 0)


class WhenTheReportCannotBeRead(unittest.TestCase):
    """読めないものはエラーで、無いものではない。何も言わないレポートを「何も
    落ちなかった」と読んではならない。起きてもいない走行でステップが通る。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "report.xml"

    def test_no_file_at_all(self) -> None:
        run = parse_junit(self.path, output="pytest: command not found")
        self.assertFalse(run.green)
        self.assertEqual(run.errors, 1)
        self.assertEqual(run.tests, 0)

    def test_truncated_xml(self) -> None:
        # 殺された pytest が残すもの。
        self.path.write_text('<testsuites><testsuite tests="3"', encoding="utf-8")
        run = parse_junit(self.path)
        self.assertFalse(run.green)
        self.assertEqual(run.errors, 1)

    def test_well_formed_xml_that_is_not_a_report(self) -> None:
        self.path.write_text("<something-else/>", encoding="utf-8")
        run = parse_junit(self.path)
        self.assertFalse(run.green)
        self.assertEqual(run.errors, 1)


if __name__ == "__main__":
    unittest.main()
