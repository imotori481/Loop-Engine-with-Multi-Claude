"""`loop now` の JSON を組み立てる。ホストのダッシュボードが SSH で読む。

保守ユーザーが sudo なしで流す。ダッシュボードは端末の無い SSH で呼ぶので、
パスワードを訊けない。台帳のある plan/ は runner だけが読めるので、ここで読むのは
/srv/loop/logs だけだ。ここは humanw 経由で保守ユーザーが読める。

今の回のトークン数も、クリティックの指摘も、ログから読む。写しの台帳に届くのは
コミットのときだけで、計画づくりの途中や、refine で止まったときには届かない。
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
import time

ROLES = ("planner", "critic", "solver")

# 行の形は loop（finish と step）と loop.py（record_usage と cmd_plan_refine）が決める。
# 変えるときはここも合わせる。
LOOP_HEADER = re.compile(r"^=== loop (\w+) 開始 \((\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)\)")
STEP_HEADER = re.compile(r"^--- loop\.py .* \((\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)\)$")
USAGE = re.compile(r"^\[USAGE\] who=(\w+) ")
FIELD = re.compile(r"(\w+)=(\S+)")
KINDS = ("input", "cache_write", "cache_read", "output")
CRITIQUE_HEADER = "=== critique "
ALL_GREEN = "全ステップが緑"


def read(path, parse):
    try:
        with open(path, encoding="utf-8") as handle:
            return parse(handle)
    except (OSError, ValueError):
        return None


def last_result(handle):
    lines = [line.strip()[4:] for line in handle if line.startswith("=== loop ")]
    return lines[-1] if lines else None


def stamp(day: str, clock: str) -> str:
    """ログの時刻を台帳と同じ形にする。ログの時刻は箱の地方時。"""
    return f"{day}T{clock}{time.strftime('%z')}"


def project_logs(logs: str, project: str) -> list[str]:
    """プロジェクトの走行ログを古い順に。名前は loop の start が付ける <project>-<日付>-<時刻>.log。

    <project>-latest.log は同じものへのリンクなので外す。名前は日付と時刻の形で
    照らす。`game-[0-9]*` では、game-2 というプロジェクトのログまで拾う。
    """
    name = re.compile(re.escape(project) + r"-\d{8}-\d{6}\.log")
    return sorted(path for path in glob.glob(os.path.join(logs, glob.escape(project) + "-*.log"))
                  if name.fullmatch(os.path.basename(path)))


def add_usage(loop: dict, line: str) -> None:
    """`[USAGE]` の1行を、役割ごとのトークン数、種類別、USD に足す。

    read と write を持たない行（それを出す前のランナーのもの）が1行でもあれば、
    種類別は None にする。入力のうちキャッシュの分が分からない。
    """
    fields = dict(FIELD.findall(line))
    who = fields.get("who")
    if who not in ROLES:
        return
    try:
        tokens_in, tokens_out = int(fields["in"]), int(fields["out"])
    except (KeyError, ValueError):
        return
    loop["tokens"][who] += tokens_in + tokens_out
    loop["calls"] += 1
    try:
        loop["usd"][who] += float(fields.get("usd", 0))
    except ValueError:
        pass
    if loop["kinds"] is None:
        return
    try:
        read, write = int(fields["read"]), int(fields["write"])
    except (KeyError, ValueError):
        loop["kinds"] = None
        return
    kinds = loop["kinds"][who]
    kinds["input"] += tokens_in - read - write
    kinds["cache_write"] += write
    kinds["cache_read"] += read
    kinds["output"] += tokens_out


def current_loop(paths: list[str]) -> dict | None:
    """最後の `loop go` から後のログを足し、今の回のトークン数と最後の指摘を返す。

    1回は `loop go` から完了まで。途中の `loop continue` は同じ回に入る。
    `loop go` のログが無ければ None。
    """
    loop = None
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        header = LOOP_HEADER.match(lines[0]) if lines else None
        if header and header.group(1) == "go":
            loop = {"started": stamp(header.group(2), header.group(3)), "calls": 0,
                    "tokens": dict.fromkeys(ROLES, 0),
                    "kinds": {role: dict.fromkeys(KINDS, 0) for role in ROLES},
                    "usd": dict.fromkeys(ROLES, 0.0), "critique": None}
        if loop is None:
            continue
        at = None
        block = None
        for line in lines:
            if block is not None:
                if not line.startswith(("[", "===", "---")):
                    block.append(line)
                    continue
                loop["critique"] = {"at": at, "text": "\n".join(block).strip()}
                block = None
            step = STEP_HEADER.match(line)
            if step:
                at = stamp(step.group(1), step.group(2))
            if USAGE.match(line):
                add_usage(loop, line)
            if line.startswith(CRITIQUE_HEADER):
                block = [line]
        if block is not None:
            loop["critique"] = {"at": at, "text": "\n".join(block).strip()}
    return loop


def outcome(running: bool, last: str | None) -> str:
    if running:
        return "running"
    return "green" if last and ALL_GREEN in last else "stopped"


def findings(logs: str) -> dict | None:
    """plan refine が書いた、人が直す指摘の写し。human/in は humanw で読める。

    bootstrap が消すので、あれば今の計画への批評だ。waiting なら人が直してよい。
    """
    path = os.path.join(os.path.dirname(logs), "human", "in", "CRITIQUE.json")
    value = read(path, json.load)
    return value if isinstance(value, dict) and isinstance(value.get("modes"), dict) else None


def head_branch(repo: str) -> str | None:
    """bare の HEAD が指すブランチ。HEAD は runner の所有だが、誰でも読める。"""
    value = read(os.path.join(repo, "HEAD"), lambda handle: handle.read().strip())
    prefix = "ref: refs/heads/"
    return value[len(prefix):] if value and value.startswith(prefix) else None


def projects(logs: str, current: str) -> list[dict]:
    """箱のプロジェクトの一覧。loop-project.sh の list と同じ見分け方をする。

    parked/ は root だけが入れるが、/srv/loop/projects/<名前> は誰でも読めるので、
    あるかどうかは分かる。
    """
    root = os.path.join(os.path.dirname(logs), "projects")
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    found = []
    for name in names:
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        state = ("current" if name == current
                 else "parked" if os.path.isdir(os.path.join(path, "parked")) else "new")
        found.append({"name": name, "state": state,
                      "branch": head_branch(os.path.join(path, "repo.git"))})
    return found


def switch(logs: str, moving: bool) -> dict | None:
    """ダッシュボードが始めたプロジェクトの切り替え。出力の末尾を添える。"""
    tail = read(os.path.join(logs, "switch.log"),
                lambda handle: handle.read().splitlines()[-20:])
    if tail is None and not moving:
        return None
    return {"running": moving, "log": tail or []}


def main(argv: list[str]) -> int:
    project, active, logs, moving = argv[1:5]
    running = active == "true"
    last = read(os.path.join(logs, f"{project}-latest.log"), last_result)
    loop = current_loop(project_logs(logs, project))
    if loop is not None:
        loop["outcome"] = outcome(running, last)
    print(json.dumps({"project": project, "running": running,
                      "now": read(os.path.join(logs, "now.json"), json.load),
                      "last": last, "loop": loop, "findings": findings(logs),
                      "projects": projects(logs, project),
                      "switch": switch(logs, moving == "true")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
