"""ライブラリの参照が、パッケージの XML ドキュメントから行で引ける形になること。

    python3 -m unittest discover -s runner/tests
"""

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "provision"))

import loop_refs  # noqa: E402

XML = """<?xml version="1.0"?>
<doc>
  <assembly><name>Promete</name></assembly>
  <members>
    <member name="T:Promete.Input.MouseButton">
      <summary>マウスのボタン入力を表します。</summary>
    </member>
    <member name="P:Promete.Input.MouseButton.IsButtonDown">
      <summary>
        このボタンがこのフレームで押されたかどうかを取得します。
      </summary>
    </member>
    <member name="P:Promete.Windowing.IWindow.Scale">
      <summary>拡大率。<see cref="P:Promete.Windowing.IWindow.Size"/> は変わりません。</summary>
    </member>
  </members>
</doc>
"""


class TheIndex(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.package = self.root / "promete.2.1.0.nupkg"
        with zipfile.ZipFile(self.package, "w") as archive:
            archive.writestr("lib/net10.0/Promete.xml", XML)

    def test_types_and_members_are_one_line_each(self):
        out = self.root / "promete"
        self.assertEqual(loop_refs.main(["loop_refs.py", str(self.package), "Promete.xml",
                                         str(out)]), 0)
        self.assertEqual((out / "TYPES.txt").read_text(encoding="utf-8"),
                         "Promete.Input.MouseButton  マウスのボタン入力を表します。\n")
        members = (out / "MEMBERS.txt").read_text(encoding="utf-8").splitlines()
        self.assertIn("P:Promete.Input.MouseButton.IsButtonDown  "
                      "このボタンがこのフレームで押されたかどうかを取得します。", members)
        # 参照は名前にする。要約の中で何を指しているかが Grep で引ける。
        self.assertIn("P:Promete.Windowing.IWindow.Scale  拡大率。"
                      "Promete.Windowing.IWindow.Size は変わりません。", members)
        self.assertTrue((out / "Promete.xml").is_file())

    def test_a_package_without_the_xml_is_refused(self):
        self.assertEqual(loop_refs.main(["loop_refs.py", str(self.package), "Other.xml",
                                         str(self.root / "out")]), 1)


if __name__ == "__main__":
    unittest.main()
