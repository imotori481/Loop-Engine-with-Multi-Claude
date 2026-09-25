"""ランナーの客観的な状態を読み、人間の判断を追記する。

ダッシュボードは、作業の良し悪しをあえて推し量らない。ランナーがすでに出した
事実を伝え、変わらない特定の要求に対して人間が何を決めたかを記録する。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return records
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            records.append({"event": "UNREADABLE_LEDGER_RECORD", "line": number})
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


# この機械以外の場所から決めてよいもの。作業の差し戻しには判断しか要らない。
# 走行の停止は、スマホから届いてほしい唯一の操作だ。「遊んでみて良かった」と
# 言うのは別の行為になる。レビューがあるのは、どの機械も画面を確かめられない
# からだ。窓を開けない端末に、それを保証させてはならない。
REMOTE_DECISIONS = {
    "review": {"revise"},
    "escalation": {"respond", "stop"},
    "planner": {"respond", "stop"},
}

# 詰まっているのは別々の2者で、詰まった理由も違う。
# ESCALATION.md は、ランナーがステップを基準に通せなかったことを表す。
# PLANNER_ESCALATION.md は、プランナーが状況を読み、許された改訂ではどれも
# 役に立たないと結論したことを表す。これは (b) か (c) で、RUNNER_SPEC 6-2 が
# 人間に残している判断だ。片方に答えても、もう片方に答えたことにはならない。
# だから別々の要求にし、別々の ID を振る。
ESCALATION_FILES = {
    "escalation": ("ESCALATION.md", "実装が停止し、人間の判断を待っています"),
    "planner": ("PLANNER_ESCALATION.md",
                "プランナーが「自分には直せない」と返しました。基準か設計の判断です"),
}


def request_id(kind: str, value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(kind.encode("ascii") + b"\0" + encoded).hexdigest()[:16]


class DashboardState:
    def __init__(self, project: Path, data_dir: Path):
        self.project = project.resolve()
        self.data_dir = data_dir.resolve()
        self.decisions_file = self.data_dir / "decisions.jsonl"
        # サーバはスレッドで動くので、2つの判断が同時に届くことがある。
        # 要求がまだ保留中かを確かめることと、答えを記録することは、分けられ
        # ない1つの操作でなければならない。そうでないと、2つ目の答えが、
        # 1つ目がすでに崩した状態に対して書かれる。
        self._lock = threading.Lock()

    def snapshot(self) -> dict[str, Any]:
        ledger = read_jsonl(self.project / "plan" / "ledger.jsonl")
        tasks = _read_json(self.project / "plan" / "tasks.json", {})
        decisions = read_jsonl(self.decisions_file)
        answered = {
            (item.get("kind"), item.get("request_id"))
            for item in decisions if item.get("event") == "HUMAN_DECISION"
        }
        steps = tasks.get("steps", []) if isinstance(tasks, dict) else []
        step_ids = [step.get("id") for step in steps if isinstance(step, dict)]
        green = []
        for record in ledger:
            if record.get("event") == "GREEN" and record.get("step") not in green:
                green.append(record.get("step"))

        stuck = []
        for kind, (filename, title) in ESCALATION_FILES.items():
            path = self.project / "plan" / filename
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            identifier = request_id(kind, text)
            if (kind, identifier) in answered:
                continue
            stuck.append({"id": identifier, "kind": kind, "title": title, "detail": text})

        all_green = next(
            (record for record in reversed(ledger) if record.get("event") == "ALL_GREEN"),
            None,
        )
        review = None
        if all_green is not None:
            review_id = request_id("review", all_green)
            if ("review", review_id) not in answered:
                review = {
                    "id": review_id,
                    "kind": "review",
                    "title": "全ステップGreenです。成果物を実際に確認してください",
                    "detail": "機械的な受け入れ条件は完了しました。成果物を起動し、承認または差し戻しを記録してください。",
                }

        pending = stuck + ([review] if review is not None else [])
        last = ledger[-1] if ledger else None
        if any(item["kind"] == "planner" for item in stuck):
            phase = "planner_escalated"
        elif stuck:
            phase = "escalated"
        elif review:
            phase = "review_required"
        elif all_green:
            phase = "human_reviewed"
        elif ledger:
            phase = "running" if last and last.get("event") != "RUN_ALL_STOP" else "stopped"
        else:
            phase = "not_started"

        return {
            "project": str(self.project),
            "phase": phase,
            "steps": {"total": len(step_ids), "green": len(green), "ids": step_ids},
            "pending": pending,
            "last_event": last,
            "recent_events": ledger[-50:],
            "decisions": decisions[-50:],
        }

    def decide(self, kind: str, request: str, decision: str, note: str,
               scope: str = "local", user: str = "") -> dict[str, Any]:
        with self._lock:
            return self._decide(kind, request, decision, note, scope, user)

    def _decide(self, kind: str, request: str, decision: str, note: str,
                scope: str, user: str) -> dict[str, Any]:
        snapshot = self.snapshot()
        matching = next(
            (item for item in snapshot["pending"]
             if item["id"] == request and item["kind"] == kind),
            None,
        )
        if matching is None:
            raise ValueError("the request is no longer pending")
        allowed = {
            "review": {"approve", "revise"},
            "escalation": {"respond", "stop"},
            "planner": {"respond", "stop"},
        }
        if decision not in allowed.get(kind, set()):
            raise ValueError("decision is not valid for this request")
        if scope != "local" and decision not in REMOTE_DECISIONS.get(kind, set()):
            raise ValueError(
                "that has to be decided at the machine that can run the result")
        if decision in {"revise", "respond"} and not note.strip():
            raise ValueError("this decision requires a note")
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "event": "HUMAN_DECISION",
            "kind": kind,
            "request_id": request,
            "decision": decision,
            "note": note.strip(),
            # 答えがどこから来たかも、答えの一部だ。スマホから記録した承認と、
            # 机の前で記録した承認は意味が違う。だから、どちらだったかを
            # 記録に残す。
            "scope": scope,
            "user": user,
        }
        self._append(record)
        return record

    def _append(self, record: dict[str, Any]) -> None:
        """1行を追記し、ディスクまで書き出す。

        ファイル全体を読んで一時ファイル越しに書き戻す方式は使わない。それだと
        判断のたびに以前の判断をすべて書き直すことになり、書き直しの途中で
        落ちたり、2つの書き手が競合したりすると、すでに無事だった答えまで
        壊れる。追記なら、すでにあるものには触れない。この記録は、ほかの
        どの仕組みからも作り直せない唯一のものだ。ランナーはテストが何をしたかは
        知っているが、人間が遊んでみて何を結論したかは知らない。
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self.decisions_file.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
