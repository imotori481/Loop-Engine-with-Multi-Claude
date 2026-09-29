"""`loop findings` の中身。クリティックの指摘の、人が直す写しを見せ、書き換える。

保守ユーザーが sudo なしで流す。ホストのダッシュボードも SSH でこれを呼ぶ。
写し（/srv/loop/human/in/CRITIQUE.json）は、plan refine が批評のたびに書いて止まる。
グループ humanw に書き込みを許してあり、プランナーとクリティックは読めない。

書き換えてよいのは、ランナーが人を待っているあいだの、指摘1件の title と evidence
だけ。指摘を消すことも足すこともできない。ランナーも再開のときに同じことを確かめる。

    python3 loop_findings.py show <写し>
    python3 loop_findings.py set <写し>   # 標準入力に {"mode", "index", "title", "evidence"}
"""

from __future__ import annotations

import json
import sys

MODE_JA = {"coverage": "要件を満たすか（coverage）",
           "trace": "使う人の操作で届くか（trace）"}
# 1つの欄の上限。ダッシュボードの入力欄と合わせる。
MAX_CHARS = 4000


def load(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and isinstance(value.get("modes"), dict) else None


def show(path: str) -> int:
    value = load(path)
    if value is None:
        print("今の回にクリティックの指摘は無い")
        return 0
    state = "直せる。直したら 'loop continue'" if value.get("waiting") else "直せない"
    print(f"批評 {value.get('round')}回目（{value.get('at', '')}）: {state}")
    for mode, findings in value["modes"].items():
        print()
        print(f"## {MODE_JA.get(mode, mode)}")
        if not findings:
            print("（指摘なし）")
        for n, finding in enumerate(findings):
            print(f"[{n}] {finding.get('title', '')}")
            if finding.get("machine_would_notice") is False:
                print("    どの関門も気づかない")
            for line in str(finding.get("evidence", "")).splitlines():
                print(f"    {line}")
    return 0


def rewrite(value: dict, request: object) -> str | None:
    """指摘1件の title と evidence を書き換える。断る理由を返す。"""
    if not value.get("waiting"):
        return "ランナーは人を待っていない。直せるのは plan refine が止まっているあいだだけ"
    if not isinstance(request, dict):
        return "JSON のオブジェクトを渡す"
    mode, index = request.get("mode"), request.get("index")
    title, evidence = request.get("title"), request.get("evidence")
    findings = value["modes"].get(mode)
    if not isinstance(findings, list):
        return f"知らないモード: {mode}"
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(findings):
        return f"{mode} に {index} 番の指摘は無い"
    if not isinstance(title, str) or not title.strip():
        return "title が空"
    if not isinstance(evidence, str):
        return "evidence が文字列ではない"
    if len(title) > MAX_CHARS or len(evidence) > MAX_CHARS:
        return f"長すぎる。1つの欄は {MAX_CHARS} 字まで"
    findings[index] = {**findings[index], "title": title.strip(), "evidence": evidence.strip()}
    return None


def set_finding(path: str, text: str) -> int:
    value = load(path)
    if value is None:
        print("loop findings: 直す指摘が無い", file=sys.stderr)
        return 1
    try:
        request = json.loads(text)
    except ValueError as error:
        print(f"loop findings: JSON として読めない: {error}", file=sys.stderr)
        return 1
    refused = rewrite(value, request)
    if refused:
        print(f"loop findings: {refused}", file=sys.stderr)
        return 1
    # 置き換えずに中身を書き直す。human/in は sticky で、ランナーが作ったファイルを
    # 保守ユーザーは置き換えられない。書き込みはグループ humanw に許してある。
    with open(path, "r+", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        handle.truncate()
    print("直した")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("show", "set"):
        print("使い方: loop_findings.py show|set <写し>", file=sys.stderr)
        return 2
    if argv[1] == "show":
        return show(argv[2])
    return set_finding(argv[2], sys.stdin.read())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
