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
        self.assertEqual(loop_refs.main(["loop_refs.py", "xml", str(self.package), "Promete.xml",
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
        self.assertEqual(loop_refs.main(["loop_refs.py", "xml", str(self.package), "Other.xml",
                                         str(self.root / "out")]), 1)


# DxLib.h は Shift_JIS で、関数を1行ずつ、末尾の // に説明を書く。
HEADER = """#ifndef DX_LIB_H
#define DX_LIB_H
#define MOUSE_INPUT_LEFT		(0x0001)			// マウス左ボタン
#define DX_FUNCTION_START
extern	int			DxLib_Init(					void ) ;						// ライブラリ初期化関数
extern	int			GetMouseInput(				void ) ;						// マウスのボタンの状態を得る
extern	int			DrawBox( int x1, int y1, int x2, int y2, unsigned int Color, int FillFlag ) ;
#endif
"""


class TheHeader(unittest.TestCase):
    def test_declarations_and_defines_are_one_line_each_from_shift_jis(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "DxLib.h").write_bytes(HEADER.encode("cp932"))
            out = root / "dxlib"
            self.assertEqual(loop_refs.main(["loop_refs.py", "header", str(root / "DxLib.h"),
                                             str(out)]), 0)
            members = (out / "MEMBERS.txt").read_text(encoding="utf-8").splitlines()
            self.assertEqual(members, [
                "#define MOUSE_INPUT_LEFT (0x0001)  マウス左ボタン",
                "int DxLib_Init(void)  ライブラリ初期化関数",
                "int GetMouseInput(void)  マウスのボタンの状態を得る",
                "int DrawBox(int x1, int y1, int x2, int y2, unsigned int Color, int FillFlag)",
            ])
            # ソルバーが Read で読めるよう、ヘッダは UTF-8 で写す。
            self.assertIn("マウス左ボタン", (out / "DxLib.h").read_text(encoding="utf-8"))
            self.assertFalse((out / "TYPES.txt").exists())


if __name__ == "__main__":
    unittest.main()
