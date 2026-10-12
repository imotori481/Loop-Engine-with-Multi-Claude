"""持ち込む先のライブラリの参照を作る。root で流す。

プランナーとソルバーがライブラリの関数を推測ではなく実物から知るためにある。取り込んだ
Promete のプロジェクトで、ソルバーはマウスのボタンの名前を知らず、反射で当て推量して
黙って false を返すコードを書いた。

行で引けるファイルを書く。ソルバーは Grep で名前を引けば、要約ごと並ぶ。

    TYPES.txt    型を1行ずつ。`Promete.Input.Mouse  <要約>`。型の無いライブラリには無い
    MEMBERS.txt  メンバーと関数を1行ずつ。`P:Promete.Input.MouseButton.IsButtonDown  <要約>`

    python3 loop_refs.py xml <nupkg> <XML の名前> <出力ディレクトリ>
        パッケージに入っている XML ドキュメントから。36-dotnet.sh が Promete に使う。
        XML 自体も写す
    python3 loop_refs.py header <ヘッダ> <出力ディレクトリ>
        C のヘッダの関数の宣言と #define から。`loop refs dxlib` が DxLib.h に使う。
        ヘッダ自体も UTF-8 にして写す
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


def write_lines(path: str, entries: list[tuple[str, str]]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(f"{name}  {summary}".rstrip() + "\n" for name, summary in entries)


def from_xml(package: str, name: str, out: str) -> int:
    with zipfile.ZipFile(package) as archive:
        # 対象の版が複数あれば、名前の順で最後のもの（いちばん新しい対象）を取る。
        found = sorted(entry for entry in archive.namelist()
                       if entry.startswith("lib/") and entry.endswith("/" + name))
        if not found:
            print(f"loop_refs: {name} が {package} に無い", file=sys.stderr)
            return 1
        xml = archive.read(found[-1])
    entries = members(xml)
    os.makedirs(out, exist_ok=True)
    write_lines(os.path.join(out, "TYPES.txt"),
                sorted((n[2:], s) for n, s in entries if n.startswith("T:")))
    write_lines(os.path.join(out, "MEMBERS.txt"), sorted(entries))
    with open(os.path.join(out, name), "wb") as handle:
        handle.write(xml)
    print(f"loop_refs: {sum(n.startswith('T:') for n, _ in entries)} 個の型を {out} に書いた"
          f"（{found[-1]}）")
    return 0


# ヘッダの1行の宣言。末尾の // が要約になる。DxLib.h は関数を1行ずつ書く。
DECLARATION = re.compile(r"^\s*extern\s+(?P<decl>[^;/]*\([^;]*\)\s*);\s*(?://\s*(?P<note>.*))?$")
DEFINE = re.compile(r"^\s*#\s*define\s+(?P<name>[A-Za-z_]\w*)\s+(?P<value>[^/\n]*?)\s*"
                    r"(?://\s*(?P<note>.*))?$")


def read_header(path: str) -> str:
    """ヘッダの文。DxLib.h は Shift_JIS なので、UTF-8 で読めなければ cp932 で読む。"""
    data = open(path, "rb").read()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp932", errors="replace")


def header_entries(text: str) -> list[tuple[str, str]]:
    """(宣言, 要約) の並び。関数の宣言と、値を持つ #define。ヘッダの順のまま。"""
    entries = []
    for line in text.splitlines():
        match = DECLARATION.match(line)
        if match:
            decl = " ".join(match.group("decl").split())
            decl = re.sub(r"\(\s*", "(", re.sub(r"\s*\)", ")", decl))
            entries.append((decl, (match.group("note") or "").strip()))
            continue
        match = DEFINE.match(line)
        if match and match.group("value"):
            entries.append((f"#define {match.group('name')} {match.group('value').strip()}",
                            (match.group("note") or "").strip()))
    return entries


def from_header(header: str, out: str) -> int:
    text = read_header(header)
    entries = header_entries(text)
    if not entries:
        print(f"loop_refs: {header} に関数の宣言が無い", file=sys.stderr)
        return 1
    os.makedirs(out, exist_ok=True)
    write_lines(os.path.join(out, "MEMBERS.txt"), entries)
    with open(os.path.join(out, os.path.basename(header)), "w", encoding="utf-8") as handle:
        handle.write(text)
    print(f"loop_refs: {len(entries)} 行を {out} に書いた")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 5 and argv[1] == "xml":
        return from_xml(*argv[2:])
    if len(argv) == 4 and argv[1] == "header":
        return from_header(*argv[2:])
    print("使い方: loop_refs.py xml <nupkg> <XML の名前> <出力> | header <ヘッダ> <出力>",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
