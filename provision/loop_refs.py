"""持ち込む先のライブラリの参照を作る。36-dotnet.sh が root で流す。

プランナーとソルバーがライブラリの関数を推測ではなく実物から知るためにある。取り込んだ
Promete のプロジェクトで、ソルバーはマウスのボタンの名前を知らず、反射で当て推量して
黙って false を返すコードを書いた。

パッケージに入っている XML ドキュメントから、行で引ける2つのファイルを書く。

    TYPES.txt    型を1行ずつ。`Promete.Input.Mouse  <要約>`
    MEMBERS.txt  メンバーを1行ずつ。`P:Promete.Input.MouseButton.IsButtonDown  <要約>`

ソルバーは Grep で型の名前を引けば、そのメンバーが要約ごと並ぶ。XML 自体も写す。

    python3 loop_refs.py <nupkg> <XML の名前> <出力ディレクトリ>
"""

from __future__ import annotations

import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

KINDS = {"T": "type", "M": "method", "P": "property", "F": "field", "E": "event"}


def text_of(element: ET.Element | None) -> str:
    """要約の文。<see cref="T:X"/> は X に、<paramref name="x"/> は x にする。"""
    if element is None:
        return ""
    parts = [element.text or ""]
    for child in element:
        ref = child.get("cref") or child.get("name") or child.get("langword") or ""
        parts.append(re.sub(r"^[A-Z]:", "", ref) or text_of(child))
        parts.append(child.tail or "")
    return " ".join("".join(parts).split())


def members(xml: bytes) -> list[tuple[str, str]]:
    """(名前, 要約) の並び。名前は XML ドキュメントの ID（`P:Promete.X.Y`）。"""
    root = ET.fromstring(xml)
    found = []
    for member in root.iter("member"):
        name = member.get("name") or ""
        if name[:2] in {f"{k}:" for k in KINDS}:
            found.append((name, text_of(member.find("summary"))))
    return found


def write(out: str, xml: bytes) -> int:
    entries = members(xml)
    types = sorted((name[2:], summary) for name, summary in entries if name.startswith("T:"))
    rest = sorted(entries)
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "TYPES.txt"), "w", encoding="utf-8") as handle:
        handle.writelines(f"{name}  {summary}".rstrip() + "\n" for name, summary in types)
    with open(os.path.join(out, "MEMBERS.txt"), "w", encoding="utf-8") as handle:
        handle.writelines(f"{name}  {summary}".rstrip() + "\n" for name, summary in rest)
    return len(types)


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("使い方: loop_refs.py <nupkg> <XML の名前> <出力ディレクトリ>", file=sys.stderr)
        return 2
    package, name, out = argv[1:]
    with zipfile.ZipFile(package) as archive:
        # 対象の版が複数あれば、名前の順で最後のもの（いちばん新しい対象）を取る。
        found = sorted(entry for entry in archive.namelist()
                       if entry.startswith("lib/") and entry.endswith("/" + name))
        if not found:
            print(f"loop_refs: {name} が {package} に無い", file=sys.stderr)
            return 1
        xml = archive.read(found[-1])
    count = write(out, xml)
    with open(os.path.join(out, name), "wb") as handle:
        handle.write(xml)
    print(f"loop_refs: {count} 個の型を {out} に書いた（{found[-1]}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
