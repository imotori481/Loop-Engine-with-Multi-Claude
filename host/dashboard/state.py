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


# グラフに出す役。台帳の USAGE の who と同じ名前を使う。
ROLES = ("planner", "critic", "solver")


# トークンの種類と、USAGE の usage の中での名前。
KINDS = {"input": "input_tokens", "cache_write": "cache_creation_input_tokens",
         "cache_read": "cache_read_input_tokens", "output": "output_tokens"}


def usage_kinds(record: dict[str, Any]) -> dict[str, int]:
    """USAGE 1件の、種類別のトークン数。"""
    usage = record.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    kinds = {}
    for kind, key in KINDS.items():
        value = usage.get(key)
        kinds[kind] = int(value) if isinstance(value, (int, float)) else 0
    return kinds


def usage_tokens(record: dict[str, Any]) -> int:
    """USAGE 1件のトークン数。数え方はランナーの画面の in と out に合わせる。

    入力は、キャッシュから読んだ分と書いた分を足す。
    """
    return sum(usage_kinds(record).values())


def usage_usd(record: dict[str, Any]) -> float:
    value = record.get("cost_usd")
    return float(value) if isinstance(value, (int, float)) else 0.0


def token_runs(ledger: list[dict[str, Any]], source: str = "") -> list[dict[str, Any]]:
    """1つの台帳を、loop go から完了までの回に分け、回ごと役ごとのトークン数を足す。

    tokens は役ごとの合計、kinds は役ごとの種類別、usd は役ごとの cost_usd の和。

    1回は PLAN_BOOTSTRAP から始まり、次の PLAN_BOOTSTRAP の手前で終わる。
    計画づくりと批評は run --all より前に流れるので、RUN_ALL_START では区切らない。
    PLAN_BOOTSTRAP より前に記録があれば、最初の記録から1回目を始める。
    USAGE が1件も無い回は、消費を記録する前の台帳なので返さない。

    結果は最後に起きたものを取る。ALL_GREEN なら完了、RUN_ALL_STOP なら停止。
    次の PLAN_BOOTSTRAP が来た回は、完了していなければ中断とする。
    """
    runs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for record in ledger:
        event = record.get("event")
        if event == "PLAN_BOOTSTRAP" or current is None:
            if current is not None and current["outcome"] != "green":
                current["outcome"] = "abandoned"
            current = {"source": source, "started": record.get("ts", ""),
                       "outcome": "running", "calls": 0,
                       "tokens": dict.fromkeys(ROLES, 0),
                       "kinds": {role: dict.fromkeys(KINDS, 0) for role in ROLES},
                       "usd": dict.fromkeys(ROLES, 0.0)}
            runs.append(current)
        if event == "USAGE" and record.get("who") in ROLES:
            who = record["who"]
            for kind, value in usage_kinds(record).items():
                current["kinds"][who][kind] += value
            current["tokens"][who] += usage_tokens(record)
            current["usd"][who] += usage_usd(record)
            current["calls"] += 1
        elif event == "ALL_GREEN":
            current["outcome"] = "green"
        elif event == "RUN_ALL_STOP":
            current["outcome"] = "stopped"
        elif event == "RUN_ALL_START":
            current["outcome"] = "running"
    return [run for run in runs if run["calls"]]


def ledger_files(root: Path) -> list[Path]:
    """写しの置き場の下にある台帳。loop-pull の projects\\*、runs\\*、project を拾う。

    同じ回が2か所にあるときは先に拾ったほうの名前が残る。project より
    projects\\<name> のほうが、どのプロジェクトかが分かるので先に拾う。
    """
    return sorted(root.glob("*/*/plan/ledger.jsonl")) + sorted(root.glob("*/plan/ledger.jsonl"))


def token_history(root: Path) -> list[dict[str, Any]]:
    """写しの置き場にある全台帳の回を、開始時刻の順に並べて番号を振る。

    1つのプロジェクトの台帳には、ふつう1回分しか入らない。bootstrap は緑の
    ステップがあると断るからだ。だから回の履歴は、写しを横断して作る。
    project は今のプロジェクトの写しで、projects の下と同じ回を持つ。
    開始時刻とトークン数が同じ回は1つにまとめる。
    """
    seen = set()
    runs = []
    for path in ledger_files(root):
        source = path.parent.parent.relative_to(root).as_posix()
        for run in token_runs(read_jsonl(path), source):
            key = (run["started"], tuple(run["tokens"].values()))
            if key in seen:
                continue
            seen.add(key)
            runs.append(run)
    runs.sort(key=lambda run: run["started"])
    for number, run in enumerate(runs, 1):
        run["run"] = number
    return runs


def request_id(kind: str, value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(kind.encode("ascii") + b"\0" + encoded).hexdigest()[:16]


def approved_review(ledger: list[dict[str, Any]],
                    decisions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """写しの最後の ALL_GREEN に対する承認。無ければ None。

    PR に出してよいのは、承認した ALL_GREEN が写しの最後のものである間だけだ。
    次の回が緑になれば、前の承認はその回について何も言っていない。
    """
    all_green = next(
        (record for record in reversed(ledger) if record.get("event") == "ALL_GREEN"), None)
    if all_green is None:
        return None
    review_id = request_id("review", all_green)
    return next((item for item in reversed(decisions)
                 if item.get("event") == "HUMAN_DECISION" and item.get("kind") == "review"
                 and item.get("request_id") == review_id
                 and item.get("decision") == "approve"), None)


class DashboardState:
    def __init__(self, project: Path, data_dir: Path, mirrors: Path | None = None):
        self.project = project.resolve()
        self.data_dir = data_dir.resolve()
        # トークンの履歴を読む写しの置き場。loop-pull は project と同じ場所に
        # projects と runs を置く。
        self.mirrors = mirrors.resolve() if mirrors is not None else self.project.parent
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

        pull_request = None
        approved = approved_review(ledger, decisions)
        if approved is not None:
            pull_request = {"request_id": approved["request_id"], "result": next(
                (item for item in reversed(decisions) if item.get("event") == "PULL_REQUEST"
                 and item.get("request_id") == approved["request_id"]), None)}

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
            "token_runs": token_history(self.mirrors),
            "pull_request": pull_request,
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

    def approved(self) -> dict[str, Any] | None:
        """写しの最後の ALL_GREEN に対する承認と、その ALL_GREEN の記録。"""
        ledger = read_jsonl(self.project / "plan" / "ledger.jsonl")
        approval = approved_review(ledger, read_jsonl(self.decisions_file))
        if approval is None:
            return None
        all_green = next(r for r in reversed(ledger) if r.get("event") == "ALL_GREEN")
        return {**approval, "steps": all_green.get("steps") or []}

    def record_pull_request(self, request: str, outcome: dict[str, Any]) -> dict[str, Any]:
        """PR を出した結果を追記する。成功なら url、失敗なら error を持つ。"""
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": "PULL_REQUEST",
                  "request_id": request, **outcome}
        with self._lock:
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
