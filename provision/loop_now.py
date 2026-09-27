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
USAGE = re.compile(r"^\[USAGE\] who=(\w+) .*? in=(\d+) out=(\d+)")
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
                    "tokens": dict.fromkeys(ROLES, 0), "critique": None}
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
            usage = USAGE.match(line)
            if usage and usage.group(1) in ROLES:
                loop["tokens"][usage.group(1)] += int(usage.group(2)) + int(usage.group(3))
                loop["calls"] += 1
            if line.startswith(CRITIQUE_HEADER):
                block = [line]
        if block is not None:
            loop["critique"] = {"at": at, "text": "\n".join(block).strip()}
    return loop


def outcome(running: bool, last: str | None) -> str:
    if running:
        return "running"
    return "green" if last and ALL_GREEN in last else "stopped"


def main(argv: list[str]) -> int:
    project, active, logs = argv[1:4]
    running = active == "true"
    last = read(os.path.join(logs, f"{project}-latest.log"), last_result)
    loop = current_loop(project_logs(logs, project))
    if loop is not None:
        loop["outcome"] = outcome(running, last)
    print(json.dumps({"project": project, "running": running,
                      "now": read(os.path.join(logs, "now.json"), json.load),
                      "last": last, "loop": loop}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
