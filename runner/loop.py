#!/usr/bin/env python3
"""ループのランナー。v1 で、捨てる前提で書いてある。

    sudo -u runner python3 /srv/loop/runner/loop.py run --all

RUNNER_SPEC が述べる、関門を強制する層。エージェントではなく普通の
プログラムで、そこが要点だ。関門に価値があるのは、判断をしないからだ。
失敗が「惜しい」かどうかを決めるものは、ここには何も無い。

流れは3段で、各段の出口で人がせき止める（RUNNER_SPEC 3 章）:

    上流   仕様書 -> 基本設計仕様書                プランナー <-> 人間
    中段   基本設計 -> モジュールごとの詳細設計    プランナー -> クリティック -> 人間
    下流   モジュールごとの実装                    ソルバー <-> VERIFY

下流はモジュールを1つずつ、次の位相で回す:

    PLAN_LOAD -> TEST_WRITE -> STUB -> RED_GATE -> FREEZE -> IMPL <-> VERIFY -> GREEN

内部では、モジュールを1つの「ステップ」の辞書に直して回す（module_step）。

標準ライブラリだけを使う。`runner` として動き、runner はリポジトリを所有し、
エージェントを起動するための sudo の例外を持つ。
"""

from __future__ import annotations

import argparse
import ast
import atexit
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

LOOP = Path("/srv/loop")
PROJECT = LOOP / "project"
PLAN = PROJECT / "plan"

# 書き込みの柵の場所。プロジェクトごとに決まる（Unity はコードを Assets/ の下に
# 置く）。loop-project.sh が /srv/loop/layout.json を今のプロジェクトのものへの
# リンクにする。root の所有なので、ランナーもソルバーも書き換えられない。無ければ
# src/ と tests/。
LAYOUT_FILE = LOOP / "layout.json"
LAYOUT_DEFAULT = {"src": "src", "tests": "tests"}
# 柵にしてはならない場所。ランナーの持ち物か、凍結したツールチェーンだ。
LAYOUT_RESERVED = {".git", "plan", ".runner", ".venv", "node_modules", "docs", "ac"}


def layout_problems(layout: object) -> list[str]:
    """柵の場所として使えない理由。使えるなら空。loop-project.sh と同じ規則。"""
    if not isinstance(layout, dict) or set(layout) != {"src", "tests"}:
        return ['layout must be {"src": <dir>, "tests": <dir>}']
    problems = []
    parts = {}
    for key, value in layout.items():
        if not isinstance(value, str) or not value:
            problems.append(f"{key} must be a non-empty string")
            continue
        parts[key] = tuple(value.split("/"))
        # 空白や記号は、プロビジョニングの bash が引用し損ねたときに別のパスになる。
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", value):
            problems.append(f"{key} may use only letters, digits, '.', '_', '-' "
                            f"and '/': {value!r}")
        elif value.startswith("/") or value.endswith("/") \
                or any(p in ("", ".", "..") for p in parts[key]):
            problems.append(f"{key} must be a relative path without '.', '..' or "
                            f"empty parts: {value!r}")
        elif parts[key][0] in LAYOUT_RESERVED:
            problems.append(f"{key} may not be under {parts[key][0]}/: {value!r}")
    if len(parts) == 2:
        short, long_ = sorted(parts.values(), key=len)
        if long_[:len(short)] == short:
            problems.append("src and tests may not contain one another")
    return problems


def read_layout() -> dict[str, str]:
    try:
        layout = json.loads(LAYOUT_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(LAYOUT_DEFAULT)
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"cannot read {LAYOUT_FILE}: {e}")
    problems = layout_problems(layout)
    if problems:
        sys.exit(f"{LAYOUT_FILE} is not a usable layout: " + "; ".join(problems))
    return layout


def layout_text(text: str) -> str:
    """ブリーフの文の {SRC} {TESTS} {SRC_FROM_TESTS} を、今の柵の場所に置き換える。

    プランナーに渡す場所は、書き写さずに、ランナーが実際に強制するものから取る。
    """
    from_tests = os.path.relpath(LAYOUT["src"], LAYOUT["tests"]).replace(os.sep, "/")
    return (text.replace("{SRC_FROM_TESTS}", from_tests)
            .replace("{SRC}", LAYOUT["src"]).replace("{TESTS}", LAYOUT["tests"]))


def in_layout(path: str, key: str) -> bool:
    """`path` が柵の `key`（"src" か "tests"）の下にあるか。`..` で抜けるものは外。"""
    parts = path.split("/")
    base = LAYOUT[key].split("/")
    return (len(parts) > len(base) and parts[:len(base)] == base
            and not any(p in ("", ".", "..") for p in parts))


LAYOUT = read_layout()
TESTS = PROJECT / LAYOUT["tests"]
SRC = PROJECT / LAYOUT["src"]
STATE = PROJECT / ".runner"
BRIEF_DIR = LOOP / "brief"
PYTEST = PROJECT / ".venv" / "bin" / "pytest"
# 同じ考え方の TypeScript 側。ツールチェーンは凍結してプロジェクトの外に置き
# （provision/35-node.sh）、solver がたどれるが差し替えられないシンボリック
# リンクで届かせる。
VITEST = PROJECT / "node_modules" / ".bin" / "vitest"
SOLVER_RUN = LOOP / "bin" / "solver-run"
PLANNER_RUN = LOOP / "bin" / "planner-run"
PLANNER_BRIEF = LOOP / "planner" / "brief"
PLANNER_OUT = LOOP / "planner" / "out"
CRITIC_RUN = LOOP / "bin" / "critic-run"
CRITIC_BRIEF = LOOP / "critic" / "brief"
CRITIC_OUT = LOOP / "critic" / "out"
# クリティックが書いてよいファイル名は1つだけ。プランナーが3つなのと同じ理由で、
# 与えられていない名前は、ランナーが読むのを拒む名前だ。
FINDINGS_NAME = "FINDINGS.json"

# 人間の経路で、プランナーには書けない。仕様書と、せきで人が書いた意見がここに
# 届く。どちらも人だけが言ってよいことだ。/srv/loop/planner/out と対になって
# いて、形は同じ、向きは逆、グループは別。
HUMAN_IN = LOOP / "human" / "in"
# 人が書いた仕様書。`loop go` が置き、spec import が docs/spec/ に取り込む。
HUMAN_SPEC = HUMAN_IN / "spec"
# せきで人が書いた意見。次のプランナーの呼び出しに渡し、受け入れたら消す。
HUMAN_COMMENT = HUMAN_IN / "COMMENT.md"
# クリティックの指摘の、人が読む写し。ダッシュボードと `loop findings` が読む。
CRITIQUE_FOR_HUMAN = HUMAN_IN / "CRITIQUE.json"

# 設計仕様書。編集対象のリポジトリの docs/ に段ごとに置き、コミットする。
# 0700 runner で、ソルバーは読めない。ソルバーに要る部分はランナーがブリーフに入れる。
DOCS = PROJECT / "docs"
DOCS_SPEC = DOCS / "spec"        # 人が書いた仕様書
DOCS_BASIC = DOCS / "basic"      # 基本設計仕様書。上流の成果物
DOCS_MODULES = DOCS / "modules"  # 詳細設計仕様書。中段の成果物。src/ と同じ形
# 受け入れ条件。モジュールごとの JSON で、ランナーだけが読む。.gitignore で
# コミットから外す。TEST_WRITE のブリーフにだけ入れる。
AC = PROJECT / "ac"

LEDGER = PLAN / "ledger.jsonl"
# いまの段と、関門が確かめた設計のハッシュ。コミットする。
STAGE_FILE = PLAN / "stage.json"
# 中段の出口の、インターフェースチェックの結果とクリティックの指摘。コミットする。
INTERFACE = PLAN / "INTERFACE.json"
CRITIQUE = PLAN / "CRITIQUE.json"
# 下流で止まったモジュールと、その理由。人が直すモジュールを選ぶために読む。
FAILURE = PLAN / "FAILURE.md"
# 設計仕様書と受け入れ条件から、ランナーが作る下流の計画。受け入れ条件を含む
# ので、コミットしない .runner/ に置く。
TASKS = STATE / "tasks.json"
STAGES = ("upstream", "midstream", "downstream", "review")

# tests/ はプロジェクトの venv の pytest でだけ走らせる。素の
# `python3 -m pytest` にすると、solver 自身の ~/.local のパッケージが sys.path に
# 戻る。「詰まったら pip install」を効かなくしている唯一の仕組みがそれだ
# （RUNNER_SPEC 1-3）。
PYTEST_ARGS = ["-q", "-p", "no:cacheprovider", "--strict-markers"]

# 走行の設定。下流の計画（TASKS）はランナーが設計から作るので、人が上限や
# ソルバーの段を書く場所はこちらにする。root の持ち物で、ランナーは読むだけだ。
# 無ければ下の既定で走る。
SETTINGS_FILE = LOOP / "settings.json"
SETTINGS_KEYS = {"timeouts", "limits", "policy", "solver_tiers"}

# RUNNER_SPEC 10 章が未決にしている値。出発点としての固定値で、どれも
# SETTINGS_FILE の "timeouts" で上書きできる。
#
# 返ってこないテストの実行は、ここでは予想される失敗だ。無限ループする実装は
# ソルバーが普通に書くもので、上限が無ければランナーは永遠に待つ。
#
# エージェント自身の上限は、ここではなく solver-run と planner-run にある。
# ランナーは別の uid のプロセスを止められないので、こちら側だけで掛けた上限は、
# 誰にも止められないエージェントを残す。だからこれらの値は起動スクリプトに
# 「渡し」、ランナー自身の subprocess の上限は、スクリプトの `timeout` が発動
# しなかったときの備えとしてその上に置く。渡していなかったのは実際のバグだった。
# スクリプトの既定は 900 秒なので、ここの数を上げても何も動かず、このファイルの
# 値が 1800 なのに run 5 は 900 秒で2回殺された。
#
# 推測ではなく測った値だ。計画づくりは 8分（run 4）、12分（run 3）、15分超
# （run 5、planner-run の既定 900 秒で殺され、何も書かれなかった）かかった。
# 規則が増えるほど伸びる。規則はどれも、プランナーが書き出す前に計画が満たす
# べきものだからだ。ここで上限が発動すると丸損になる。Claude Code はファイルを
# 最後に書くので、殺された計画の呼び出しからは拾える途中の計画が残らない。
# だから低すぎる設定の代償は、高すぎる設定の代償よりずっと大きい。
TIMEOUTS = {"test": 120, "solver": 960, "planner": 1800, "critic": 900}
# ランナーの備えを、エージェント自身の上限からどれだけ上に置くか。solver-run や
# planner-run の `--kill-after=30` と、出力を書く時間を覆えば足りる。
BACKSTOP_MARGIN = 120

# 利用上限と一時的な混雑。3役は同じサブスクリプションの枠を使うので、上限に
# 当たるのは設計の失敗でもソルバーの失敗でもない。それを Halt にすると
# モジュールが止まって中段に戻り、設計の書き直しがプランナーを呼んで、同じ枠を
# さらに使う。だから見分けて、待ってから同じ呼び出しをやり直す。
#
# 文面は Claude Code のエラーのドキュメントにあるもの。-p では、実行中の
# 失敗はこの文が標準出力に出て、0 以外で終わる。
#   usage_limit  サブスクリプションの枠。Claude Code は自分ではやり直さない
#   rate_limit   429 と 529。Claude Code が何度かやり直したうえで諦めたもの
QUOTA_PATTERNS = {
    "usage_limit": re.compile(r"You[’']ve hit your \w+ limit"),
    "rate_limit": re.compile(r"Request rejected \(429\)|temporarily limiting requests"
                             r"|Repeated 529 Overloaded"),
}
# 1回の待ち時間と、待つ回数の上限。既定は 15分 × 24回 = 6時間で、セッションの
# 枠が戻るまでの時間を覆う。週の枠は覆わない。そのときは待ち切ってから止まる。
QUOTA = {"wait_seconds": 900, "waits": 24}


class QuotaExhausted(Exception):
    """待ち切っても枠が戻らなかった。Halt ではないので、中段に戻さない。"""

    def __init__(self, who: str, phase: str, kind: str):
        super().__init__(f"{who} is still out of quota ({kind}) in {phase}")
        self.who = who
        self.phase = phase
        self.kind = kind


# 箱が書き込みを拒んだ跡。ソルバーにもプランナーにも直せないので、中段に
# 戻さずに止める。柵の下のディレクトリが 2755 で作られ、ソルバーの Write がすべて
# EACCES で落ちたとき、試行をすべてそれで使い切った。
# 誤りと取り違えないよう、箱のパス（/srv/loop の下）と同じ行に出たものだけを見る。
# Node は `EACCES: permission denied, open '<path>'`、Python は
# `[Errno 13] Permission denied: '<path>'` の形で出す。
INFRA_ERROR = re.compile(r"\b(?:EACCES|EPERM|EROFS|ENOSPC)\b|\[Errno (?:1|13|28|30)\]"
                         r"|Permission denied|Read-only file system|No space left on device")


class InfraBroken(Exception):
    """箱が書き込みを拒んだ。Halt ではないので、中段に戻さない。"""

    def __init__(self, phase: str, evidence: str):
        super().__init__(f"the sandbox refused the {phase} phase: {evidence}")
        self.phase = phase
        self.evidence = evidence


# "revisions" は、プランナーが機械の検査に落ちた設計を直す回数の上限だ。
# プランナーは検査の判定を渡されて直すよう頼まれる。ある違反を別の違反と
# 取り替えるエージェントは、上限が無ければそれを永遠に続ける。
# "attempts" は全モジュールの試行の回数で、0 は既定の 3 を意味する。
# "test_writes" は、コンパイルできないテストを書き直させる回数の上限だ。
LIMITS = {"revisions": 3, "attempts": 0, "test_writes": 3}

# ステップの中の次の試行を、どう始めるか。
#
#   "repair"   -- 失敗した木から、何が壊れたかを伝えて始める。ソルバーが自分の
#                 誤りを読んで元に戻せるときに正しい。
#   "resample" -- きれいな木から、RED_GATE の失敗だけを持って始める。修理では
#                 なく、独立した抽選。
#
# 後者があるのは、試行がすべて同じ種類のものではないからだ。何を誤ったかが
# 見えないソルバーは、損傷を元に戻さず積み重ね、後の試行ほど悪い木から始まる。
# 一方でランナーは「厳密な」検証器だ。言いくるめられない関門に対する N 回の
# 独立した抽選は、同じ予算の別の使い方で、弱いソルバーにはより良い使い方になる。
# SETTINGS_FILE の "policy" で上書きできる。
POLICY = {"retry": "repair"}
RETRY_MODES = ("repair", "resample")

# 試すソルバーのバックエンドを、順に並べる。次の段がそのステップを初めて見る
# 前に、各段は試行を全部使う。ランナーは名前を solver-run に渡し、実際の
# コマンドへの対応は solver-run が持つ。だからこれは、ランナーや計画が何を
# 走らせるかを選ぶ手段ではない。
#
# 普通は1つ。2つにすると、安いかローカルのバックエンドが仕事をし、枠の限られた
# ほうはそれが終えられなかったモジュールにだけ使う。使い切った先にあるのは
# 中段への差し戻しで、それは人の手を1回使う。
#
# 既定値は設計ではなく箱の性質なので、ここに置く。既定は Claude Code だけ。
SOLVER_TIERS = ["claude"]

# コマンドを実行できるバックエンド。codex はサンドボックスの中でテストを
# 走らせられる。claude は Bash を許していない（solver-claude）。local は
# 1回の問い合わせでファイルを返すだけ（solver-local）。
SOLVERS_THAT_RUN_COMMANDS = {"codex"}


# --------------------------------------------------------------------------
# 小さな道具
# --------------------------------------------------------------------------


class Halt(Exception):
    """ステップを止める。台帳に入る理由を運ぶ。"""

    def __init__(self, phase: str, reason: str, detail: str = ""):
        super().__init__(reason)
        self.phase = phase
        self.reason = reason
        self.detail = detail


class SolverTimeout(Halt):
    """ソルバーが solver-run の上限時間内に終わらなかった。

    Halt の一種なので、受け止めない場所では今までどおりステップを止める。受け止める
    のは IMPL のループだけだ。実装の試行の時間切れは、試行1回分の失敗であって、
    設計の問題ではない。run 8 の S10 では、残り1回の試行を使わずに止まり、
    人間の手を待った。
    """


def run(cmd: list[str], cwd: Path = PROJECT, check: bool = False,
        env: dict[str, str] | None = None,
        timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
        env=({**os.environ, **env} if env else None),
        timeout=timeout,
    )


def attempts_for(step: dict) -> int:
    return LIMITS["attempts"] or step.get("max_attempts", 3)


def attempt_schedule(step: dict) -> list[str]:
    """試行1回につき1項目で、その試行を走らせるバックエンドの名前を並べる。"""
    return [t for t in SOLVER_TIERS for _ in range(attempts_for(step))]


def source_files(path: Path):
    """`path` の下の、コンパイル済みバイトコード以外のすべてのファイル。

    __pycache__ がここで効く理由は分かりにくい。.pyc はインタプリタを動かした
    アカウントの所有になるので、solver が書いたものが紛れ込むと、ランナーは
    chmod すらできない。ランナー自身の pytest でもバイトコードは切ってある
    （pytest_run を参照）。ここで飛ばすのは、ほかの何かが残したものの分だ。
    """
    for child in path.rglob("*"):
        if child.is_file() and "__pycache__" not in child.parts:
            yield child


def ledger(event: str, *, echo: str | None = None, **fields) -> None:
    """追記専用。VM が落ちた後にステップを再開する手段が台帳で、WSL2 では
    落ちるかどうかではなく、いつ落ちるかの問題だ（RUNNER_SPEC 1-6）。

    echo を渡すと、画面にはそれを出す。台帳には常に fields を全部書く。
    """
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    shown = echo if echo is not None else \
        " ".join(f"{k}={v}" for k, v in fields.items() if k != "detail")
    print(f"[{event}] {shown}")


# --------------------------------------------------------------------------
# いまの作業
# --------------------------------------------------------------------------
#
# 台帳は起きたことの記録で、ホストに届くのはコミットのときだけだ。
# 人が知りたい「いま誰がどのステップの何をしているか」は、そこからは読めない。
# だから、変わるたびに1つのファイルを丸ごと書き直す。箱の `loop now` がこれを
# 読み、ホストのダッシュボードが SSH で引く。
#
# /srv/loop/logs は runner と保守ユーザー（humanw）だけが読める。ステップの goal を
# 載せても、それを読むべきでない役には届かない。

NOW_FILE = LOOP / "logs" / "now.json"
# 誰が何をしているかの文脈。コマンドとステップは呼び出し元が設定する。
NOW: dict = {"command": None, "step": None, "attempt": None, "key": None, "since": None}

ROLE_JA = {"planner": "プランナー", "critic": "クリティック",
           "solver": "ソルバー", "runner": "ランナー"}

def describe_now(who: str, phase: str, detail: str = "") -> str:
    """いまの作業を日本語の1文にする。"""
    step = NOW["step"]
    if who == "planner":
        if NOW["command"] == "basic draft":
            return "仕様書から基本設計仕様書を書いている"
        return "基本設計仕様書からモジュールの詳細設計仕様書を書いている"
    if who == "critic":
        return "詳細設計仕様書を批評している"
    if who == "solver":
        text = {"TEST_WRITE": f"{step} の受け入れ条件からテストを書いている",
                "STUB": f"{step} のスタブを書いている",
                "IMPL": f"{step} を実装している"}.get(phase, f"{step} の {phase} をしている")
        if phase == "IMPL" and NOW["attempt"]:
            text += f"（試行 {NOW['attempt']}）"
        return text
    return {"red": f"{step} のテストが、実装の無いうちは落ちることを確かめている",
            "verify": f"{step} の実装をテストで確かめている",
            "check": "エージェントの出力を確かめている",
            "quota": "利用枠が戻るのを待っている"}.get(phase, phase)


def steps_now() -> list[dict]:
    """各モジュールの状態。下流の計画がまだ無ければ空。"""
    try:
        tasks = json.loads(TASKS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    done = green_steps()
    rows = []
    for step in tasks.get("steps", []) if isinstance(tasks, dict) else []:
        if not isinstance(step, dict):
            continue
        sid = step.get("id")
        state = "green" if sid in done else "active" if sid == NOW["step"] else "pending"
        rows.append({"id": sid, "goal": step.get("goal", ""), "state": state})
    return rows


def report_now(who: str | None, phase: str = "", detail: str = "") -> None:
    """いまの作業を NOW_FILE に書く。who が None なら、何もしていない。

    書けなくても走行は止めない。これは人が見るための写しで、関門ではない。
    """
    if who is None:
        activity = None
        NOW["key"] = NOW["since"] = None
    else:
        key = (who, phase, NOW["step"], NOW["attempt"], detail)
        if key != NOW["key"]:
            NOW["key"], NOW["since"] = key, time.strftime("%Y-%m-%dT%H:%M:%S%z")
        activity = {"who": who, "who_ja": ROLE_JA.get(who, who), "phase": phase,
                    "step": NOW["step"], "attempt": NOW["attempt"],
                    "text_ja": describe_now(who, phase, detail), "since": NOW["since"]}
    record = {"updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "pid": os.getpid(),
              "command": NOW["command"], "stage": read_stage().get("stage"),
              "activity": activity, "steps": steps_now()}
    try:
        temporary = NOW_FILE.with_name(NOW_FILE.name + ".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o640)
        os.replace(temporary, NOW_FILE)
    except OSError:
        pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# 書き込みの柵
# --------------------------------------------------------------------------


def set_writable(*, tests: bool | None, src: bool | None) -> None:
    """solver に、tests/ か src/ のどちらか一方だけへの書き込みを許す。

    files_write の許可リストを、指示ではなく仕組みにしたもの。粒度は粗く、
    ファイル単位ではなくディレクトリ単位なので、後で下の `assert_touched` が
    正確なパスを確かめる。粗い仕組みと正確な検査の組み合わせは、プロンプトの中に
    しか無い細かい仕組みに勝る。

    `None` は「このディレクトリはそのままにする」を意味する。FREEZE の後の
    tests/ には、これだけが正しい値だ。制限する方向でもモードを当て直すと、
    書き込みビットを所有者に返し、FREEZE が作ったものを黙って崩してしまう。
    """
    for path, writable in ((TESTS, tests), (SRC, src)):
        if writable is None:
            continue
        if writable:
            shutil.chown(path, group="solverw")
            path.chmod(0o2775)
            for child in owned_dirs(path):
                child.chmod(0o2775)
            for child in source_files(path):
                child.chmod(0o664)
        else:
            path.chmod(0o2755)
            for child in owned_dirs(path):
                child.chmod(0o2755)
            for child in source_files(path):
                child.chmod(0o444)


def owned_dirs(path: Path) -> list[Path]:
    """`path` の下のディレクトリのうち、ランナーが所有するもの。

    ソルバーが作ったディレクトリはソルバーの所有で、ランナーは chmod できない。
    それは飛ばし、書いたものは assert_touched が確かめる。
    """
    me = os.getuid()
    return [d for d in path.rglob("*")
            if d.is_dir() and not d.is_symlink() and d.stat().st_uid == me]


def discard_attempt(files_write: list[str]) -> list[str]:
    """失敗した試行を1回分取り消す。このステップの凍結済みのテストは残す。

    `reset` が使う `git reset --hard` は、あえて使わない。このステップのテストは
    書かれているがまだコミットされていない（コミットは GREEN で行う）ので、ここで
    reset すると、試行が満たそうとしているテストそのものを消してしまう。

    触るのはステップ自身の書き込みパスだけで、それは `assert_touched` が「変わった
    のはここだけ」とすでに確かめた集合と同じだ。HEAD にあるパスは HEAD に戻す。
    無いパスは消す。この試行が作り出すまで存在しなかったからだ。
    """
    tracked = set(run(["git", "ls-files", "--"] + files_write).stdout.splitlines())
    dropped = []
    for rel in files_write:
        if rel in tracked:
            run(["git", "checkout", "HEAD", "--", rel], check=True)
        elif (PROJECT / rel).exists():
            (PROJECT / rel).unlink()
        else:
            continue
        dropped.append(rel)
    return dropped


def adopt(*dirs: Path) -> list[str]:
    """solver がいま書いたものの所有権を引き取る。

    solver が作ったファイルは `solver` の所有になり、ランナーは自分が所有しない
    ファイルを chmod できない。この手順が無いと、書き込みの柵は開けられても
    二度と閉じられない。`chown` なら直るが root が要り、ランナーはあえてそれを
    持たない。

    ランナーを通してファイルを書き直せば、特権を一切使わずに同じ結果になる。
    親のディレクトリは runner の所有なので unlink が許され、書き直したファイルは
    runner のものになる。中身はバイト単位で同じなので、git には何も見えず、
    凍結のマニフェストにも影響しない。
    """
    me = os.getuid()
    adopted = []
    for directory in dirs:
        for f in source_files(directory):
            if f.stat().st_uid != me:
                data = f.read_bytes()
                f.unlink()
                f.write_bytes(data)
                f.chmod(0o664)
                adopted.append(str(f.relative_to(PROJECT)))
    return adopted


def freeze_tests() -> dict[str, str]:
    """FREEZE（RUNNER_SPEC 4-3）。仕組みは chmod で、ここで返すマニフェストは
    その上に重ねたトリップワイヤだ。仕事をしているのはマニフェストではない。"""
    shutil.chown(TESTS, group="runner")
    TESTS.chmod(0o2555)
    manifest = {}
    for f in sorted(TESTS.rglob("*")):
        if not f.is_file() or f.suffix not in LANGUAGE["test_suffixes"]:
            continue
        f.chmod(0o444)
        manifest[str(f.relative_to(PROJECT))] = sha256(f)
    return manifest


# --------------------------------------------------------------------------
# git で変更を見つける
# --------------------------------------------------------------------------


# plan/、.runner/、docs/ は 0700 runner。solver はそこに書けないので、その下の
# 変更は定義上ランナー自身のものだ。いま追記した台帳、いま作った junit のレポート。
# それを solver の書き込みに数えると、どのステップも自分の帳簿付けで落ちる。
RUNNER_OWNED = ("plan/", ".runner/", "docs/")

# テストのコードは .gitignore で外してあるので、git status には出ない。ソルバーを
# 呼ぶ前にテストの柵の中を写し取り、呼んだ後の違いを書き込みに数える（tests_changed）。
TESTS_BEFORE: dict[str, str] = {}


def tests_snapshot() -> dict[str, str]:
    """テストの柵の中のすべてのファイルと、そのハッシュ。"""
    if not TESTS.is_dir():
        return {}
    return {str(f.relative_to(PROJECT).as_posix()): sha256(f) for f in source_files(TESTS)}


def tests_changed(before: dict[str, str]) -> set[str]:
    """`before` を取った後に、作られたか変わったか消えたテストの柵の中のファイル。"""
    after = tests_snapshot()
    return {rel for rel in before.keys() | after.keys() if before.get(rel) != after.get(rel)}


def touched_paths() -> set[str]:
    out = run(["git", "status", "--porcelain", "--untracked-files=all"]).stdout
    paths = set()
    for line in out.splitlines():
        if not line.strip():
            continue
        # "XY path" か "XY old -> new" の形
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if path.startswith(RUNNER_OWNED):
            continue
        paths.add(path)
    return paths


def assert_written(phase: str, required: list[str], said: str = "") -> None:
    """その位相に頼んだファイルが存在し、中身があること。

    assert_touched は、許可リストの「外」に何も書かれていないことを確かめる。
    「内」に何かが書かれたかについては何も言わず、2つは別の問いだ。run 8 の S2
    では TEST_WRITE がファイルを1つも書かずに戻り、solver が書いてはいけない
    場所に書いていなかったので、位相は ok=True と記録された。その後 RED_GATE が
    空のレポートを読んでエラーと呼んだ。原因から2位相離れた場所だった。
    """
    missing = [p for p in required
               if not (PROJECT / p).is_file() or not (PROJECT / p).stat().st_size]
    if missing:
        # ソルバーの返答を添える。書けなかった理由（EACCES など）を言っているのは
        # それだけで、ほかには残らない。
        raise Halt(phase, "the files this phase had to produce are missing or empty",
                   "expected: " + ", ".join(missing)
                   + (f"\n\nsolver said:\n{said[-2000:]}" if said and said.strip() else ""))


def assert_touched(phase: str, allowed: list[str]) -> None:
    """solver が書いたのは、ステップが許した場所だけであること。

    これがしないことに注意する。solver に何を変えたかは訊かない。その問いには
    git が答え、solver は git に届かない（.git は 0700 runner）。
    """
    allowed_set = set(allowed)
    actual = touched_paths() | tests_changed(TESTS_BEFORE)
    stray = sorted(actual - allowed_set)
    if stray:
        raise Halt(
            phase,
            "solver wrote outside its allowlist",
            "wrote: " + ", ".join(stray) + "\nallowed: " + ", ".join(sorted(allowed_set)),
        )


# --------------------------------------------------------------------------
# テストを走らせる
# --------------------------------------------------------------------------


@dataclass
class TestRun:
    tests: int
    failures: int
    errors: int
    skipped: int
    failure_kinds: list[str]
    passed_names: list[str]
    output: str
    # アサーションそのもの。落ちたテスト1件につき1つで、テスト名と比較の結果。
    # 失敗のうち、ソルバーが手を打てる唯一の部分だ。
    failure_details: list[str] = field(default_factory=list)
    # 失敗がどのテストファイルにあるか。pytest の報告どおりの形
    # （"tests.test_models"）。VERIFY はスイート全体を走らせるので、壊れたものが
    # このステップのものか、すでに緑だったステップのものかを言う必要がある。
    failed_files: list[str] = field(default_factory=list)

    # 判定をまったく出さなかったテスト。"tests.test_models::test_rate" の形。
    skipped_names: list[str] = field(default_factory=list)

    @property
    def green(self) -> bool:
        # スキップされたテストは判定を出していないので、緑の一部にはなれない。
        # RED_GATE はそれを明言し（R3）、VERIFY は言っていなかった。この非対称は
        # PASS_TO_PASS の穴だった。通っていたテストがスキップし始めると
        # （後の実装が真にする skipif や importorskip）、関門はそれを走らせずに
        # またぎ、黙って縮んだスイートでステップを緑と数えた。いまは2つの関門が
        # 同じ理由で同じものを拒む。
        return (self.tests > 0 and self.failures == 0
                and self.errors == 0 and self.skipped == 0)


# R5: 本物のアサーションだけを赤と数える。ImportError や収集エラーはスタブが
# 壊れていることを表し、赤に見えてまったく別の意味を持つ。それを認めると、
# 動くテストを一度も持たないまま、ステップが RED_GATE を「通る」。
RED_KINDS = re.compile(r"^(AssertionError|Failed)\b")

# L14: 中身を述べていないコンテナ型。実際に型を注釈している場所（"->" か ":"
# の後）でだけ照合する。同じ文字列の中の文（"... a list of ids"）を署名と
# 取り違えないためだ。
ANNOTATION = re.compile(r"(?:->|:)\s*([A-Za-z_][\w.]*)\s*([\[<]?)")


# --------------------------------------------------------------------------
# 計画を書く言語
# --------------------------------------------------------------------------
#
# 項目は2つで、プラグインの仕組みは無い。言語が1つしか無いうちに切り出した
# アダプタは、間違った場所で切られていただろう。これは2つ目なので、違いは
# 推測ではなく事実だ。違いは4つだった。判定を出すコマンド、凍結する拡張子、
# ファイルのパスが import できる名前になる規則、形を持たない型名。
#
# 違わなかったものにも同じだけ価値がある。junit のレポート、全関門の算術、
# 書き込みの柵、台帳、止まったときの規則、型の語彙以外の設計の検査。
# 規則はもともと言語に依存していなかった。Python の形をしていたのは表現だけだ。
#
# 素の JavaScript ではなく TypeScript にした理由は L14 にある。契約は次の
# ステップに渡すものの形を述べなければならず、型の構文を持たない言語では
# できない。vitest は .ts を esbuild で変換し、別のビルド手順も tsc も無い。
# 型は「読まれる」ためにある。契約を書くプランナーと、それを読むソルバーが読む。
#
# 振る舞いの分岐は拡張子ではなく `name` で見る。データで済む差は項目にする。
# `environment_files` は、その言語のために箱が根に置くファイルだ。箱は計画の
# 言語を知らずに全部を置くので、ほかの言語の計画には見せない（environment_facts）。
#
# ここにあるのは言語そのものの差だけだ。コードを持ち込む先（Unity、DXライブラリ、
# ブラウザのページ）の差は FRAMEWORKS にあり、`default_framework` がその既定を選ぶ。
LANGUAGE_BASES = {
    "python": {
        "name": "python",
        "label": "Python",
        "default_framework": "none",
        "test_runner": PYTEST,
        "wiring": "conftest.py",
        # 20-layout.sh はどの言語の箱にも置く。ほかの言語の計画には見せない。
        "environment_files": frozenset({"conftest.py"}),
        "naming_note": "",
        "stub_shape": "a stub that returns a wrong value of the right type",
        "stub_note": "",
        "fixable_crashes": None,
        "provides_pattern": re.compile(r"(?:def|class)\s+([A-Za-z_]\w*)"),
        "red_kinds": RED_KINDS,
        "source_suffix": ".py",
        "test_suffixes": (".py",),
        # パッケージのディレクトリは __init__ で自分を名乗る。ほかのものは名乗らない。
        "index_name": "__init__",
        "module_separator": ".",
        "shapeless": frozenset({
            "tuple", "list", "dict", "set", "frozenset",
            "Tuple", "List", "Dict", "Set", "FrozenSet",
            "Sequence", "Mapping", "Iterable", "Iterator", "Collection",
        }),
        "shape_bracket": "[",
        "shape_example": "dict[str, Generator], tuple[GameState, int]",
        # モジュール名を名指ししたと数えるために、その名前に隣接してはならない
        # 文字。ドットを含めるのは Python の区切り文字だからだ。
        # `incgame.engine.sub` の中に現れる `incgame.engine` は、別のモジュールを指す。
        "name_boundary": r"[\w.]",
        # 1つのモジュールを作るファイルの拡張子。C++ だけが2つで、ヘッダとソースの対だ。
        "module_suffixes": (".py",),
        "layout_note": """Put the package inside {SRC}/, e.g. `{SRC}/yourpkg/models.py`, and import it as
`from yourpkg.models import Thing` -- `{SRC}` is on sys.path, so it does not
appear in imports.
""",
        "contract_note": """A provides line is the declaration as Python writes it, without its body:
    {"provides": "def parse(text: str) -> Config"}
    {"provides": "class Board: width: int; height: int; def score(self) -> int"}
The names are `parse` and `Board`; requires and provides match on those names.""",
    },
    "typescript": {
        "name": "typescript",
        "label": "TypeScript",
        "default_framework": "dom",
        "test_runner": VITEST,
        "wiring": "vitest.config.mjs",
        "environment_files": frozenset({"vitest.config.mjs"}),
        "naming_note": """
Name your tests with DOUBLE quotes or backticks, never single quotes. The
criteria above are prose and contain apostrophes ("the result's resource"),
and one of those inside a single-quoted name ends the string: the file stops
compiling and not one of your tests runs.
""",
        "stub_shape": "a stub that returns a wrong value of the right type",
        "stub_note": "",
        "fixable_crashes": None,
        "provides_pattern": re.compile(
            r"(?:function|class|interface|type|enum|const|let)\s+([A-Za-z_]\w*)"),
        "red_kinds": RED_KINDS,
        "source_suffix": ".ts",
        "test_suffixes": (".ts",),
        "index_name": "index",
        "module_separator": "/",
        # `object` と `any` があるのは、`dict` があるのと同じ理由だ。契約が
        # 何も言わずに名指しできる形で、そこから書いたスタブは、条件が何を
        # 求めても満たしてしまう。
        "shapeless": frozenset({
            "Array", "ReadonlyArray", "Record", "Map", "Set", "WeakMap",
            "Promise", "Iterable", "Iterator",
            "object", "Object", "any", "unknown",
        }),
        "shape_bracket": "<",
        "shape_example": "Record<string, Generator>, [GameState, number]",
        # ドットは含めない。TypeScript の区切り文字はスラッシュで、ドットは
        # 拡張子だ。含めると `src/idlegame/model.ts` が拒まれる。この行ほど
        # はっきりモジュールを名指しするものは無く、L15 の目的を満たしている
        # のに、検査が Python の文法でそれを拒む。最初の TypeScript の
        # bootstrap の1回目で見つかった。B3 の欠陥と同じ形で、誤っていたのは
        # 計画ではなく規則で、そのために試行を1回使った。
        "name_boundary": r"[\w]",
        "module_suffixes": (".ts",),
        "layout_note": """Every source file is `.ts` under {SRC}/, e.g. `{SRC}/idlegame/models.ts`, and
every test file is `.ts` under {TESTS}/. Import with a RELATIVE path and no
extension in the specifier is wrong here -- write the extension:
`import { Thing } from "{SRC_FROM_TESTS}/idlegame/models.ts"` from a test file
directly in {TESTS}/. vitest resolves it and esbuild strips the types; there is
no build step and no tsc, so a type is something the next module READS, not
something a compiler checks.

""",
        "contract_note": """A provides line is the export as TypeScript writes it, without its body:
    {"provides": "export function canPlace(board: Board, x: number, y: number): boolean"}
    {"provides": "export interface Board { cells: Stone[]; size: number }"}
    {"provides": "export type Stone = 'black' | 'white'"}
    {"provides": "export const START: Board"}
The names are `canPlace`, `Board`, `Stone` and `START`; requires and provides
match on those names.""",
    },
    # テストは net8.0 の NUnit 3 で、36-dotnet.sh が凍結したフィードだけから restore
    # する。csproj はランナーが /srv/loop/dotnet/build に書く（dotnet_projects）。
    # 取り込んだ Unity のプロジェクトの根に置くと、IDE が Unity の csproj と一緒に
    # 拾ってしまう。コードの側の対象とC#の版は FRAMEWORKS が決める。
    #
    # C# の宣言は型の中にあるので、契約の名前は型（`Board`）とメンバー
    # （`Board.Score`）の2つの単位を持つ。provides_pattern の2つの組がそれだ。
    "csharp": {
        "name": "csharp",
        "label": "C#",
        "default_framework": "unity",
        "test_runner": Path("/usr/bin/dotnet"),
        "wiring": None,
        "environment_files": frozenset(),
        "naming_note": """
A test is a C# method, and its name is an identifier: letters, digits and
underscores only. Name it after the criterion in PascalCase with underscores
(`Score_counts_every_row`); do not copy the criterion's prose into it.
""",
        # スタブのフィールドは宣言だけで、初期化子を持たない（CSHARP_STUB_BODY）。
        # 取り込んだ Unity のプロジェクトの回で、配列のフィールドを足す S3 が、準備の
        # 段の `d.arr[0] = 999` で NullReferenceException になり、R5 で2回止まった。
        # 書き手はフィールドが null だと知らされていなかった。
        "stub_shape": "a stub whose methods throw",
        "stub_note": """
THE STUB, IN C#. Every method and constructor this step provides throws
`NotImplementedException("__stub__")`, and a test that dies on that exception
counts as red. Every field and auto-property this step provides is declared
WITHOUT its initialiser, so against the stub it holds its default: null for an
array, a List, a string or any class; 0 or false for a value type.

A test that indexes, iterates, reads `.Length` or `.Count` of, or calls a method
on such a member while it is null dies with NullReferenceException. That is not
a red test. It is a broken one, and the step is rejected. So before the first
use of a reference-type member this step provides -- in the arrange part of a
test as much as in its assertions -- write `Assert.IsNotNull(member, "<name>")`,
and before an index, assert that the length is large enough.
""",
        # 上の誤りでテストが落ちたときは、書き手が直せる。スタブはランナーが契約から
        # 書いたもので、落ちたのはテストの側だ。
        "fixable_crashes": re.compile(
            r"^System\.(?:NullReferenceException|IndexOutOfRangeException)$"),
        "provides_pattern": re.compile(
            r"\b(?:class|struct|enum|interface)\s+([A-Za-z_]\w*)"
            r"|([A-Za-z_]\w*\.[A-Za-z_]\w*)\s*(?:\(|\{|;|=|$|--|<)"),
        # 型の後ろ、名前の前に型を書くので、":" や "->" の後ろを見る ANNOTATION は
        # 使えない。語の境目で、裸のコンテナ型を探す。
        "shape_pattern": re.compile(
            r"(?<![\w.])(List|IList|IReadOnlyList|Dictionary|IDictionary|"
            r"IReadOnlyDictionary|HashSet|ISet|IEnumerable|ICollection|"
            r"IReadOnlyCollection|Queue|Stack|Tuple|ArrayList|Hashtable|object|dynamic)"
            r"\b\s*(<?)"),
        # junit の type は常に "failure" で、例外の型を持たない（smoke-dotnet の
        # 実測）。failure_kind が message から読み、アサーションをこの名前にする。
        # スタブは値を返さず、印の付いた例外を投げる（CSHARP_STUB_BODY）。
        "red_kinds": re.compile(r"^(?:AssertionException|StubNotImplemented)$"),
        "source_suffix": ".cs",
        "test_suffixes": (".cs",),
        # テストの前にビルドする。Unity の参照を写すので、初回は1分を超えうる。
        "min_test_timeout": 600,
        # C# には「ディレクトリそのもの」を表すファイルが無い。
        "index_name": "",
        "module_separator": "/",
        "shapeless": frozenset({
            "List", "IList", "IReadOnlyList", "Dictionary", "IDictionary",
            "IReadOnlyDictionary", "HashSet", "ISet", "IEnumerable", "ICollection",
            "IReadOnlyCollection", "Queue", "Stack", "Tuple", "ArrayList", "Hashtable",
            "object", "dynamic",
        }),
        "shape_bracket": "<",
        "shape_example": "List<Cell>, Dictionary<string, int>, (Board board, int score)",
        "name_boundary": r"[\w]",
        "module_suffixes": (".cs",),
        "contract_note": """A provides line is a type, or a member written with its type in front of its
name:
    {"provides": "class Board { public int Width; public int Height; }"}
    {"provides": "static int Board.Score(Board board)"}
The name of the first is `Board` and of the second `Board.Score`; requires and
provides match on those names.""",
    },
    # C++ は、箱では標準の C++17 のロジックだけを g++ と GoogleTest で確かめる。
    # DXライブラリや Windows の API は Linux に無い。それを include するファイルは
    # ビルドから外し（cpp_sources）、人が Visual Studio でビルドする。CMakeLists.txt は
    # ランナーが /srv/loop/cpp/build に書く。プロジェクトの根には置かない。
    #
    # 契約の名前は、型（`Board`）、メンバー関数（`Board::score`）、自由関数
    # （`countLines`）の3つの単位を持つ。provides_pattern の3つの組がそれだ。
    # 契約が名指しするファイルはヘッダで、source_suffix が `.h` なのはそのため。
    "cpp": {
        "name": "cpp",
        "label": "C++",
        "default_framework": "dxlib",
        "test_runner": Path("/usr/bin/g++"),
        "wiring": None,
        "environment_files": frozenset(),
        "naming_note": """
A test is `TEST(Suite, Name)`, and both are identifiers. The suite is the
file's name (`BoardTest.cpp` uses `TEST(BoardTest, ...)` and nothing else). Name
each test after its criterion in PascalCase with no underscores
(`ScoreCountsEveryRow`): GoogleTest joins the two with an underscore, so an
underscore in either can make two tests collide. Do not copy the criterion's
prose into it.
""",
        "stub_shape": "a stub whose functions throw",
        "stub_note": f"""
THE STUB, IN C++. Every function and constructor this step provides throws
`std::logic_error("__stub__")`, and a test that dies on that exception counts
as red. Every data member this step provides is value-initialised by the stub
(`int width{{}};`, `std::vector<Cell> cells{{}};`), so against the stub it holds
0, false, an empty string or an empty container.

A test that reads past the end of such a container is not red, it is broken.
`v[0]` on an empty vector is undefined behaviour: the whole test program dies,
no test reports anything, and the step is rejected. `v.at(0)` throws
std::out_of_range, which is not red either. So before the first index into a
container this step provides -- in the arrange part of a test as much as in its
assertions -- write `ASSERT_GE(v.size(), n)`, and index with `.at()`.
""",
        # 上の誤りで落ちたテストは、書き手が直せる。スタブはソルバーが書いたものだが、
        # 落ちたのはテストの側だ。
        "fixable_crashes": re.compile(r"^std::out_of_range$"),
        "provides_pattern": re.compile(
            r"\b(?:class|struct|enum(?:\s+class)?)\s+([A-Za-z_]\w*)"
            r"|\b([A-Za-z_]\w*::~?[A-Za-z_]\w*)\s*\("
            r"|\b([A-Za-z_]\w*)\s*\("),
        # 型の後ろ、名前の前に型を書くので、C# と同じく語の境目で探す。`std::` は
        # あってもなくてもよい。
        "shape_pattern": re.compile(
            r"(?<![\w])(?:std::)?(vector|map|unordered_map|set|unordered_set|multimap|"
            r"multiset|list|deque|array|pair|tuple|any|function)\b\s*(<?)"),
        # GoogleTest の junit は例外の型を持たない。cpp_failure_kind が message から
        # 読み、アサーションとスタブの例外をこの名前にする。
        "red_kinds": re.compile(r"^(?:AssertionFailure|StubNotImplemented)$"),
        "source_suffix": ".h",
        "test_suffixes": (".cpp", ".h", ".hpp"),
        # テストの前にビルドする。初回は全部をコンパイルする。
        "min_test_timeout": 600,
        "index_name": "",
        "module_separator": "/",
        "shapeless": frozenset({
            "vector", "map", "unordered_map", "set", "unordered_set", "multimap",
            "multiset", "list", "deque", "array", "pair", "tuple", "any", "function",
        }),
        "shape_bracket": "<",
        "shape_example": "std::vector<Cell>, std::map<std::string, int>, std::pair<Board, int>",
        "name_boundary": r"[\w]",
        # ヘッダとソースの対で1つのモジュールにする。設計仕様書は拡張子を落とした名前で1つ。
        "module_suffixes": (".h", ".cpp"),
        "contract_note": """A provides line is a type, a member function written with its class, or a free
function:
    {"provides": "struct Board { int width; int height; }"}
    {"provides": "int Board::score() const"}
    {"provides": "int countLines(const Board& board)"}
The names are `Board`, `Board::score` and `countLines`; requires and provides
match on those names.""",
    },
}

# --------------------------------------------------------------------------
# コードを持ち込む先
# --------------------------------------------------------------------------
#
# 同じ言語でも、出来たコードを持ち込む先で変わるものがある。コードの側の対象と
# 言語の版、ファイルの置き方の決まり、箱に無い画面やエンジンについての事実、
# 箱が根に置くページ。それをここに置き、言語の項目に重ねる（compose_language）。
#
# 名前は閉じた一覧にする。箱は凍結したツールチェーンだけで走り、egress は閉じて
# いる。ライブラリの名前を自由に書けても、フィードに無いものは restore できない。
#
#   layout_note        言語の layout_note の後ろに続ける
#   environment_files  言語のものに足す
#   facts              (実行環境の1行, UI の1行, 画面についての段落) を返す
#   page               根のページを見せる文を返す。ページの無い先は持たない
#   refs               ライブラリの参照の名前。REFS の下の置き場（library_refs）

# C# のコードは OutputType の無いライブラリとして組む（dotnet_projects）。入口は
# どの持ち込む先でもテストで確かめられない。
CSHARP_LIBRARY_NOTE = """The code is built as a class library named LoopCode, with no OutputType.
There is no `Main` here and `Assembly.EntryPoint` is always null. A file of
top-level statements (the usual `Program.cs`) is left out of the build, so
nothing in it compiles or runs here. So no criterion can check how the program
starts -- what `Main` or `Program.cs` does, or what it calls first. The entry
file may be a module of its own; the person checks it by hand after the run.
Keep it thin, and put the behaviour it starts in classes the tests can call.

"""

FRAMEWORKS = {
    "python": {
        # 持ち込む先を持たない。画面の事実は tkinter が読み込めるかで決まる。
        "none": {
            "facts": lambda: python_facts(),
        },
    },
    "typescript": {
        # ブラウザのページ。箱が根に index.html を置き、テストは happy-dom の DOM で走る。
        "dom": {
            "environment_files": frozenset({"index.html"}),
            "layout_note": """THE PAGE ALREADY EXISTS AND YOU DO NOT WRITE IT. `index.html` sits at the
repository root, which is outside the write fence, so it belongs to the
environment rather than to any step. It is four lines and it does exactly one
thing:

    import { start } from "/{SRC}/main.ts";
    start(document.getElementById("app"));

So the design MUST have a module `{SRC}/main.ts`, and it MUST export
`start(root: HTMLElement): void` with `"entry": true`. Nothing else about the
page is yours to decide. Everything `start` does is ordinary code under
{SRC}/: it is under the fence, the tests can reach it, and its criteria are
written against what it puts in the document -- what the element contains,
which buttons exist, which of them are disabled, and what changes when one is
clicked.
""",
            "facts": lambda: dom_facts(),
            "page": lambda: dom_page_text(),
        },
    },
    "csharp": {
        # Unity に持ち込むコード。コードは netstandard2.1 で C# 9 まで、テストは
        # Unity の Test Framework と同じ系統の NUnit 3。loop-unity-refs が参照を
        # 送っていれば、それでコンパイルする（unity_refs）。送っていなくても、
        # Unity に置ける形で書く。
        "unity": {
            "dotnet_sdk": "8.0",
            "dotnet_target": "netstandard2.1",
            "dotnet_test_target": "net8.0",
            "dotnet_langversion": "9.0",
            "dotnet_implicit_usings": "disable",
            "dotnet_nullable": "disable",
            "dotnet_packages": (),
            "unity_refs": True,
            "layout_note": """Every source file is `.cs` under {SRC}/, e.g. `{SRC}/Logic/Board.cs`, and
every test file is `.cs` under {TESTS}/. The code compiles as one assembly
(netstandard2.1, C# 9) and the tests as another that references it, so a test
uses the code through `using <its namespace>;` -- there are no file imports.

One public type per file, and the file is named after the type
(`Board.cs` holds `Board`). Unity requires this of every MonoBehaviour and it
costs nothing elsewhere. A new file's namespace follows its folder under {SRC}/
(`{SRC}/Logic/Board.cs` -> `namespace Logic`). A file that already exists keeps
the namespace it has: a declaration read from it ends in `-- namespace <name>`,
and code that uses it writes `using <name>;`.

""" + CSHARP_LIBRARY_NOTE + """Tests use NUnit 3: `using NUnit.Framework;`, a public class with `[Test]`
methods, and `Assert.AreEqual(expected, actual)` or `Assert.That(actual,
Is.EqualTo(expected))`. Every test class is inside a namespace, and its name
is the file's name (`{TESTS}/BoardTests.cs` holds `BoardTests`): the runner
selects a step's tests by that class name.

C# 9 is the ceiling: no `record struct`, no file-scoped `namespace X;`, no
`global using`, no `required` members. They do not compile here or in Unity.
""",
            "facts": lambda: ("Runtime: .NET 8 for the tests; the code is built as netstandard2.1",
                              *unity_facts()),
        },
        # Promete（2D のゲームエンジン）に持ち込むコード。2.1.0 は net10.0 だけを持つので、
        # コードもテストも net10.0 で、SDK は 10。C# の版は Promete 自身のテストに合わせる。
        # Promete のプロジェクトは暗黙の using と nullable を有効にして書かれているので、
        # 取り込んだコードを同じ設定でコンパイルする。
        "promete": {
            "dotnet_sdk": "10.0",
            "dotnet_target": "net10.0",
            "dotnet_test_target": "net10.0",
            "dotnet_langversion": "14.0",
            "dotnet_implicit_usings": "enable",
            "dotnet_nullable": "enable",
            "dotnet_packages": ("Promete",),
            "refs": "promete",
            "layout_note": """Every source file is `.cs` under {SRC}/, e.g. `{SRC}/Logic/Board.cs`, and
every test file is `.cs` under {TESTS}/. The code compiles as one assembly
(net10.0, C# 14, implicit usings and nullable reference types on) that
references the Promete 2.1.0 package, and the tests as another that references
the code, so a test uses the code through `using <its namespace>;` -- there are
no file imports.

One public type per file, and the file is named after the type
(`Board.cs` holds `Board`). A new file's namespace follows its folder under
{SRC}/ (`{SRC}/Logic/Board.cs` -> namespace `Logic`). A file that already exists
keeps the namespace it has: a declaration read from it ends in
`-- namespace <name>`, and code that uses it writes `using <name>;`.

""" + CSHARP_LIBRARY_NOTE + """Tests use NUnit 3: `using NUnit.Framework;`, a public class with `[Test]`
methods, and `Assert.AreEqual(expected, actual)` or `Assert.That(actual,
Is.EqualTo(expected))`. Every test class is inside a namespace, and its name
is the file's name (`{TESTS}/BoardTests.cs` holds `BoardTests`): the runner
selects a step's tests by that class name.

Promete here is version 2, and older examples show version 1. A scene takes the
plugins it uses through its constructor (`public class MainScene(Keyboard
keyboard) : Scene`) and reads the screen through `View` and the clock through
`Time`. A scene reaches the app through `App`. The window is
`Promete.Backends.IGameView`, taken through the constructor like a plugin: its
size is `view.Size = (640, 480)` and its title `view.Title`, set in the first
scene's `OnStart`, or the window keeps Promete's default size. `App.Window`
and `IWindow` are obsolete in version 2 and removed in 3; do not use them.
`Shape.CreateRect(left, top, right, bottom, color)` takes two corners, not a
position and a size: a 200x56 rectangle at (540, 300) is
`Shape.CreateRect(540, 300, 740, 356, color)`.
Input is a plugin taken through the constructor. `mouse[MouseButtonType.Left]`
is the left button: `IsButtonDown` is true on the frame it goes down,
`IsPressed` while it is held, `IsButtonUp` on the frame it is released, and
`mouse.Position` is the pointer. A key has `IsKeyDown`, `IsPressed` and
`IsKeyUp` in the same sense. No test can see the window or press a button; the
person checks both after the run.

The entry `Program.cs` starts as Promete's template: it runs a `MainScene`
declared in the same file. A scene declared elsewhere never starts until the
entry changes, and a plugin a scene takes must be registered there with
`.Use<T>()` (`.Use<Mouse>()`), or the scene cannot be built. So when the run
adds the first scene or a plugin a scene takes, the entry is a module of its
own: it deletes the template's scene, adds `using <the scene's namespace>;`,
runs the run's first scene, and registers every plugin the scenes take.
""",
            "facts": lambda: ("Runtime: .NET 10 for the code and the tests; Promete 2.1.0",
                              *promete_facts()),
        },
    },
    "cpp": {
        # DXライブラリで Windows に持ち込むコード。DXライブラリを include するファイルは
        # ビルドから外す（cpp_sources）。DXライブラリを使わない C++ の計画もこの形で
        # 走り、早見表は DXライブラリを使うと分かったときだけ見せる（cpp_facts）。
        "dxlib": {
            "refs": "dxlib",
            "layout_note": """Every header is `.h` and every source file is `.cpp`, under {SRC}/: the header
declares (`{SRC}/logic/Board.h`) and the source file of the same name defines
(`{SRC}/logic/Board.cpp`). Every header starts with `#pragma once`. Include a
project header by its path under {SRC}/ -- `#include "logic/Board.h"` -- from
code and tests alike; {SRC}/ is on the include path.

The code is standard C++17 and nothing else. The box builds it with g++, and a
person later builds it with Visual Studio (MSVC), so: no compiler extensions,
no `<bits/stdc++.h>`, no variable-length arrays, no `#include <windows.h>` or
DxLib.h in anything a test reaches. Write new code in the global namespace.

Write every file in ASCII only, comments and string literals included. MSVC
reads a source file without a byte order mark in the system code page, and a
UTF-8 Japanese comment there breaks the build on the person's machine.

Tests use GoogleTest: `#include <gtest/gtest.h>`, `TEST(Suite, Name)`,
`EXPECT_EQ(expected, actual)`, `EXPECT_TRUE`, `EXPECT_THROW(call, type)`. A test
file has no `main`; the runner links one. Each test file uses exactly one
suite, named after the file (`{TESTS}/BoardTest.cpp` holds `TEST(BoardTest,
...)` only): the runner selects a module's tests by that name.
""",
            "facts": lambda: ("Standard: C++17, built with CMake; tests: GoogleTest",
                              *cpp_facts()),
        },
    },
}


def compose_language(language: str, framework: str | None = None) -> dict:
    """言語の項目に、持ち込む先の項目を重ねる。framework が None なら言語の既定。"""
    base = LANGUAGE_BASES[language]
    name = framework or base["default_framework"]
    overlay = FRAMEWORKS[language][name]
    return {**base, **overlay, "framework": name,
            "layout_note": base.get("layout_note", "") + overlay.get("layout_note", ""),
            "environment_files": base["environment_files"]
            | overlay.get("environment_files", frozenset()),
            "page": overlay.get("page")}


# 言語ごとの既定の組み合わせ。計画が "framework" を言わなければ、これで走る。
LANGUAGES = {name: compose_language(name) for name in LANGUAGE_BASES}

# 段（plan/stage.json）の "language" と "framework" で選ぶ。basic draft --language が
# 決める。言語を持たない段では Python で読む。
LANGUAGE = dict(LANGUAGES["python"])


def use_language(language: str, framework: str | None = None) -> None:
    """LANGUAGE を、その言語と持ち込む先の組み合わせにする。"""
    LANGUAGE.clear()
    LANGUAGE.update(compose_language(language, framework))

# pytest は失敗の本文を必ず "<file>:<line>: <ExceptionName>" で終える。例外の
# クラスが実際に読めるのはその最後の行だ。failure_kind を参照。
# vitest は変換エラーに色を付け、そのエスケープコードのせいで、引用した先
# （ブリーフ、FAILURE.md、台帳）のどこでも文が読めなくなる。
#
# ESC の無い形も取り除く。JUnit の XML に ESC は書けないので、vitest は
# ESC だけを落として `[38;5;249m` の部分を残す。構文エラーの位置を示す図は
# NO_COLOR を見ずに色を付けるので、レポートの message はこの切れ端だらけになる。
# ESC の無い形は数字を必須にする。`arr[m]` の `[m` を消さないためだ。
ANSI = re.compile(chr(27) + r"\[[0-9;]*m|\[\d{1,3}(?:;\d{1,3})*m")
FAILURE_TAIL = re.compile(r":\s*([A-Za-z_][\w.]*)\s*$")


def failure_kind(failure: ET.Element) -> str:
    """1つのテストを終わらせた例外のクラス。

    vitest は `type` を設定するので、それで済む。pytest は設定しないので、Python の
    レポートでは本文から読み取る必要がある。すぐ思いつく `message` 属性は誤った
    場所で、その誤り方は実際のステップで初めて露見した:

        assert float("nan") == 5.0     -> "AssertionError: assert nan == 5.0"
        assert state.resources == 5.0  -> "assert nan == 5.0"

    どちらも普通のアサーションだ。2つ目は説明が2行にわたる
    （"+ where nan = <GameState>.resources"）ので、pytest が接頭辞を落とし、クラス名が
    消える。message を "AssertionError" で照合すると、値についてのアサーションは
    認め、属性についてのアサーションは拒むことになる。そしてデータモデルのステップ
    （たいていの計画はそこから始まる）は属性について確かめる。RED_GATE は、まったく
    正しい赤を拒むところだった。

    本文の最後の行は、説明がどうであれ、同じ形でクラスを運ぶ:

        tests/test_models.py:16: AssertionError
        tests/test_models.py:21: Failed              (pytest.raises が何も捕まえなかった)
        tests/test_models.py:25: AttributeError      (呼び出しそのものが壊れている)

    これがちょうど、R5 が見分けるための区別だ。
    """
    if LANGUAGE["name"] == "csharp":
        return csharp_failure_kind(failure)
    if LANGUAGE["name"] == "cpp":
        return cpp_failure_kind(failure)
    declared = (failure.get("type") or "").strip()
    if declared:
        return declared
    body = (failure.text or "").strip()
    if body:
        match = FAILURE_TAIL.search(body.splitlines()[-1])
        if match:
            return match.group(1)
    # 本文を省く junit の書き手のためだけのもの。あえて単純にしてある。クラスが
    # 読めなければ、R5 は気前よく推測するのではなく拒むべきだ。
    message = (failure.get("message") or "").strip()
    return message.splitlines()[0] if message else "<no type>"


# NUnit を junit に書くロガーは、例外の型を type に書かない（常に "failure"）。
# 型は message の先頭にある: "System.NullReferenceException : Object reference ..."
CSHARP_EXCEPTION = re.compile(r"^\s*([A-Za-z_][\w.]*(?:Exception|Error))\s*:")


def csharp_failure_kind(failure: ET.Element) -> str:
    """NUnit の失敗の種類。smoke-dotnet と probe-unity の実測に合わせてある。

        "  Expected: 6\\n  But was:  5\\n"                  -> AssertionException
        "System.Security.SecurityException : ECall ..."   -> System.Security.SecurityException

    例外の接頭辞を先に見る。例外の本文が "Expected:" を含むことはあっても、
    アサーションの message が例外の型で始まることは無い。どちらでもなければ
    （Assert.Fail の文など）1行目を返し、R5 は推測せずに拒む。
    """
    message = (failure.get("message") or "").strip()
    first = message.splitlines()[0] if message else ""
    # スタブが投げる印の付いた例外（CSHARP_STUB_BODY）。スタブに対して落ちた
    # ことを示すので、アサーションと同じく赤と認める。印の無い
    # NotImplementedException は、ほかの例外と同じく拒む。
    if CSHARP_STUB_MARK.match(first):
        return "StubNotImplemented"
    match = CSHARP_EXCEPTION.match(first)
    if match:
        return match.group(1)
    if "Expected:" in message and "But was:" in message:
        return "AssertionException"
    return first or "<no type>"


# 持ち込む先のライブラリの参照。Promete は 36-dotnet.sh が、DXライブラリは
# `loop refs dxlib` が root で置き、誰でも読めて誰も書けない。MEMBERS.txt を1行1件で
# 持ち、型のあるライブラリは TYPES.txt も持つ（provision/loop_refs.py）。
REFS = LOOP / "refs"


def library_refs() -> Path | None:
    """いまの持ち込む先の参照の置き場。無ければ None。"""
    name = LANGUAGE.get("refs")
    path = REFS / name if name else None
    return path if path is not None and (path / "MEMBERS.txt").is_file() else None


def library_types() -> str:
    """参照にある型の名前を、名前空間ごとに1行にまとめる。プランナーに渡す。"""
    path = library_refs()
    if path is None or not (path / "TYPES.txt").is_file():
        return ""
    spaces: dict[str, list[str]] = {}
    for line in (path / "TYPES.txt").read_text(encoding="utf-8").splitlines():
        name = line.split(None, 1)[0] if line.strip() else ""
        if not name:
            continue
        space, _, short = name.rpartition(".")
        spaces.setdefault(space, []).append(short)
    return "\n".join(f"    {space}: {', '.join(names)}" for space, names in sorted(spaces.items()))


def library_namespaces() -> dict[str, str]:
    """参照にある型の短い名前から名前空間への対応。スタブの using に使う。

    総称型の印（`Container`1`）は外す。入れ子の型（外側が型の名前）と、2つの
    名前空間にある名前は入れない。どちらも using で正しく指せない。
    """
    path = library_refs()
    if path is None or not (path / "TYPES.txt").is_file():
        return {}
    names = [line.split(None, 1)[0].split("`")[0]
             for line in (path / "TYPES.txt").read_text(encoding="utf-8").splitlines()
             if line.strip()]
    full = set(names)
    found: dict[str, str] = {}
    twice: set[str] = set()
    for name in names:
        space, _, short = name.rpartition(".")
        if not space or space in full:
            continue
        if short in found and found[short] != space:
            twice.add(short)
        found[short] = space
    return {short: space for short, space in found.items() if short not in twice}


def library_refs_section() -> str:
    """ソルバーのブリーフの、ライブラリの参照の節。参照が無ければ空。"""
    path = library_refs()
    if path is None:
        return ""
    files = ", ".join(sorted(p.name for p in path.iterdir() if p.is_file()
                             and not p.name.startswith(".")))
    return f"""
# The library reference
The documented functions, members and constants of the library this code is
built against are in {path} ({files}). `MEMBERS.txt` lists them one per line
with a short summary, and `TYPES.txt`, where there is one, lists the types.
Before you use a library function or member, Grep `MEMBERS.txt` for its name
or its type and use what is listed. One that is not listed may still exist;
then look in the other file there, or in how the project uses it.
"""


DOTNET_TOOLS = LOOP / "dotnet"
DOTNET_BUILD = DOTNET_TOOLS / "build"
UNITY_REFS = LOOP / "unity-refs"
# dotnet の出力を英語に固定する。ビルドエラーは行の形で読むからだ。HOME に頼らず、
# NuGet のキャッシュと CLI の状態は runner だけが入れる build の下に置く。
# ノードとコンパイラのサーバを残さない。走行の後に runner のプロセスが残ると、
# loop-project.sh の busy() が切り替えを拒む。
DOTNET_ENV = {
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1",
    "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1", "DOTNET_CLI_UI_LANGUAGE": "en",
    "DOTNET_CLI_HOME": str(DOTNET_BUILD / "home"),
    "NUGET_PACKAGES": str(DOTNET_BUILD / "packages"),
    "MSBUILDDISABLENODEREUSE": "1",
}


def unity_refs() -> bool:
    """Unity の参照アセンブリでコンパイルするか。Unity に持ち込む計画で、
    loop-unity-refs が参照を送ったときだけそうする。"""
    return bool(LANGUAGE.get("unity_refs")) and (UNITY_REFS / "refs").is_dir()


def dotnet_global_json() -> str:
    """build に置く global.json。持ち込む先の SDK に固定する。

    箱には SDK が2つある（36-dotnet.sh）。dotnet は作業ディレクトリから global.json を
    探し、無ければいちばん新しい SDK を使う。Unity の計画を SDK 8 のまま走らせる
    ために、dotnet は build で呼び、ここに置いたものを読ませる。
    """
    return json.dumps({"sdk": {"version": f"{LANGUAGE['dotnet_sdk']}.100",
                               "rollForward": "latestFeature", "allowPrerelease": False}},
                      indent=2) + "\n"


def csharp_entry_files() -> list[str]:
    """柵のコードの場所にある、トップレベル文を持つ .cs。根からの相対パス。

    コードは OutputType の無いライブラリとして組むので、トップレベル文はコンパイル
    できない（CS8805）。Promete のプロジェクトの Program.cs はこの形で、取り込むと
    ビルド全体が落ちる。C++ の cpp_sources と同じく、ビルドから外して人の手で直す
    入口として扱う。読めないファイルは外さない。壊れていることはビルドが伝える。
    """
    found: list[str] = []
    if not SRC.is_dir():
        return found
    for path in sorted(SRC.rglob("*.cs")):
        try:
            outline = csharp_outline(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if outline is None:
            continue
        if any(csharp_is_statement(outline, start, stop) for start, stop in outline["others"]):
            found.append(path.relative_to(PROJECT).as_posix())
    return found


def csharp_is_statement(outline: dict, start: int, stop: int) -> bool:
    """最上位の単位が文か。型でも using でもない最上位の宣言（csharp_outline の
    others）には、文のほかに、本体の無い型（`record Card(int Rank);`、
    `interface IMarker;`）、delegate、extern alias、`[assembly: ...]` だけの単位がある。"""
    sig = csharp_after_attributes(outline["shape"], start, stop)
    head = " ".join(outline["code"][sig:stop].split()).rstrip(";").strip()
    return bool(head) and not CSHARP_TYPE_HEAD.match(head) \
        and not re.match(r"(?:extern\s+alias|(?:[\w\s]*\s)?delegate)\b", head)


def dotnet_projects() -> dict[Path, str]:
    """ランナーが書く2つの csproj と global.json。柵の場所、凍結したフィードの版、
    持ち込む先から作る。

    プロジェクトの根には置かない（36-dotnet.sh の冒頭を参照）。コードとテストの対象、
    C# の版、SDK、使うパッケージは持ち込む先が決め（FRAMEWORKS）、テストは NUnit 3。
    Unity の参照があれば、コードはそれでコンパイルし、テストは実行のためにそれを
    出力に写す。
    """
    versions = dict(item.split("=", 1) for item in
                    (DOTNET_TOOLS / "feed" / ".versions").read_text(encoding="utf-8").split())
    refs = sorted((UNITY_REFS / "refs").glob("*.dll")) if unity_refs() else []
    if refs:
        langversion = (UNITY_REFS / "langversion.txt").read_text(encoding="utf-8").strip()
        defines = ";".join(line.strip() for line in
                           (UNITY_REFS / "defines.txt").read_text(encoding="utf-8").splitlines()
                           if line.strip())
    else:
        langversion, defines = LANGUAGE["dotnet_langversion"], ""

    def references(private: bool) -> str:
        return "".join(
            f'\n    <Reference Include="{dll.stem}"><HintPath>{dll}</HintPath>'
            f'<Private>{str(private).lower()}</Private></Reference>' for dll in refs)

    common = f"""    <LangVersion>{langversion}</LangVersion>
    <DefineConstants>$(DefineConstants);{defines}</DefineConstants>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
    <Nullable>{LANGUAGE["dotnet_nullable"]}</Nullable>
    <ImplicitUsings>{LANGUAGE["dotnet_implicit_usings"]}</ImplicitUsings>
    <NoWarn>$(NoWarn);CS0414;CS0649;CS0169;CS0436;MSB3277</NoWarn>"""
    # 持ち込む先のパッケージはコードが参照し、テストはコードを通して受け取る。
    packages = "".join(f'\n    <PackageReference Include="{name}" Version="{versions[name]}" />'
                       for name in LANGUAGE["dotnet_packages"])
    # 入口はビルドから外す（csharp_entry_files）。
    entries = "".join(f'\n    <Compile Remove="{PROJECT / rel}" />' for rel in csharp_entry_files())
    code = f"""<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>{LANGUAGE["dotnet_target"]}</TargetFramework>
    <AssemblyName>LoopCode</AssemblyName>
{common}
  </PropertyGroup>
  <ItemGroup>
    <Compile Include="{SRC}/**/*.cs" />{entries}{packages}
  </ItemGroup>
  <ItemGroup>{references(False)}
  </ItemGroup>
</Project>
"""
    tests = f"""<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>{LANGUAGE["dotnet_test_target"]}</TargetFramework>
    <AssemblyName>LoopTests</AssemblyName>
    <IsPackable>false</IsPackable>
{common}
  </PropertyGroup>
  <ItemGroup>
    <Compile Include="{TESTS}/**/*.cs" />
    <ProjectReference Include="../code/Code.csproj" />
    <PackageReference Include="NUnit" Version="{versions['NUnit']}" />
    <PackageReference Include="NUnit3TestAdapter" Version="{versions['NUnit3TestAdapter']}" />
    <PackageReference Include="Microsoft.NET.Test.Sdk" Version="{versions['Microsoft.NET.Test.Sdk']}" />
    <PackageReference Include="JunitXml.TestLogger" Version="{versions['JunitXml.TestLogger']}" />
  </ItemGroup>
  <ItemGroup>{references(True)}
  </ItemGroup>
</Project>
"""
    return {DOTNET_BUILD / "code" / "Code.csproj": code,
            DOTNET_BUILD / "tests" / "Tests.csproj": tests,
            DOTNET_BUILD / "global.json": dotnet_global_json()}


def prepare_dotnet() -> str | None:
    """csproj を書き、中身が変わったときだけ restore する。できなければ理由を返す。

    restore はフィードだけから行う（nuget.config がほかのソースを消す）。csproj が
    変わるのは、柵の場所か持ち込む先か Unity の参照かフィードの版が変わったときだけだ。
    dotnet は build で呼ぶ。そこの global.json が SDK を決める（dotnet_global_json）。
    """
    try:
        projects = dotnet_projects()
    except (OSError, KeyError, ValueError) as e:
        return f"cannot write the C# projects: {e}"
    changed = False
    for path, text in projects.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            changed = True
    tests = DOTNET_BUILD / "tests" / "Tests.csproj"
    if changed or not (tests.parent / "obj" / "project.assets.json").exists():
        try:
            proc = run([str(LANGUAGE["test_runner"]), "restore", str(tests),
                        "--configfile", str(DOTNET_TOOLS / "nuget.config")],
                       cwd=DOTNET_BUILD, env=DOTNET_ENV, timeout=TIMEOUTS["test"])
        except subprocess.TimeoutExpired:
            return f"dotnet restore did not finish within {TIMEOUTS['test']}s"
        if proc.returncode != 0:
            return (proc.stdout + proc.stderr)[-3000:]
    return None


# ビルドエラーの1行。MSBuild は末尾に csproj のパスを [] で付ける。
DOTNET_ERROR = re.compile(r"^\s*(/[^(\n]+\.cs)\(\d+,\d+\): error (CS\d+): (.*?)(?: \[[^\]\n]*\])?\s*$",
                          re.MULTILINE)


def dotnet_build_failure(output: str) -> TestRun | None:
    """ビルドが通らず、レポートが書かれなかった走行を読む。エラーの行が無ければ None。

    テストファイルのエラーは、vitest の変換エラーと同じ「コンパイルできなかった」に
    する。TEST_WRITE はそれを見てソルバーに書き直しを頼む。C# は1つのファイルが
    壊れるとテストのアセンブリ全体がビルドできないので、どのテストも走らない。
    コードの側のエラーは別の名前にする。R2 がそのまま拒む。
    """
    errors = DOTNET_ERROR.findall(output)
    if not errors:
        return None
    kinds: list[str] = []
    lines: list[str] = []
    files: list[str] = []
    for path, code, text in errors:
        try:
            rel = Path(path).relative_to(PROJECT).as_posix()
        except ValueError:
            rel = path
        kind = (f"<did not compile: {rel}>" if in_layout(rel, "tests")
                else f"<build failed: {rel}>")
        if kind not in kinds:
            kinds.append(kind)
            files.append(rel)
        line = f"{rel}: error {code}: {text}"
        if line not in lines:
            lines.append(line)
    return TestRun(0, 0, len(kinds), 0, kinds, [], chr(10).join(lines),
                   failed_files=files)


def csharp_test_classes(files_test: list[str]) -> list[str]:
    """テストファイルが持つクラスの名前。ファイル名と同じにする決まり（layout_note）。"""
    return [Path(f).stem for f in files_test if f.endswith(".cs")]


def test_owner(name: str) -> str:
    """junit の classname を、ステップのテストファイルと突き合わせる形にする。

    NUnit の classname は "名前空間.クラス" で、クラスはファイル名と同じだ。
    ほかの言語は classname をそのまま使う。
    """
    return name.rsplit(".", 1)[-1] if LANGUAGE["name"] == "csharp" else name


# --------------------------------------------------------------------------
# C++
# --------------------------------------------------------------------------
#
# 箱で確かめるのは標準の C++17 のロジックだけだ。DXライブラリも Windows の API も
# Linux には無い。それを include するファイルは、ビルドから外す（cpp_sources）。
# ビルドは CMake で、CMakeLists.txt はランナーが柵の場所と外したファイルから書く。
# 置き場は C# の csproj と同じ理由でプロジェクトの外にする。

CPP_BUILD = LOOP / "cpp" / "build"
CMAKE = Path("/usr/bin/cmake")
# コンパイラの出力を英語の ASCII に固定する。UTF-8 のロケールでは、g++ はエラーの
# 名前を ‘ ’ で囲み、行の形で読めなくなる。
CPP_ENV = {"LC_ALL": "C", "LANG": "C"}
CPP_SOURCE_SUFFIXES = (".cpp", ".cc", ".cxx")
CPP_STUB_BODY = 'throw std::logic_error("__stub__");'
CPP_INCLUDE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*([<"])([^>"\n]+)[>"]', re.M)
# 箱にあるものとして扱うヘッダ。C++17 の標準ライブラリと C の互換ヘッダ。
# GoogleTest はテストのために箱が持つ。
CPP_STANDARD_HEADERS = frozenset("""
algorithm any array atomic bitset cassert cctype cerrno cfenv cfloat charconv chrono
cinttypes climits clocale cmath codecvt complex condition_variable csetjmp csignal
cstdarg cstddef cstdint cstdio cstdlib cstring ctime cuchar cwchar cwctype deque
exception execution filesystem forward_list fstream functional future initializer_list
iomanip ios iosfwd iostream istream iterator limits list locale map memory
memory_resource mutex new numeric optional ostream queue random ratio regex
scoped_allocator set shared_mutex sstream stack stdexcept streambuf string string_view
system_error thread tuple type_traits typeindex typeinfo unordered_map unordered_set
utility valarray variant vector
assert.h ctype.h errno.h fenv.h float.h inttypes.h limits.h locale.h math.h setjmp.h
signal.h stdarg.h stddef.h stdint.h stdio.h stdlib.h string.h time.h uchar.h wchar.h
wctype.h
""".split())
CPP_TEST_HEADERS = ("gtest/", "gmock/")


def cpp_resolve(name: str, quoted: bool, here: Path) -> Path | None:
    """include の名前を、リポジトリのファイルにする。無ければ None。

    探す順は、`"..."` なら include したファイルの隣、次に柵のコードの場所、
    最後に根。CMakeLists.txt の include の場所と同じ順だ。
    """
    bases = ([here] if quoted else []) + [SRC, PROJECT]
    for base in bases:
        target = (base / name).resolve()
        if target.is_file() and PROJECT.resolve() in target.parents:
            return target
    return None


def cpp_sources() -> tuple[list[str], dict[str, str]]:
    """柵のコードの場所の .cpp を、箱でビルドするものと外すものに分ける。

    外すのは、直接かリポジトリのヘッダを通して、標準でもリポジトリのものでもない
    ヘッダ（DxLib.h、windows.h など）を include するファイルだ。返すのは、ビルド
    するファイルと、外すファイルからそのヘッダの名前への対応。どちらも根からの
    相対パス。`#ifdef _WIN32` の中の include も数えるので、それで外れるファイルもある。
    """
    blocked: dict[Path, str | None] = {}

    def missing(path: Path, seen: set[Path]) -> str | None:
        if path in blocked:
            return blocked[path]
        if path in seen:
            return None
        seen.add(path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        found = None
        for bracket, name in CPP_INCLUDE.findall(text):
            name = name.strip()
            target = cpp_resolve(name, bracket == '"', path.parent)
            if target is not None:
                found = missing(target, seen)
            elif name not in CPP_STANDARD_HEADERS and not name.startswith(CPP_TEST_HEADERS):
                found = name
            if found:
                break
        blocked[path] = found
        return found

    built: list[str] = []
    left_out: dict[str, str] = {}
    if not SRC.is_dir():
        return built, left_out
    for path in sorted(SRC.rglob("*")):
        if not path.is_file() or path.suffix not in CPP_SOURCE_SUFFIXES:
            continue
        rel = path.relative_to(PROJECT).as_posix()
        header = missing(path.resolve(), set())
        if header:
            left_out[rel] = header
        else:
            built.append(rel)
    return built, left_out


def cpp_test_sources() -> list[str]:
    if not TESTS.is_dir():
        return []
    return [p.relative_to(PROJECT).as_posix() for p in sorted(TESTS.rglob("*"))
            if p.is_file() and p.suffix in CPP_SOURCE_SUFFIXES]


def cpp_cmake_lists() -> str:
    """ランナーが書く CMakeLists.txt。コードは静的ライブラリ、テストは gtest_main とつなぐ。

    ファイルは glob ではなく1つずつ並べる。外したファイルを入れないためと、
    ファイルが増えたことを CMake に気づかせるためだ。
    """
    code, _ = cpp_sources()

    def files(paths: list[str]) -> str:
        return "".join(f'\n    "{(PROJECT / p).as_posix()}"' for p in paths)

    include = f'"{SRC.as_posix()}" "{PROJECT.as_posix()}"'
    if code:
        library = (f"add_library(loop_code STATIC{files(code)})\n"
                   f"target_include_directories(loop_code PUBLIC {include})")
    else:
        library = ("add_library(loop_code INTERFACE)\n"
                   f"target_include_directories(loop_code INTERFACE {include})")
    return f"""cmake_minimum_required(VERSION 3.16)
project(LoopCpp CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
find_package(GTest REQUIRED)
{library}
add_executable(loop_tests{files(cpp_test_sources())})
target_link_libraries(loop_tests PRIVATE loop_code GTest::gtest_main)
"""


# g++ と ld のエラーの行。ld の行はファイルを持たないことがある。
CPP_ERROR = re.compile(r"^(/[^:\n]+):(\d+):(?:\d+:)? (?:fatal )?error: (.*)$", re.M)
CPP_UNDEFINED = re.compile(r"undefined reference to [`'](.+?)'\s*$", re.M)


def cpp_build_failure(output: str) -> TestRun | None:
    """ビルドが通らなかった出力を読む。エラーの行が無ければ None。

    dotnet_build_failure と同じく、テストファイルのエラーは「コンパイルできなかった」、
    コードの側とリンクのエラーは「ビルドできなかった」にする。
    """
    kinds: list[str] = []
    lines: list[str] = []
    files: list[str] = []
    for path, number, text in CPP_ERROR.findall(output):
        try:
            rel = Path(path).relative_to(PROJECT).as_posix()
        except ValueError:
            rel = path
        kind = (f"<did not compile: {rel}>" if in_layout(rel, "tests")
                else f"<build failed: {rel}>")
        if kind not in kinds:
            kinds.append(kind)
            files.append(rel)
        line = f"{rel}:{number}: error: {text}"
        if line not in lines:
            lines.append(line)
    undefined = list(dict.fromkeys(CPP_UNDEFINED.findall(output)))
    if undefined:
        kinds.append("<build failed: undefined reference>")
        lines += [f"undefined reference to {name}" for name in undefined]
    if not kinds:
        return None
    return TestRun(0, 0, len(kinds), 0, kinds, [], chr(10).join(lines),
                   failed_files=files)


def cpp_build(seconds: int) -> TestRun | None:
    """CMakeLists.txt を書き、ビルドする。通れば None、通らなければその判定。"""
    try:
        text = cpp_cmake_lists()
        CPP_BUILD.mkdir(parents=True, exist_ok=True)
        lists = CPP_BUILD / "CMakeLists.txt"
        if not lists.exists() or lists.read_text(encoding="utf-8") != text:
            lists.write_text(text, encoding="utf-8")
    except OSError as e:
        return TestRun(0, 0, 1, 0, ["<build failed: cannot write the CMake project>"], [], str(e))
    out = CPP_BUILD / "out"
    steps = []
    # CMakeLists.txt が変われば、--build が自分で設定し直す。
    if not (out / "CMakeCache.txt").exists():
        steps.append([str(CMAKE), "-S", str(CPP_BUILD), "-B", str(out),
                      "-G", "Unix Makefiles", "-DCMAKE_BUILD_TYPE=Debug"])
    steps.append([str(CMAKE), "--build", str(out), "--parallel", str(os.cpu_count() or 2)])
    for argv in steps:
        try:
            proc = run(argv, env=CPP_ENV, timeout=seconds)
        except subprocess.TimeoutExpired:
            return TestRun(0, 0, 1, 0, [f"<timeout: the build did not finish within {seconds}s>"],
                           [], "")
        if proc.returncode != 0:
            output = proc.stdout + proc.stderr
            return cpp_build_failure(output) or TestRun(
                0, 0, 1, 0, ["<build failed>"], [], output[-3000:])
    return None


def cpp_test_suites(files_test: list[str]) -> list[str]:
    """テストファイルが持つスイートの名前。ファイル名と同じにする決まり（layout_note）。"""
    return [Path(f).stem for f in files_test if f.endswith(CPP_SOURCE_SUFFIXES)]


# GoogleTest は、テストの本体から出た例外をこう報告する。
CPP_EXCEPTION = re.compile(r'C\+\+ exception with description "(.*?)" thrown in ', re.S)
# 標準ライブラリの at() が投げる std::out_of_range の what()。libstdc++ の形。
CPP_OUT_OF_RANGE = re.compile(r"_M_range_check|::at\b")


def cpp_failure_kind(failure: ET.Element) -> str:
    """GoogleTest の失敗の種類。junit の type は空で、例外の型を持たない。

        "BoardTest.cpp:12\\nExpected equality of these values: ..."  -> AssertionFailure
        "unknown file\\nC++ exception with description \\"__stub__\\" ..." -> StubNotImplemented

    例外の報告を先に見る。どちらでもない失敗は GoogleTest のアサーション
    （EXPECT_*、ASSERT_*、FAIL）だ。
    """
    message = (failure.get("message") or "") + "\n" + (failure.text or "")
    match = CPP_EXCEPTION.search(message)
    if match:
        what = match.group(1)
        if what == "__stub__":
            return "StubNotImplemented"
        return "std::out_of_range" if CPP_OUT_OF_RANGE.search(what) else "std::exception"
    if "Unknown C++ exception thrown in " in message:
        return "UnknownException"
    return "AssertionFailure" if message.strip() else "<no type>"


def test_argv(files_test: list[str], xml_path: Path) -> tuple[list[str], dict[str, str]]:
    """判定を出すコマンドと、それに要る環境変数。

    走らせる部分と分けてあるので、テストは実行せずに何が実行されるかを確かめ
    られる。agent_command があるのと同じ理由だ。
    """
    if LANGUAGE["name"] == "csharp":
        # テストのアセンブリは柵のテストの場所を全部コンパイルする。ステップの
        # テストだけを走らせるのは、クラスの名前での絞り込みだ。前後の "." は、
        # BoardTests が BoardTestsExtra に当たらないようにするため。
        argv = [str(LANGUAGE["test_runner"]), "test",
                str(DOTNET_BUILD / "tests" / "Tests.csproj"),
                "--no-restore", "-nologo", "-p:UseSharedCompilation=false",
                # 既定の TestResults/ は作業ツリーに書かれ、assert_touched に引っかかる。
                "--results-directory", str(xml_path.with_suffix("")),
                "--logger", f"junit;LogFilePath={xml_path}"]
        classes = csharp_test_classes(files_test)
        if classes:
            argv += ["--filter", "|".join(f"FullyQualifiedName~.{c}." for c in classes)]
        return argv, dict(DOTNET_ENV)
    if LANGUAGE["name"] == "cpp":
        # テストのプログラムは柵のテストの場所を全部持つ。ステップのテストだけを
        # 走らせるのは、スイートの名前での絞り込みだ。DISABLED_ のテストも走らせる。
        # 走らせなければ、何も判定しないテストが通ったものとして数えられる。
        argv = [str(CPP_BUILD / "out" / "loop_tests"), f"--gtest_output=xml:{xml_path}",
                "--gtest_also_run_disabled_tests"]
        suites = cpp_test_suites(files_test)
        if suites:
            argv.append("--gtest_filter=" + ":".join(f"{s}.*" for s in suites))
        return argv, dict(CPP_ENV)
    if LANGUAGE["name"] == "typescript":
        # `watch` ではなく `run`。vitest の既定は対話的で、永遠に止まったランナーは
        # 終わらないステップとまったく同じに見える。
        # 実行ファイルは npx ではなく、凍結したツールチェーンから絶対パスで取る。
        # npx は取ってくることも厭わないからだ。
        return ([str(LANGUAGE["test_runner"]), "run", *files_test,
                 "--reporter=junit", f"--outputFile={xml_path}"],
                {"CI": "1", "NO_COLOR": "1"})
    # バイトコードは作らない。.pyc は書いた者の所有になり、tests/ の下に solver
    # 所有のものがあると、ランナーはそこのモードを当て直せなくなる。
    return ([str(LANGUAGE["test_runner"]), *files_test, *PYTEST_ARGS,
             "--junitxml", str(xml_path)],
            {"PYTHONDONTWRITEBYTECODE": "1"})


def pytest_run(tag: str, files_test: list[str]) -> TestRun:
    """RUNNER_SPEC 4 章: 判定は終了コードではなく、必ず junit の XML から読む。

    渡すものは関門ごとに違い、それは意図したものだ。RED_GATE にはこのステップの
    テストファイルだけを渡す。その仕事は、これらのテストがこの実装が無いために
    落ちることを確かめることで、スイートの残りまで数えると R1 が意味を失う。
    VERIFY には tests/ 全体を渡す。その仕事はもう半分、これらがいま通り、かつ
    通っていたものが1つも落ちていないことを確かめることだからだ。"""
    report_now("runner", tag.split("-")[0])
    STATE.mkdir(parents=True, exist_ok=True)
    xml_path = STATE / f"pytest-{tag}.xml"
    argv, env = test_argv(files_test, xml_path)
    # vitest は既にあるレポートに追記するので、前の試行の古いレポートが今回の
    # 分と一緒に数えられてしまう。
    xml_path.unlink(missing_ok=True)
    csharp = LANGUAGE["name"] == "csharp"
    cpp = LANGUAGE["name"] == "cpp"
    if csharp:
        problem = prepare_dotnet()
        if problem:
            return TestRun(0, 0, 1, 0, ["<dotnet restore failed>"], [], problem)
    # C# と C++ は走らせる前にビルドする。Unity のプロジェクトでは数十秒かかる。
    seconds = max(TIMEOUTS["test"], LANGUAGE.get("min_test_timeout", 0))
    if cpp:
        failed = cpp_build(seconds)
        if failed:
            return failed
    try:
        # C# は build で呼ぶ。そこの global.json が SDK を決める（dotnet_global_json）。
        proc = run(argv, cwd=DOTNET_BUILD if csharp else PROJECT, env=env, timeout=seconds)
    except subprocess.TimeoutExpired:
        # 失敗ではなくエラーとして報告する。実際そうで、スイートは判定をまったく
        # 出していない。RED_GATE では R2 がそのまま拒み、VERIFY は失敗した試行と
        # 数えて、その理由をソルバーに伝える。
        return TestRun(0, 0, 1, 0, [f"<timeout: no verdict after {seconds}s>"], [],
                       f"The test run did not terminate within {seconds}s. The most "
                       f"likely cause is a loop in the implementation that never exits.")
    if csharp and not xml_path.exists():
        failed = dotnet_build_failure(proc.stdout + proc.stderr)
        if failed:
            return failed
    if cpp and not xml_path.exists():
        # GoogleTest はレポートを最後にまとめて書く。範囲外の添字などでプログラムが
        # 落ちると、どのテストの判定も残らない。最後の [ RUN ] の行が、落ちたテストだ。
        return TestRun(0, 0, 1, 0, [f"<test program died: exit status {proc.returncode}>"], [],
                       (proc.stdout + proc.stderr)[-3000:])
    return parse_junit(xml_path, proc.stdout + proc.stderr)


def parse_junit(xml_path: Path, output: str = "") -> TestRun:
    """テストランナーが書いたレポートから、判定を読む。

    pytest_run と分けてあるのは、関門の算術が実際にあるのがここだからだ。
    スイートを走らせてレポートを作らなくても、レポートを渡して何と言うかを
    確かめられる価値がある。

    読めないものは、無いものではなく必ずエラーにする。読み取れないレポートは
    テストについて何も言っておらず、それを「失敗なし」と読む関門は、起きても
    いない実行でステップを通してしまう。
    """
    if not xml_path.exists():
        return TestRun(0, 0, 1, 0, ["<no junit report: the test runner did not start>"], [], output)

    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError as exc:
        return TestRun(0, 0, 1, 0, [f"<unparsable junit report: {exc}>"], [], output)

    # 最初の1つではなく、すべての suite を読む。pytest は実行全体で <testsuite> を
    # 1つだけ書くので、root.find("testsuite") を読むことは合計を読むことと区別が
    # つかなかった。vitest はテストファイルごとに1つ書く。VERIFY は tests/ 全体を
    # 渡すので、複数ファイルのスイートで最初の suite だけを読むと、1ファイルだけを
    # 数えて残りを緑と呼ぶことになる。失敗を少なく数える関門は、関門が無いより悪い。
    suites = root.findall("testsuite")
    if not suites:
        return TestRun(0, 0, 1, 0, ["<malformed junit report>"], [], output)

    kinds: list[str] = []
    details: list[str] = []
    uncompiled: list[str] = []
    passed: list[str] = []
    broken: list[str] = []
    no_verdict: list[str] = []
    for case in root.iter("testcase"):
        # 一度もコンパイルされなかったテストファイル。vitest は変換の失敗を、
        # ファイルのパスそのものを名前に持つ合成の testcase 1つとして、<failure>
        # 付きで報告する。外からは、落ちるテストを1つだけ持つファイルと区別が
        # つかず、何も走っていないことに誰かが気づく前に、R1 が件数で発火する。
        # pytest にはこの場合が無い。壊れたテストモジュールは収集の <error> に
        # なり、R2 が捕まえる。
        #
        # run 8 の S1 がこれに当たった。英語の文で書かれた受け入れ条件から写した
        # テスト名が単一引用符で囲まれ、中にアポストロフィがあった。ファイルには
        # テストが12件あったのに、レポートは1件と言い、ランナーは構文エラーの修正を
        # プランナーに頼むところだった。
        if case.get("name") and case.get("name") == case.get("classname"):
            uncompiled.append(case.get("name"))
            continue
        failures = case.findall("failure")
        errors = case.findall("error")
        skips = case.findall("skipped")
        if not (failures or errors or skips):
            passed.append(case.get("name") or "<unnamed>")
        if failures or errors:
            broken.append(case.get("classname") or "<unknown>")
        if skips:
            no_verdict.append(f"{case.get('classname') or '<unknown>'}"
                              f"::{case.get('name') or '<unnamed>'}")
        for failure in failures + errors:
            kinds.append(failure_kind(failure))
            # アサーションそのもの。ソルバーが手を打てる唯一の部分だ。標準出力
            # からは取らない。pytest は失敗をそこに出すが、vitest の junit の
            # 報告はレポートのパスしか出さない。標準出力から取ると、ソルバーは
            # ファイルに失敗があることだけを知らされ、どれが、なぜかは知らされ
            # ない。実際、2つの別のバックエンドが同じ誤りを5回繰り返した。
            message = ANSI.sub("", failure.get("message") or "").strip()
            details.append(f"{case.get('name') or '<unnamed>'}"
                           + (chr(10) + "    " + message.replace(chr(10), chr(10) + "    ")
                              if message else ""))

    def total(attribute: str) -> int:
        return sum(int(suite.get(attribute, 0) or 0) for suite in suites)

    # エラーとして数え、テストの件数から除く。実際そうで、コンパイルされなかった
    # ファイルは何も走らせていない。落ちたテスト1件として報告すると、存在しな
    # かったテストについての判定になる。
    if uncompiled:
        kinds.extend(f"<did not compile: {name}>" for name in uncompiled)
        # 理由は標準出力ではなくレポートの中にある。`output` だけを渡された
        # 呼び出し側には "JUNIT report written to ..." しか見えない。これについての
        # 最初の停止は、まさにそういう見た目だった。
        detail = chr(10).join(
            ANSI.sub("", f.get("message") or "")
            for case in root.iter("testcase")
            if case.get("name") == case.get("classname")
            for f in case.findall("failure"))
        output = (output + chr(10) + detail).strip()
        return TestRun(
            tests=max(0, total("tests") - len(uncompiled)),
            failures=max(0, total("failures") - len(uncompiled)),
            errors=total("errors") + len(uncompiled),
            skipped=total("skipped"),
            failure_kinds=kinds, failure_details=details,
            passed_names=passed, output=output,
            failed_files=sorted(set(broken + uncompiled)), skipped_names=no_verdict)

    return TestRun(
        tests=total("tests"),
        failures=total("failures"),
        errors=total("errors"),
        skipped=total("skipped"),
        failure_kinds=kinds,
        failure_details=details,
        passed_names=passed,
        output=output,
        failed_files=sorted(set(broken)),
        skipped_names=no_verdict,
    )


# --------------------------------------------------------------------------
# ソルバーを呼ぶ
# --------------------------------------------------------------------------


def agent_command(user: str, script: Path, brief_path: Path, limit: int,
                  backend: str | None = None) -> list[str]:
    """エージェントを1回呼ぶための argv。

    呼び出し側と分けてあるので、「上限を渡している」ことをテストで確かめられる。
    solver-run も planner-run も2つ目の引数が無いと 900 秒を使うので、TIMEOUTS で
    上げてもここで渡さなければ上限にならない。run 5 はまさにそれで、このファイルが
    1800 と言っているのに 900 秒で2回殺された。
    """
    argv = ["sudo", "-u", user, str(script), str(brief_path), str(limit)]
    if backend is not None:
        # 渡すのは名前で、コマンドではない。名前は solver-run が解決し、
        # solver-run は root の所有だ。ランナーはバックエンドを足せず、sudo を
        # 持つ人がすでに置いたものを求めることしかできない。
        argv.append(backend)
    return argv


def quota_problem(out: str) -> str | None:
    """出力が利用上限か一時的な混雑を示していれば、その種類を返す。"""
    for kind, pattern in QUOTA_PATTERNS.items():
        if pattern.search(out):
            return kind
    return None


def unwrap_result(proc: subprocess.CompletedProcess):
    """`claude -p --output-format json` の出力から、結果の文と JSON を取り出す。

    起動スクリプトは JSON で出力させている。呼び出し側はこれまでどおり文を
    受け取り、JSON は消費量の記録に回す。JSON として読めない出力は、起動前の
    エラーや codex のように JSON を出さないバックエンドのもので、そのまま返す。
    """
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return proc, None
    if not isinstance(data, dict) or not ("result" in data or "usage" in data):
        return proc, None
    text = str(data.get("result") or data.get("subtype") or "")
    return subprocess.CompletedProcess(proc.args, proc.returncode, text + "\n",
                                       proc.stderr), data


def record_usage(who: str, phase: str, data: dict) -> None:
    """1回の呼び出しで使った枠を台帳に残す。どの役が枠を使ったかを run 後に読む。

    台帳には usage を丸ごと書く。画面には1行に要るものだけを出す。usage の
    中身は、キャッシュの内訳や iterations まで入っていて、1回の呼び出しで
    画面を数行占めていた。入力は、キャッシュから読んだ分と書いた分を足す。
    read と write はそのうちのキャッシュの読み取りと書き込みで、`loop now` が
    種類別の消費を数えるのに使う。
    """
    usage = data.get("usage") or {}
    models = sorted(data.get("modelUsage") or {})
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    tokens_in = int(usage.get("input_tokens") or 0) + cache_read + cache_write
    tokens_out = int(usage.get("output_tokens") or 0)
    seconds = (data.get("duration_ms") or 0) / 1000
    cost = data.get("total_cost_usd")
    echo = (f"who={who} phase={phase} model={','.join(models) or '-'} "
            f"in={tokens_in} out={tokens_out} read={cache_read} write={cache_write} "
            f"sec={seconds:.0f}"
            + (f" usd={cost:.2f}" if isinstance(cost, (int, float)) else "")
            + (" ERROR" if data.get("is_error") else ""))
    ledger("USAGE", echo=echo, who=who, phase=phase,
           models=models,
           usage=data.get("usage"),
           cost_usd=cost,
           duration_ms=data.get("duration_ms"),
           turns=data.get("num_turns"),
           is_error=data.get("is_error"))


def run_agent(who: str, phase: str, invoke, detail: str = "") -> subprocess.CompletedProcess:
    """エージェントを1回呼ぶ。枠が無くて失敗したときだけ、待ってやり直す。

    それ以外の結果は、成功も失敗もそのまま返す。判断は呼び出し側に残す。
    TimeoutExpired もそのまま上に通す。出力が JSON なら、消費量を台帳に残し、
    結果の文だけを返す。detail はいまの作業の説明にだけ使う（report_now）。
    """
    for round_no in range(1, QUOTA["waits"] + 2):
        report_now(who, phase, detail)
        try:
            proc, data = unwrap_result(invoke())
        finally:
            report_now("runner", "check")
        if data is not None:
            record_usage(who, phase, data)
        kind = quota_problem(proc.stdout + proc.stderr) if proc.returncode != 0 else None
        if kind is None:
            return proc
        if round_no > QUOTA["waits"]:
            break
        ledger("QUOTA_WAIT", who=who, phase=phase, kind=kind, round=round_no,
               of=QUOTA["waits"], seconds=QUOTA["wait_seconds"])
        report_now("runner", "quota")
        time.sleep(QUOTA["wait_seconds"])
    ledger("QUOTA_EXHAUSTED", who=who, phase=phase, kind=kind)
    raise QuotaExhausted(who, phase, kind)


def infra_evidence(phase: str, text: str) -> str | None:
    """箱が書き込みを拒んだ行。無ければ None。

    拒まれて当然の場所は数えない。ソルバーは plan/ も .runner も読めず、IMPL の
    あいだは凍結したテストに書けない。そこで拒まれたのはソルバーの誤りで、柵が
    効いた証拠だ。数えるのは、その位相でソルバーが書けるはずの柵と、ランナーの
    ビルドの置き場だけだ。
    """
    writable = [SRC, DOTNET_BUILD, CPP_BUILD] + ([TESTS] if phase != "IMPL" else [])
    for line in text.splitlines():
        if not INFRA_ERROR.search(line):
            continue
        for raw in re.findall(rf"{re.escape(str(LOOP))}/[^\s'\"`,:;)]+", line):
            path = Path(raw)
            if any(path == root or root in path.parents for root in writable):
                return line.strip()
    return None


def check_infra(phase: str, text: str) -> None:
    """箱が書き込みを拒んだ跡があれば、台帳に残して InfraBroken で止める。"""
    evidence = infra_evidence(phase, text)
    if evidence is None:
        return
    ledger("INFRA_FAILURE", step=NOW.get("step") or "-", phase=phase,
           evidence=evidence[-400:])
    raise InfraBroken(phase, evidence[-400:])


def call_solver(phase: str, brief: str, backend: str | None = None) -> str:
    """ソルバーにブリーフを1つだけ渡し、ほかには何も渡さない。

    ブリーフは /srv/loop/brief（runner:solverw 0750）に書く。ソルバーは読めるが
    書けず、存在する経路はそれだけだ。ここから 0700 runner の plan/ への道は
    無い。docs/ と ac/ も同じだ。だから「ソルバーは設計の全体を読んではならない」は、
    誰かが守る必要のある規則ではない。
    """
    BRIEF_DIR.mkdir(parents=True, exist_ok=True)
    brief_path = BRIEF_DIR / f"{phase.lower()}.md"
    brief_path.write_text(brief, encoding="utf-8")
    # ディレクトリの setgid ビットに頼らず、グループを明示する。ソルバーが読めない
    # ブリーフは "solver exited 2" として失敗する。真だが役に立たず、足りない
    # グループから3層離れている。
    shutil.chown(brief_path, group="solverw")
    brief_path.chmod(0o640)

    limit = TIMEOUTS["solver"]
    # 必ず名前を渡し、solver-run の既定には任せない。どのバックエンドが緑を
    # 出したかは、後で台帳から答えられなければならない。sudo の境界の向こうで
    # 決まった既定は、答えにならない。
    backend = backend or SOLVER_TIERS[0]
    TESTS_BEFORE.clear()
    TESTS_BEFORE.update(tests_snapshot())
    try:
        proc = run_agent("solver", phase, lambda: run(
            agent_command("solver", SOLVER_RUN, brief_path, limit, backend),
            timeout=limit + BACKSTOP_MARGIN))
    except subprocess.TimeoutExpired:
        # ここに来たのは、solver-run 自身のもっと短い上限が発動しなかったからだ。
        # ここからエージェントを止めることはできない。別の uid のもので、ランナーは
        # そのための sudo を持たない。だから、プロセスが消えたとほのめかさず、
        # はっきりそう言う。
        raise Halt(phase, f"solver still running after {limit + BACKSTOP_MARGIN}s",
                   "solver-run's internal timeout did not fire; a solver process "
                   "may still be alive. Check with: pgrep -a -u solver")
    out = proc.stdout + proc.stderr
    check_infra(phase, out)
    if proc.returncode == 124:
        raise SolverTimeout(phase, "solver hit its own timeout in solver-run", out[-4000:])
    if proc.returncode != 0:
        raise Halt(phase, f"solver exited {proc.returncode}", out[-4000:])

    # ほかの何かが木を見る前に引き取る。ここから先、ランナーはモードを当て直せ
    # なければならず、それができるのは自分が所有するファイルだけだ。
    taken = adopt(TESTS, SRC)
    if taken:
        ledger("ADOPT", phase=phase, backend=backend, files=taken)
    return out


# --------------------------------------------------------------------------
# ブリーフ（RUNNER_SPEC 5 章）
# --------------------------------------------------------------------------


def existing_requirements(step: dict, provided: set[str]) -> list[str]:
    """requires に書かれた名前のうち、依存先ではなく既存のコードが宣言するものの行。

    行は計画の requires の文字列ではなく、HEAD から読んだものを渡す。プランナーが
    写し間違えても、ソルバーには実際にある署名が届く。
    """
    wanted = {declared_name(r) for r in step["contracts"].get("requires", [])} - provided
    if not wanted:
        return []
    return [line for line in existing_contracts() if declared_name(line) in wanted]


def dep_contract_lines(step: dict, *, existing: bool = True) -> list[str]:
    """このステップが依存するすべてのステップの provides の行を、平らに並べる。
    `existing` なら、requires が名指しする既存の宣言の行も足す。

    dep_contracts は同じものをブリーフ用に描く。そこではステップごとのまとまりに
    価値がある。スタブの生成に要るのは名前と置き場だけなので、行で渡す。
    """
    lines: list[str] = []
    for dep in step.get("depends_on", []):
        path = STATE / "contracts" / f"{safe_id(dep)}.json"
        if not path.exists():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        provides = value.get("provides") if isinstance(value, dict) else value
        if isinstance(provides, list):
            lines.extend(str(p) for p in provides)
    if existing:
        lines += existing_requirements(step, {declared_name(line) for line in lines})
    return lines


def dep_contracts(step: dict) -> str:
    parts = []
    for dep in step.get("depends_on", []):
        path = STATE / "contracts" / f"{safe_id(dep)}.json"
        if not path.exists():
            raise Halt("PLAN_LOAD", f"module {step['id']} depends on {dep}, which has no contract yet")
        parts.append(f"From {dep}:\n{path.read_text(encoding='utf-8')}")
    found = existing_requirements(step, {declared_name(line) for line
                                         in dep_contract_lines(step, existing=False)})
    if found:
        parts.append("From the code already in the repository:\n" + "\n".join(found))
    return "\n\n".join(parts) if parts else "(none -- this module depends on nothing)"


def render_acceptance(step: dict) -> str:
    return "\n".join(
        f"{i}. [{a['case']}{', ' + a['kind'] if a.get('kind') else ''}] "
        f"given {a['given']} then {a['then']}"
        for i, a in enumerate(step["acceptance"], 1)
    )


def test_files_section(step: dict) -> str:
    """単体テストと統合テストの置き場。統合テストの条件が無ければ空。"""
    kinds = {a.get("kind") or "unit" for a in step["acceptance"]}
    if "integration" not in kinds:
        return ""
    where = (f"Write the unit criteria in {step['files_test'][0]} and the integration "
             f"criteria in\n{step['files_test'][1]}." if len(step["files_test"]) > 1 else
             f"Every criterion is an integration criterion: write them all in\n"
             f"{step['files_test'][0]}.")
    return f"""
{where} An integration test runs this module together with the
real modules it requires -- no stand-ins, no mocks of them.
"""


def render_provides(step: dict) -> str:
    return "\n".join(step["contracts"]["provides"])


def render_invariants(step: dict) -> str:
    inv = step["contracts"].get("invariants") or []
    return "\n".join(f"- {i}" for i in inv) if inv else "(none stated)"


def solver_material(step: dict, context: str) -> str:
    """TEST_WRITE と IMPL のブリーフを、この同じ本文で始める。

    背景、契約、署名は同じモジュールの位相をまたいで同じだ。先頭がそろっていれば、
    後の呼び出しはこの部分をプロンプトキャッシュから読む。位相ごとの指示は、
    この後に置く。受け入れ条件は TEST_WRITE にだけ渡すので、ここには入れない。
    """
    return f"""Below is the context for one module of a build. Your task follows
after it.

# Project context
{context}

# Contracts you may rely on
{dep_contracts(step)}

# Invariants that must hold
{render_invariants(step)}

# Signatures this step provides
{render_provides(step)}
{library_refs_section()}"""


def replaced_tests_section(replaced: list[str], expected: int) -> str:
    """TEST_WRITE のブリーフの、差し替える既存のテストファイルについての節。"""
    if not replaced:
        return ""
    return f"""
# These test files already exist, and you replace them
{chr(10).join(replaced)}

Their tests check behaviour this step changes, so they are removed. Write each
of these files from scratch with this step's tests only. Do not keep, adapt or
copy the old tests. Together with the other files above, the step has exactly
{expected} tests.

You may read one of these files to see how it is set up (imports, namespace,
fixtures), but its tests describe the old behaviour. If your editing tool
refuses to overwrite a file you have not read, read its first line (offset 1,
limit 1) and then write the whole file.
"""


def brief_test_write(step: dict, context: str, broken: str = "",
                     replaced: list[str] | None = None, crashed: str = "") -> str:
    # goal は渡さない。テストは受け入れ条件から作るもので、これからソルバーに
    # 頼む実装の説明から作るものではない。
    acceptance = render_acceptance(step)
    return f"""{solver_material(step, context)}
# Your task

Write tests for the signatures above. Do not write an implementation.

# Acceptance criteria -- write exactly {step["expected_tests"]} tests, one per criterion
{acceptance}

# Files you may create or modify
{chr(10).join(step["files_test"])}
{test_files_section(step)}
Write nothing outside those paths. The implementation does not exist yet, so
every test you write must fail when run against {LANGUAGE["stub_shape"]}.
Do not weaken a test to make it pass, and do not create the module under test.
{LANGUAGE["stub_note"]}{replaced_tests_section(replaced or [], step["expected_tests"])}{naming_note()}{compile_failure_section(broken)}{crash_section(crashed)}"""


def compile_failure_section(broken: str) -> str:
    """コンパイラ自身の言葉を、そのまま返す。

    前もって与えた警告より価値がある。ブリーフはすでに、単一引用符のテスト名に
    アポストロフィを入れるなと言っていたが、ソルバーはそれでも2回そうした。誤りの
    前に読んだ規則は、ブリーフのほかのすべてと競り合う。エラーメッセージは誤りの
    後に、それだけで、行と列を持って届く。
    """
    if not broken:
        return ""
    return f"""
# YOUR LAST ATTEMPT DID NOT COMPILE

Nothing you wrote ran. The file was discarded and you are writing it again.

{broken}

Read the line and column. Fix that, and check every other line for the same
mistake before you finish -- whatever produced it once usually produced it in
several places.
"""


def crashed_tests(run_: TestRun) -> str:
    """スタブに対して、テスト自身の誤りで落ちたテストの名前と例外。無ければ空。

    赤と認めない失敗が1つでもあり、そのすべてが言語の `fixable_crashes` に当たる
    ときだけ返す。ほかの種類が混ざっていれば、壊れているのは契約かもしれず、
    書き直しでは直らない。
    """
    fixable = LANGUAGE["fixable_crashes"]
    bad = [(kind, detail) for kind, detail in zip(run_.failure_kinds, run_.failure_details)
           if not LANGUAGE["red_kinds"].match(kind)]
    if fixable is None or not bad or not all(fixable.match(kind) for kind, _ in bad):
        return ""
    return chr(10).join(detail for _, detail in bad)


def crash_section(crashed: str) -> str:
    """スタブに対して落ちたテストを、そのまま返す。compile_failure_section と同じ理由だ。"""
    if not crashed:
        return ""
    return f"""
# YOUR LAST ATTEMPT CRASHED AGAINST THE STUB

The file compiled and ran, but these tests died on an exception of their own,
not on an assertion and not on the stub's exception. A crash is not a red test.
The file was discarded and you are writing it again.

{crashed}

In each of them, find the line that uses a member while it can still be null
or too short -- often in the arrange part, before anything is asserted -- and
put an assertion in front of it. Check every other test for the same pattern
before you finish.
"""


def naming_note() -> str:
    """名指しする価値のある罠を1つ伝える。条件そのものが作る罠だからだ。

    上の条件は英語の文で、英語の文にはアポストロフィがある。それをテスト名に
    写すのは自然なことで、単一引用符の文字列の中ではそこで文字列が終わる。run 8 の
    S1 はテストを12件書き、1件も走らなかった:
    `it('... then the result's resource is exactly 1',`。

    ファイルはコンパイルできず、vitest はそれをファイル名を持つ合成の失敗テスト
    1件として報告した。ランナーは「1件、期待は12件」と見て、構文エラーを
    プランナーに送るところだった。報告の側も直してあるが、ブリーフで取り除ける
    罠は取り除いたほうがよい。文はその罠を持つ言語の `naming_note` にある。
    """
    return LANGUAGE["naming_note"]


# --------------------------------------------------------------------------
# スタブを頼まずに組み立てる
# --------------------------------------------------------------------------
#
# スタブの仕事は1つだけだ。形は正しく、値はすべて間違っていること。そうすれば
# RED_GATE は、各テストが実装が無いために落ちるのを見られる。この仕事に判断は
# 要らず、`contracts.provides` が必要なものをすべて運んでいる。署名、すべての型の
# 形、それぞれの置き場。L14 と L15 はそれを保証するためにある。
#
# 8回の run のあいだソルバーに頼んでいて、頼むことの代償は得るものより大きかった。
# run 8 の S1 は、条件に正しく答えるスタブのせいで RED_GATE で4回拒まれた。
# boolean に `return false`、温度を下げてモデルが外れを引かなくなってからは
# createGame に `return {resource: 0, generators: {}, lastUpdate: 0}`。最も
# ありそうな答えで、正解でもある。ブリーフは既定値を返すなとはっきり禁じていた。
# それは欠けずに届き、読めて、設定を変えて2回とも無視された。
#
# モデルが無視できる指示は、モデルが届かない規則より価値が低い。chmod 777 を
# 見張るのではなく sudo を取り上げるのと同じ理屈だ。だからランナーがスタブを書く。
#
# 作りとして慎重にしてある。自信をもって読み取れないものは None を返し、これまで
# どおりソルバーに頼む。コンパイルできない生成スタブは、置き換える問題より悪い。

TS_DECLARATION = re.compile(
    r"^(?P<file>[\w./-]+\.ts)\s*:\s*"
    r"(?P<kind>interface|type|function|const)\s+(?P<name>\w+)(?P<rest>.*)$")
TS_SIGNATURE = re.compile(r"^\((?P<args>.*)\)\s*:\s*(?P<returns>.+?)\s*$")


def literal_keys(step: dict) -> list[str]:
    """条件が引用している識別子らしい文字列を、現れた順に返す。

    キーを持つコンテナの番兵の値は、呼び出し側が引くキーを持っていなければ
    ならない。そうでないと、テストは値で落ちる前にキーを引くところで壊れる。
    `CATALOG.cursor.rate` は比較の前に例外を投げ、RED_GATE はそれを R5 で拒む。
    契約はどのキーかを言えない。`Record<string, GeneratorDef>` は型であって、
    名簿ではない。条件は言え、条件はランナーが読んでよいものだ。
    これは STUB のブリーフが防いでいる漏れではない。あの規則は条件を「モデル」から
    遠ざけ、見せられた答えを決め打ちさせないためのものだ。ここでは何にも何も
    見せない。ランナーがキーの名前を取り、番兵の値で埋めるので、値はすべて
    間違ったままだ。
    多めに集めても害は無い。誰も引かないキーは、コンテナのキーについての
    アサーションを食い違わせるだけで、それはどのみち望む結果だ。
    """
    text = " ".join(f"{a.get('given', '')} {a.get('then', '')}"
                    for a in step.get("acceptance", []))
    seen: dict[str, None] = {}
    for token in re.findall(r"['\"]([A-Za-z_]\w*)['\"]", text):
        seen.setdefault(token, None)
    return list(seen)


def split_top(text: str, separators: str) -> list[str]:
    """括弧の外にある区切り文字だけで分ける。

    `(a: A, b: B) => C` の中の `,` や、`=>` の `>` で分けてはいけない。
    """
    parts, current, depth, previous = [], [], 0, ""
    for ch in text:
        if ch in "([{<":
            depth += 1
        elif ch in ")]}" or (ch == ">" and previous != "="):
            depth -= 1
        if ch in separators and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
        previous = ch
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def split_function_type(kind: str) -> tuple[str, str] | None:
    """`(args) => returns` を (args, returns) に分ける。関数の型でなければ None。

    正規表現では足りない。引数の中に関数の型があると、最後の `) =>` まで
    引数として読んでしまう。先頭の括弧に対応する閉じ括弧を数えて探す。
    """
    kind = kind.strip()
    if not kind.startswith("("):
        return None
    depth = 0
    for index, ch in enumerate(kind):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                rest = kind[index + 1:].lstrip()
                if rest.startswith("=>"):
                    return kind[1:index], rest[2:].strip()
                return None
    return None


def interface_fields(rest: str) -> list[tuple[str, str]]:
    """interface の中身を (名前, 型) の並びにする。メソッドは関数の型に直す。

    `subscribe(listener: (s: S) => void): () => void` を、名前 `subscribe`、
    型 `(listener: (s: S) => void) => () => void` として扱う。以前は
    「名前: 型」の正規表現で読み、メソッドの引数をフィールドと取り違えて、
    コンパイルできないスタブを書いていた（Claude で回した run 8 の S1 で6回）。
    """
    start, end = rest.find("{"), rest.rfind("}")
    if start < 0 or end < start:
        return []
    fields = []
    for member in split_top(rest[start + 1:end], ";,\n"):
        method = re.match(r"^(?:readonly\s+)?(\w+)\??\s*\(", member)
        if method:
            opened = member.index("(")
            depth = 0
            for index in range(opened, len(member)):
                if member[index] == "(":
                    depth += 1
                elif member[index] == ")":
                    depth -= 1
                    if depth == 0:
                        after = member[index + 1:].lstrip()
                        returns = after[1:].strip() if after.startswith(":") else "void"
                        fields.append((method.group(1),
                                       f"({member[opened + 1:index]}) => {returns}"))
                        break
            continue
        prop = re.match(r"^(?:readonly\s+)?(\w+)\??\s*:\s*(.+)$", member, re.S)
        if prop:
            fields.append((prop.group(1), prop.group(2).strip()))
    return fields


def sentinel_for(kind: str, types: dict, keys: list[str] | None = None) -> str:
    """その型の値のうち、正しい実装がどんな入力に対しても返さないもの。

    唯一の例外は boolean で、そういう値が無い。どの boolean もどこかでは正解だ。
    だからキャストをまとった文字列を返す。形の規則を破るのはここだけで、意図して
    破っている。型のどの値もどこかで正しいなら、正しい型の値で間違うことは
    できない。
    """
    kind = kind.strip().rstrip(";")
    if kind in ("void", "undefined"):
        return ""
    if kind == "number":
        return "-999999"
    if kind == "string":
        return '"__stub__"'
    if kind == "boolean":
        return '"__stub__" as unknown as boolean'
    # 関数の型は、呼べる関数を返す。キャストした文字列だと、テストが呼んだ
    # ところで TypeError になり、RED_GATE がそれを赤と認めない（R5）。
    # `[]` の判定より先に見る。`() => number[]` は配列ではない。
    function = split_function_type(kind)
    if function is not None:
        value = sentinel_for(function[1], types, keys) or "undefined"
        return f"((..._args: unknown[]) => ({value}))"
    if kind.endswith("[]"):
        return "[" + sentinel_for(kind[:-2], types, keys) + "]"
    match = re.fullmatch(r"(Array|ReadonlyArray)<(.+)>", kind)
    if match:
        return "[" + sentinel_for(match.group(2), types, keys) + "]"
    match = re.fullmatch(r"(Record|Map)<\s*[^,]+,\s*(.+)>", kind)
    if match:
        value = sentinel_for(match.group(2), types, keys)
        # __stub__ は本物の名前と並べてあえて残す。条件が挙げる名前だけにすると、
        # コンテナのキーについてのアサーションが「一致」し、スタブに対して通る
        # テストがステップを止める。
        names = ["__stub__"] + list(keys or [])
        return "{ " + ", ".join(f'"{n}": {value}' for n in names) + " }"
    if kind in types:
        return types[kind]
    # 合併型、表が知らない総称型、このステップ以外から import した名前。キャスト
    # すれば、形の規則がこれの正体について嘘をつかずに済み、テストはそれでも
    # 比較で落ちる。
    return f'"__stub__" as unknown as {kind}'


def ts_type_values(declarations: list[dict], keys: list[str] | None = None) -> dict:
    """このステップが宣言する名前付きの型ごとに、番兵の値をリテラルで作る。

    2周する。interface は、後で宣言される別の interface を持ちうるからだ。
    リンタが受け入れるものなら2周で足りる。循環はキャストに落ち、それでも
    コンパイルできる。
    """
    types: dict = {}
    for _ in range(2):
        for d in declarations:
            if d["kind"] == "interface":
                fields = interface_fields(d["rest"])
                if not fields:
                    continue
                types[d["name"]] = "{ " + ", ".join(
                    f"{f}: {sentinel_for(t, types, keys)}" for f, t in fields) + " }"
            elif d["kind"] == "type":
                rhs = d["rest"].split("=", 1)[-1].strip()
                if rhs:
                    types[d["name"]] = sentinel_for(rhs, types, keys)
    return types


def parse_contracts(lines: list[str]) -> list[dict] | None:
    """provides の各行を分解する。形に合わない行が1つでもあれば None。"""
    declarations = []
    for line in lines:
        head = line.split(" -- ")[0].split(chr(8212))[0].strip()
        match = TS_DECLARATION.match(head)
        if not match:
            return None
        declarations.append(match.groupdict())
    return declarations


# C# のスタブの本体。番兵の値は使わない。bool には誤った値が無く、文字列を
# キャストして押し込む手は InvalidCastException になる（実測）。どの型にも同じ
# 1行で済み、R5 はこの印の付いた例外だけを赤と認める（csharp_failure_kind）。
CSHARP_STUB_EXPRESSION = 'throw new System.NotImplementedException("__stub__")'
CSHARP_STUB_BODY = "{ " + CSHARP_STUB_EXPRESSION + "; }"
CSHARP_STUB_MARK = re.compile(r"^\s*System\.NotImplementedException\s*:\s*__stub__\s*$")
CSHARP_ACCESS = re.compile(r"^(?:public|internal|protected|private)\b")
CSHARP_MODIFIERS = r"(?:(?:public|internal|protected|private|static|abstract|sealed|partial|readonly|virtual|override|new)\s+)*"
# 型の名前はドットで区切れる。`Board.Cell` は Board の中の入れ子の型。
CSHARP_TYPE_NAME = r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*"
# `(...)` はプライマリコンストラクタ（C# 12）。Promete のシーンはこの形で書く
# （`class MainScene(Keyboard keyboard) : Scene`）。
CSHARP_TYPE_LINE = re.compile(
    rf"^(?P<mods>{CSHARP_MODIFIERS})(?P<kind>class|struct|interface|enum)\s+(?P<name>{CSHARP_TYPE_NAME})"
    r"(?P<params>\s*\([^()]*\))?"
    r"(?P<bases>\s*:\s*[^{]+?)?\s*(?:\{(?P<body>.*)\})?\s*;?\s*$")
CSHARP_CTOR_LINE = re.compile(
    rf"^(?P<mods>{CSHARP_MODIFIERS})(?P<type>(?:[A-Za-z_]\w*\.)*(?P<last>[A-Za-z_]\w*))\.(?P=last)"
    r"\s*\((?P<params>.*)\)\s*;?\s*$")
CSHARP_MEMBER_LINE = re.compile(
    rf"^(?P<mods>{CSHARP_MODIFIERS})(?P<ret>\S.*?)\s+(?P<type>{CSHARP_TYPE_NAME})\.(?P<name>[A-Za-z_]\w*)"
    r"\s*(?P<rest>\(.*\)|\{.*\})?\s*;?\s*$")
# 契約に現れたら `using UnityEngine;` を足す型。全部は知らないので、よく出る
# ものだけを挙げる。無い型がコンパイルで落ちれば、RED_GATE がそのまま止める。
CSHARP_UNITY_TYPES = re.compile(
    r"\b(?:Vector[234](?:Int)?|Quaternion|Color(?:32)?|Mathf|Rect(?:Int)?|Bounds(?:Int)?|"
    r"Matrix4x4|GameObject|Transform|MonoBehaviour|ScriptableObject|Sprite|Texture2D|"
    r"AudioClip|KeyCode|Ray|RaycastHit)\b")


def csharp_members(body: str) -> list[str]:
    """型の本体をメンバーに分ける。深さ 0 の `;` と、深さ 0 に戻る `}` で切る。

    `int Height { get; set; } int Score(Board b);` のように、プロパティの `}` の
    後に `;` が無くても2つに分ける。
    """
    members, current, depth = [], [], 0
    for ch in body:
        current.append(ch)
        if ch in "({[<":
            depth += 1
        elif ch in ")]>":
            depth -= 1
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                members.append("".join(current))
                current = []
                continue
        if ch == ";" and depth == 0:
            members.append("".join(current)[:-1])
            current = []
    members.append("".join(current))
    return [m.strip() for m in members if m.strip()]


def csharp_stub_member(member: str) -> str:
    """クラスか構造体のメンバー1つを、スタブの宣言にする。"""
    if not CSHARP_ACCESS.match(member):
        member = "public " + member
    if "(" in member and member.rstrip().endswith(")") and "=>" not in member:
        return f"{member} {CSHARP_STUB_BODY}"       # メソッドとコンストラクタ
    if member.rstrip().endswith("}"):
        return member                                # 自動プロパティ
    return member + ";"                              # フィールド


def csharp_type_block(entry: dict) -> str:
    """スタブの型1つ。入れ子の型（entry["nested"]）はメンバーの後ろに、字下げして置く。"""
    separator = ",\n" if entry["kind"] == "enum" else "\n"
    members = [m for m in entry["members"] if m] + [n["block"] for n in entry.get("nested", [])]
    inner = separator.join("    " + m.replace("\n", "\n    ") for m in members)
    return f"{entry['head']}\n{{\n{inner}\n}}"


def csharp_namespace(path: str) -> str | None:
    """新しいファイルの名前空間。柵の中のフォルダから作る（layout_note の決まり）。

    根にあれば空文字（グローバル名前空間）。識別子にならないフォルダ名なら None。
    """
    prefix = LAYOUT["src"] + "/"
    if not path.startswith(prefix):
        return None
    folders = path[len(prefix):].split("/")[:-1]
    if not all(re.fullmatch(r"[A-Za-z_]\w*", f) for f in folders):
        return None
    return ".".join(folders)


def generate_csharp_stub(step: dict, requires: list[str],
                         originals: dict[str, str]) -> dict[str, str] | None:
    """C# のスタブを契約から書く。None ならソルバーに頼む。

    メソッドとコンストラクタの本体は CSHARP_STUB_BODY。フィールドと自動プロパティは
    宣言のまま残す（値を持つだけで、振る舞いが無い）。既存のファイルは
    csharp_merge_stub がメンバーの本体だけを差し替える。読めない行が1つでもあれば
    None。
    """
    types: dict[str, dict] = {}     # 型の名前 -> {path, head, members, specs}
    order: list[str] = []
    for line in step["contracts"]["provides"]:
        match = re.match(r"^([\w./-]+\.cs)\s*:\s*(.+)$",
                         line.split(" -- ")[0].split(chr(8212))[0].strip())
        if not match:
            return None
        path, decl = match.group(1), match.group(2).strip()
        if path not in step["files_write"]:
            return None
        declared = CSHARP_TYPE_LINE.match(decl)
        if declared:
            name, kind = declared.group("name"), declared.group("kind")
            mods = declared.group("mods").strip()
            if not CSHARP_ACCESS.match(mods):
                mods = ("public " + mods).strip()
            entry = types.setdefault(name, {"path": path, "members": [], "specs": []})
            if entry["path"] != path or "head" in entry:
                return None
            short = name.rpartition(".")[2]
            entry["head"] = (f"{mods} {kind} {short}{(declared.group('params') or '').strip()}"
                             f"{declared.group('bases') or ''}").rstrip()
            entry["kind"] = kind
            entry["bases"] = declared.group("bases") or ""
            body = entry["body"] = declared.group("body") or ""
            if kind == "enum":
                entry["members"].append(body.strip())
            elif kind == "interface":
                entry["members"] += [m if m.endswith("}") else m + ";"
                                     for m in csharp_members(body)]
            else:
                entry["members"] += [csharp_stub_member(m) for m in csharp_members(body)]
                entry["specs"] += csharp_members(body)
            if name not in order:
                order.append(name)
            continue
        ctor = CSHARP_CTOR_LINE.match(decl)
        member = CSHARP_MEMBER_LINE.match(decl) if not ctor else None
        if ctor:
            owner = ctor.group("type")
            text = f"{ctor.group('mods')}{ctor.group('last')}({ctor.group('params')})"
        elif member:
            owner = member.group("type")
            text = (f"{member.group('mods')}{member.group('ret')} {member.group('name')}"
                    f"{member.group('rest') or ''}")
        else:
            return None
        entry = types.setdefault(owner, {"path": path, "members": [], "specs": []})
        if entry["path"] != path:
            return None
        entry["members"].append(csharp_stub_member(text.strip()))
        entry["specs"].append(text.strip())
        if owner not in order:
            order.append(owner)

    if {t["path"] for t in types.values()} != set(step["files_write"]):
        return None

    # 入れ子の型（`Tactical.SpawnTracker`）の外側の型が、契約に行を持たないことがある。
    # 外側の型も、同じファイルの型として扱う。
    for name in list(order):
        outer = name.rpartition(".")[0]
        while outer:
            if outer not in types:
                types[outer] = {"path": types[name]["path"], "members": [], "specs": []}
                order.insert(order.index(name), outer)
            elif types[outer]["path"] != types[name]["path"]:
                return None
            outer = outer.rpartition(".")[0]

    # 型の宣言の行が無いメンバーは、普通のクラスに入れる。既存のファイルでは、
    # 型の行があったか（typed）で、型の頭を確かめるかどうかを分ける。
    for name, entry in types.items():
        entry["name"] = name
        entry["typed"] = "head" in entry
        entry.setdefault("head", "public class " + name.rpartition(".")[2])
        entry.setdefault("kind", "class")
        entry["nested"] = []

    # 入れ子の型は、外側の型のメンバーとして書く。深いものから組み立てて外側に渡す。
    # 入れ子の型の既定は private なので、型の頭には public を付けてある。
    for name in sorted((n for n in types if "." in n), key=lambda n: -n.count(".")):
        if types[name.rpartition(".")[0]]["kind"] in ("enum", "interface"):
            return None
        types[name.rpartition(".")[0]]["nested"].append(
            {"name": name.rpartition(".")[2], "block": csharp_type_block(types[name])})
    order = [name for name in order if "." not in name]

    # 名前空間。既存のファイルは今の名前空間を保つ（layout_note）。requires の行は
    # `-- namespace X` を持てばそれ、無ければ作業ツリーのファイル、それも無ければ
    # フォルダから決める。前のステップが書き換えた既存のファイルの行は、名前空間を
    # 持たないが作業ツリーにある。
    namespaces: dict[str, str] = {}
    known: dict[str, str] = {}      # 型の名前 -> 名前空間。既存のファイルの using に使う
    for path in step["files_write"]:
        if path in originals:
            outline = csharp_outline(originals[path])
            ns = outline["types"][0]["namespace"] if outline and outline["types"] else ""
        else:
            ns = csharp_namespace(path)
            if ns is None:
                return None
        namespaces[path] = ns
        known.update({name: ns for name in order if types[name]["path"] == path})
    for line in requires:
        m = re.match(r"^([\w./-]+\.cs)\s*:", line.strip())
        if not m:
            continue
        stated = re.search(r" -- namespace ([\w.]+)\s*$", line)
        if stated:
            ns = stated.group(1)
        elif m.group(1) in namespaces:
            ns = namespaces[m.group(1)]
        else:
            ns = csharp_file_namespace(m.group(1))
        namespaces.setdefault(m.group(1), ns)
        known.setdefault(declared_name(line).split(".")[0], ns)
    # 持ち込む先のライブラリの型（Promete の Scene や Mouse）。契約が口にすれば using を
    # 足す。プロジェクトの型と同じ名前なら、プロジェクトの型を取る。
    library = {name: ns for name, ns in library_namespaces().items() if name not in known}
    known.update(library)

    unity = unity_refs() and any(
        CSHARP_UNITY_TYPES.search(line) for line in step["contracts"]["provides"] + requires)
    files: dict[str, str] = {}
    for path in step["files_write"]:
        if path in originals:
            merged = csharp_merge_stub(
                originals[path], [types[name] for name in order if types[name]["path"] == path],
                known, unity_refs())
            if merged is None:
                return None
            files[path] = merged
            continue
        own = namespaces.get(path, "")
        text = "\n\n".join(csharp_type_block(types[name]) for name in order
                           if types[name]["path"] == path)
        used = {ns for name, ns in library.items()
                if re.search(rf"(?<![\w.]){re.escape(name)}(?!\w)", text)}
        usings = ["System", "System.Collections.Generic", "System.Linq"] \
            + (["UnityEngine"] if unity else []) \
            + sorted({ns for ns in namespaces.values() if ns and ns != own} | used)
        if own:
            text = f"namespace {own}\n{{\n" + "\n".join(
                f"    {line}" if line else "" for line in text.splitlines()) + "\n}"
        files[path] = "".join(f"using {u};\n" for u in usings) + "\n" + text + "\n"
    return files


def generate_stub(step: dict, requires: list[str],
                  originals: dict[str, str] | None = None) -> dict[str, str] | None:
    """スタブ全体を返す。None なら、やはりソルバーに頼む。

    None は恥ずべき失敗ではない。これが言語の2つ目の、より劣った構文解析器に
    ならずに済むのは None のおかげだ。見慣れないものは、前から扱っていた経路に
    戻す。

    `originals` は、files_write のうち最後のコミットにあるファイルの中身だ。
    そのファイルでは、provides の名前の宣言だけをスタブに差し替え、ほかは残す。
    丸ごと書き直すと、ステップに関係の無い関数まで消える。
    """
    originals = originals or {}
    if LANGUAGE["name"] == "csharp":
        return generate_csharp_stub(step, requires, originals)
    if LANGUAGE["name"] != "typescript":
        return None   # Python はいまも頼む。まだこれを要したことが無い

    declarations = parse_contracts(step["contracts"]["provides"])
    if declarations is None:
        return None
    if {d["file"] for d in declarations} != set(step["files_write"]):
        # ステップが書くべきなのにどの契約も述べていないファイルか、その逆。
        # どちらにしても、ランナーはそれを書けるほど知らない。
        return None

    # 型の表には、このステップ自身のものだけでなく、前のステップが宣言したものも
    # 入れる。S2 は `const CATALOG: Catalog` を提供し、Catalog は S1 の
    # `Record<string, GeneratorDef>` だ。S1 の行が無いとその名前は分からず、番兵の
    # 値はキャストに落ち、CATALOG['cursor'].baseCost を読む最初のテストは、
    # アサーションの失敗ではなく TypeError を受け取る。RED_GATE はそれをそのまま
    # 拒む（R2/R5）。ブリーフはずっとそう言っていた。テストは確かめる前に値を
    # 分解するので、番兵の値は署名が述べる「形」を持たなければならない。
    # 1行ずつ読む。既存のコードの行には、クラスやアロー関数の const のように
    # この形に合わないものがある。まとめて読むと、その1行のせいで依存先の型が
    # すべて消える。
    inherited = [d for line in requires for d in (parse_contracts([line]) or [])]
    keys = literal_keys(step)
    types = ts_type_values(inherited + declarations, keys)
    # よそから import する名前の置き場。書き出すファイルがそれを言えるようにする。
    elsewhere: dict[str, str] = {}
    for line in requires:
        match = TS_DECLARATION.match(line.split(" -- ")[0].split(chr(8212))[0].strip())
        if match:
            elsewhere[match.group("name")] = match.group("file")

    files: dict[str, str] = {}
    for path in step["files_write"]:
        mine = [d for d in declarations if d["file"] == path]
        declared_here = {d["name"] for d in mine}
        body: list[str] = []
        pieces: dict[str, str] = {}
        needed: dict[str, set] = {}

        for d in mine:
            if d["kind"] in ("interface", "type"):
                # interface の本体の後にセミコロンは要らず、型の別名には要る。
                # `}` の後に書いても正しいが、人が開くかもしれないファイルでは
                # 読みにくい。
                line = f"export {d['kind']} {d['name']}{d['rest']}".rstrip().rstrip(";")
                body.append(line if line.endswith("}") else line + ";")
            elif d["kind"] == "const":
                kind = d["rest"].split(":", 1)[-1].strip() if ":" in d["rest"] else "unknown"
                body.append(f"export const {d['name']}: {kind} = "
                            f"{sentinel_for(kind, types, keys)};")
            else:
                signature = TS_SIGNATURE.match(d["rest"].strip())
                if signature is None:
                    return None
                returns = signature.group("returns")
                value = sentinel_for(returns, types, keys)
                body.append(
                    f"export function {d['name']}({signature.group('args')}): {returns} {{"
                    + (f"{chr(10)}  return {value};{chr(10)}}}" if value else f"{chr(10)}}}"))
            pieces[d["name"]] = body[-1]

        # 既存のファイルでは、差し替えなかった部分がすでに宣言や import をしている
        # 名前に import を足さない。同じ名前を2度 import すると、esbuild はファイル
        # ごと拒む。
        rest = ""
        if path in originals:
            merged = ts_merge_stub(originals[path], pieces)
            if merged is None:
                return None
            merged_text, rest = merged

        def known(name: str) -> bool:
            return bool(re.search(rf"(?<![\w$]){re.escape(name)}(?![\w$])", rest))

        # import: このファイルが口にし、別のファイルが宣言している名前すべて。
        text = chr(10).join(body)
        for name, source in elsewhere.items():
            if name in declared_here or known(name) \
                    or not re.search(rf"(?<!\w){name}(?!\w)", text):
                continue
            needed.setdefault(relative_module(path, source), set()).add(name)
        for d in declarations:
            if d["file"] == path or d["name"] in declared_here or known(d["name"]):
                continue
            if re.search(rf"(?<!\w){d['name']}(?!\w)", text):
                needed.setdefault(relative_module(path, d["file"]), set()).add(d["name"])

        imports = [f'import {{ {", ".join(sorted(names))} }} from "{module}";'
                   for module, names in sorted(needed.items())]
        header = chr(10).join(imports) + chr(10) * 2 if imports else ""
        files[path] = header + (merged_text if path in originals else text + chr(10))
    return files


def relative_module(importer: str, target: str) -> str:
    """`importer` が `target` をどう指すか。TypeScript が求める書き方で返す。"""
    rel = os.path.relpath(Path(target).with_suffix("").as_posix(),
                          Path(importer).parent.as_posix()).replace(os.sep, "/")
    return rel if rel.startswith(".") else "./" + rel


def existing_files_section(existing: list[str]) -> str:
    """スタブのブリーフの、既存のファイルについての節。無ければ空。"""
    if not existing:
        return ""
    return f"""
# These files already exist
{chr(10).join(existing)}

They hold code that is not yours: other functions, classes, constants, imports
and comments. Read each one before you write to it. Change only the
declarations of the names under "Signatures to provide": replace each one with
its stub as described below, keeping the signature given above. A name the file
does not have yet goes at the end of the file. Leave every other line exactly
as it is. Do not rewrite the file from scratch.

The runner compares everything else in these files with the last commit, and a
stub that changed or removed anything else stops the step. Adding an import
that a new signature needs is allowed.
"""


def brief_stub(step: dict, existing: list[str] | None = None) -> str:
    # 署名だけを渡す。goal も acceptance も invariants も渡さない。形ではなく意味を
    # 運ぶものがここにあると、それは忠実に実装され、それが覆う条件は一度も落ちる
    # のを見られないまま RED_GATE を通る（2026-08-18 にステップ S1 で痛い目を見て
    # 分かった）。
    if LANGUAGE["name"] == "csharp":
        # C# は番兵の値を使わない（CSHARP_STUB_BODY）。下の本文の半分は、値を
        # 選ぶことの難しさについてで、ここでは要らない。
        return f"""Create stubs only.

# Signatures to provide
{render_provides(step)}

# Files you may create or modify
{chr(10).join(step["files_write"])}
{existing_files_section(existing or [])}
Write each declaration with exactly the signature above. The body of every
method, constructor and property accessor you write is exactly this one
statement and nothing else:

    {CSHARP_STUB_BODY}

Keep the message "__stub__" exactly. The runner accepts a test that fails on
this exception as a test that fails against the stub; any other exception --
including a NotImplementedException with another message -- rejects the step.

Do not return values. C# has no wrong value for a bool, and forcing one in
with a cast throws InvalidCastException, which the runner rejects. Fields and
auto-properties (`{{ get; set; }}`) stay as declared: they hold values and have
no behaviour to stub.

Put each type in the namespace that follows its folder under {LAYOUT["src"]}/,
and add the `using` directives the signatures need. Implement no behaviour
whatsoever.
"""
    if LANGUAGE["name"] == "cpp":
        # C++ も番兵の値を使わない。C# と同じ理由で、印の付いた例外を投げる。
        return f"""Create stubs only.

# Signatures to provide
{render_provides(step)}

# Files you may create or modify
{chr(10).join(step["files_write"])}
{existing_files_section(existing or [])}
Declare each name in its header with exactly the signature above, and define
it in the `.cpp` of the same name. The body of every function and constructor
you write is exactly this one statement and nothing else:

    {CPP_STUB_BODY}

Add `#include <stdexcept>` to each `.cpp` that has one. Keep the message
"__stub__" exactly. The runner accepts a test that fails on this exception as a
test that fails against the stub; any other exception rejects the step.

Do not return values. Give every data member a value-initialiser (`int
width{{}};`, `std::vector<Cell> cells{{}};`) and nothing else: members hold
values and have no behaviour to stub, and an uninitialised one holds garbage
that a test could pass against by chance.

Every header starts with `#pragma once` and includes the headers its
signatures need. Write ASCII only. Implement no behaviour whatsoever.
"""
    return f"""Create stubs only.

# Signatures to provide
{render_provides(step)}

# Files you may create or modify
{chr(10).join(step["files_write"])}
{existing_files_section(existing or [])}
Each function must have exactly the signature above and must return a
CONSPICUOUS SENTINEL: a value of the declared return type that no correct
implementation would produce for any input. For a str return a marker such as
"__stub__"; for an int something like -999999; for a list a list holding one
such marker.

Do NOT return an empty, zero, or default value ("", 0, [], None), and do not
return an argument unchanged. Those are answers a correct implementation gives
for some input, so a test covering that input would pass against the stub -- and
a test that passes here has never shown it can fail, which rejects the step.

# When the type has no wrong value

A boolean has two values and a correct implementation returns each of them for
some input, so THERE IS NO WRONG BOOLEAN. Returning `false` answers every
criterion of the form "returns false when ...", and that criterion then passes
against the stub and stops the step -- run 8's S1 was rejected twice in a row
for exactly this, on `canAfford`.

This applies to BOOLEANS and to nothing else unless the type has the same
problem -- an enum or a union of two or three members. A number is not such a
type: -999999 is wrong for every input a correct implementation would see, and
it still behaves like a number, so a test that rounds it or compares it fails
on the assertion rather than on the call. The same goes for strings, arrays and
objects. Asked for this once, a stub came back with every return replaced by a
string, numbers included, which turns an assertion failure into a TypeError and
RED_GATE rejects that outright (R5).

For a boolean, THE VALUE YOU RETURN MUST NOT BE A VALUE OF THAT TYPE AT ALL.
Return the string sentinel and cast it past the type checker:

    TypeScript:  return "__stub__" as unknown as boolean;
    Python:      return "__stub__"  # type: ignore[return-value]

`"__stub__"` is neither true nor false, so `toBe(true)` and `toBe(false)` both
fail, and both fail on the comparison -- an assertion failure, the only kind
RED_GATE accepts.

The cast is not the point and casting alone does nothing. This was written
exactly once and came back as `return false as unknown as boolean`, which is
still false and still answered "returns false when ..." correctly. If the value
you typed is `true` or `false`, you have not done this.

This is the one place the rule above is deliberately broken, and only here: a
value of the right type cannot be wrong when every value of that type is right
somewhere.

The sentinel must also have the SHAPE the signature states, because the test
takes it apart before it asserts anything. A tuple returns exactly as many
elements as the annotation lists, each a sentinel of its own type
(tuple[GameState, int] -> a GameState-shaped sentinel and -999999). A dict
returns every key a caller of this signature would look up. A dataclass returns
an instance with every field filled with a sentinel.

A value the caller cannot take apart does not fail as a red test; it fails as a
broken call, which RED_GATE rejects outright (R5) and no later phase can repair.

Implement no behaviour whatsoever: no validation, no type checks, no special
cases, no branching on the input. Do not raise NotImplementedError; the tests
must fail on an assertion, not on an exception.
"""


# IMPL のブリーフに載せるファイルの中身の上限（字数の合計）。取り込んだ Unity の
# プロジェクトの回で、既存のファイルを書き換える IMPL は10〜23ターン回り、ソルバーの
# 消費の63%を占めた。ファイルを Read するたびに、それまでの文脈を送り直す。
# 載せきれないファイルは、名前だけを挙げてソルバーに読ませる。
IMPL_FILE_CHARS = 60_000


def current_files_section(step: dict) -> str:
    """files_write のうち、作業ツリーにあるファイルの今の中身。無ければ空。

    試行のたびに読み直す。修理の試行では、前の試行が書いたものが今の中身だ。

    Claude Code の Edit と Write は、同じ呼び出しの中で Read していないファイルを
    拒む。1行だけの Read でも通るので、全文を読み直させずに1行だけ読ませる。
    """
    shown, skipped, total = [], [], 0
    for rel in step["files_write"]:
        path = PROJECT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if total + len(text) > IMPL_FILE_CHARS:
            skipped.append(rel)
            continue
        total += len(text)
        shown.append(f"--- {rel} ---\n{text}")
    if not shown and not skipped:
        return ""
    parts = ["\n# The files you may modify, as they are now\n"]
    if shown:
        parts.append("\n\n".join(shown))
        parts.append("""
These are the current contents; you do not need to read them again. If your
editing tool refuses a file you have not read, read only its first line
(offset 1, limit 1) and then edit it.
""")
    if skipped:
        parts.append("Too large to show here; read these yourself:\n"
                     + chr(10).join(skipped) + "\n")
    return "\n".join(parts)


def original_code_section(step: dict) -> str:
    """files_write のうち、最後のコミットにあったファイルの、今の中身との差分。無ければ空。

    STUB は既存のメソッドの本体をスタブに差し替える。今の中身だけを見せると、
    ソルバーは元の分岐も、同じファイルの既存のヘルパーを呼んでいたことも知らずに
    本体を一から書く。取り込んだ Unity のプロジェクトの回では、`else if` を1つ
    足せば済むところで分岐をまるごと書き直し、既存の処理と同じ static 関数を
    いくつも足した。`-` の行が元のコードだ。差分だけにするのは、ファイル全体を
    二重に載せないためだ。
    """
    diffs, total = [], 0
    for rel, before in head_sources(step["files_write"]).items():
        path = PROJECT / rel
        after = path.read_text(encoding="utf-8") if path.is_file() else ""
        if after == before:
            continue
        diff = "".join(difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            f"a/{rel} (before this step)", f"b/{rel} (now)", n=5))
        if total + len(diff) > IMPL_FILE_CHARS:
            continue
        total += len(diff)
        diffs.append(diff)
    if not diffs:
        return ""
    return """
# The code before this step
The stub replaced the bodies of the members this step changes. Lines starting
with `-` below are the original code; that is where your work starts, not from
the stub.

""" + "\n".join(diffs) + """
Edit the original code, do not rewrite it. Keep its branches, its calls and its
structure, and change only what the tests need: if a new case is required, add
an `else if` (or a `case`) to the existing chain rather than replacing the
chain. Before adding a helper method, check whether the original code, the
files above or the rest of the project (search it with Grep) already have one
that does the job, and call it.
"""


def brief_impl(step: dict, context: str, tests_text: str, last_failure: str) -> str:
    return f"""{solver_material(step, context)}
# Your task

Make the tests pass by implementing the signatures above.

# Goal
{step["goal"]}

# The tests (frozen -- read-only, and they will not be accepted if modified)
{tests_text}
{current_files_section(step)}{original_code_section(step)}
# How the tests are failing right now
{last_failure or "(nothing recorded)"}

# Files you may create or modify
{chr(10).join(step["files_write"])}

Change nothing outside those paths. The tests are the specification: if a test
looks wrong, say so in your final message rather than editing it -- editing it
will be detected and the step will stop.

Make the smallest change that passes the tests. Reuse the code that is already
there -- existing methods, fields and helpers -- instead of writing new ones
that do the same thing, and do not add methods the tests do not need. Search
the project with Grep and Glob before writing something that may already exist.

Do not guess at a library's API. Code that looks members up by name at run
time (reflection, `getattr`) and quietly falls back when they are missing
compiles and passes the tests, then does nothing in the real program. If you
do not know a library member, look for how the project or the context above
uses it; if neither says, write that in your final message instead of guessing.
"""


def frozen_tests_text(step: dict) -> str:
    parts = []
    for rel in step["files_test"]:
        path = PROJECT / rel
        parts.append(f"--- {rel} ---\n{path.read_text(encoding='utf-8')}")
    return "\n\n".join(parts)


def absorb_timeout(step: dict, attempt: int, backend: str,
                   manifest: dict[str, str], last_failure: str) -> str:
    """IMPL の時間切れを、試行1回分の失敗として片付ける。次のブリーフに渡す文を返す。

    書きかけの作業は捨てる。途中で止まったファイルは、半分だけ書き換わっている
    かもしれず、次の試行が修理の土台にするには信用できない。

    それでも、成功した試行と同じ検査は通す。時間切れの前に、凍結したテストや許可
    リストの外に手を出していれば、それは試行の失敗ではなく柵の違反で、今までどおり
    止める。引き取りを先にするのは、solver が書いたファイルを runner が扱えるように
    するためだ（adopt を参照）。
    """
    adopt(TESTS, SRC)
    for rel, digest in manifest.items():
        if sha256(PROJECT / rel) != digest:
            raise Halt("VERIFY", f"frozen test was modified: {rel}")
    assert_touched("IMPL", step["files_test"] + step["files_write"])
    dropped = discard_attempt(step["files_write"])
    seconds = TIMEOUTS["solver"]
    ledger("SOLVER_TIMEOUT", step=step["id"], attempt=attempt, backend=backend,
           seconds=seconds, files=dropped)
    return (f"Your previous attempt ran out of time: it was stopped after {seconds}s, "
            "before it finished. Nothing it wrote was kept -- the files you may "
            "modify are back to what they were before it started.\n\n"
            "Do not try to do everything in one pass. Make the smallest change "
            "that addresses the failures below, write it, and stop.\n\n"
            + (last_failure or "(nothing recorded)"))


# --------------------------------------------------------------------------
# 契約の名前と、条件の規則
# --------------------------------------------------------------------------

def declared_name(line: str) -> str:
    """契約の行が宣言する名前。言語ごとの形（`provides_pattern`）で読む。
    何も宣言していなければ行全体。

    requires と provides は、この名前で突き合わせる。両側で、名前が取れないときの
    扱いを同じにしてある。合わない行を片側だけで捨てると、規則が厳しくなるのでは
    なく、答えようのないものになる。
    """
    match = LANGUAGE["provides_pattern"].search(line)
    if not match:
        return line.strip()
    # C# の型とメンバーのように、組が2つある言語もある。当たった方を取る。
    return next(group for group in match.groups() if group)


# 受け入れ条件の「具体的」: 数、引用符で囲んだリテラル、例外の型。形容詞だけでできた
# 受け入れ条件は、2人が同じように書けるテストにならない。
#
# 数字ではなく数を見る。`\d` だけだと、`s2` という変数名の "2" で満たされ、
# "s2 == p.state exactly" が具体的な結果として通った。その条件はスタブに対して
# 通り、ステップを R4 で止めた。
#
# 空のコレクションと Python の定数もリテラルとして数える。境界のケースで一番
# よく出る答えは `returns exactly []` や `{}` や `None` で、どれも具体的な値だ。
# 数えないと、プランナーは `len(tokenize(""))` → `0` のように言い換えて逃げるか、
# 書き直しで呼び出しを1回使う。TypeScript の true / false / null / undefined は
# 数えない。英語の単語と見分けがつかず、"is true for every" のような形容が通る。
CONCRETE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?!\w)|'[^']*'|\"[^\"]*\"|\b[A-Z]\w*(?:Error|Exception)\b"
                      r"|\[\]|\{\}|\(\)|\b(?:True|False|None)\b")

# 反射でコードの形だけを確かめる条件。スタブは契約どおりの型とメンバーを持つ
# ので、型の名前、基底型、メソッドの有無はスタブの時点で通り、R4 が必ず止める。
# 取り込んだ Promete のプロジェクトで、`given: typeof(Program), typeof(MainScene)` と
# `then: has a static method named 'Main'; base type whose Name is 'Scene'` の条件が
# R4 で止まり、人が2回消した。前提が反射で、かつ結果が形を主張するときだけ
# 落とす。片方だけなら、振る舞いを確かめる条件かもしれない。
REFLECTION = re.compile(
    r"\btypeof\s*\(|\breflect(?:ion|ing|ed)?\b|\bGetType\s*\(|\bAssembly\b"
    r"|\.(?:GetMethods?|GetPropert(?:y|ies)|GetFields?|GetMembers?|GetInterfaces|BaseType"
    r"|IsSubclassOf|IsAssignableFrom|EntryPoint)\b|\binspect\.|\bhasattr\s*\(",
    re.IGNORECASE)
SHAPE = re.compile(
    r"\bhas (?:an? |the )?(?:public |static |private |protected |instance )*"
    r"(?:method|property|field|member|constructor|attribute)s?\b"
    r"|\bbase ?type\b|\bderives? from\b|\binherits?\b|\bsubclass\b|\bimplements?\b"
    r"|\bEntryPoint\b|\b(?:type )?name(?:d| is)\b|\bis (?:a |an )?(?:public|static|sealed|abstract|const)\b",
    re.IGNORECASE)

CASES = {"normal", "boundary", "error"}


def modules_of(files_write: list[str]) -> list[str]:
    """ファイルが作る、import できるモジュール名。

    src/incgame/engine.py     -> incgame.engine       (Python)
    src/incgame/__init__.py   -> incgame
    src/idlegame/engine.ts    -> idlegame/engine      (TypeScript)
    src/idlegame/index.ts     -> idlegame

    2つの言語は同じ考えを違う綴りで書く。区切り文字と、「ディレクトリそのもの」を
    意味する名前だ。
    """
    suffix = LANGUAGE["source_suffix"]
    separator = LANGUAGE["module_separator"]
    index = LANGUAGE["index_name"]
    prefix = LAYOUT["src"] + "/"
    names = []
    for f in files_write:
        if not f.startswith(prefix) or not f.endswith(suffix):
            continue
        parts = f[len(prefix):-len(suffix)].split("/")
        if parts[-1] == index:
            parts = parts[:-1]
        if parts:
            names.append(separator.join(parts))
    return names


# --------------------------------------------------------------------------
# 設計仕様書を読む（RUNNER_SPEC 6 章）
# --------------------------------------------------------------------------
#
# docs/modules/ の詳細設計仕様書は、人が読む日本語の文と、ランナーが読む契約の
# ブロックを持つ。ランナーが読むのは契約のブロックと節の区切りだけで、文の中身は
# 判断しない。受け入れ条件は ac/ にモジュールごとの JSON で置く。

CONTRACT_BLOCK = re.compile(r"^```ndjson contract[ \t]*\r?\n(.*?)^```[ \t]*$", re.M | re.S)
CONTRACT_OPEN = re.compile(r"^```ndjson contract\b", re.M)
OVERVIEW_HEADING = "## 概要"
BEHAVIOUR_HEADING = "## 振る舞い"
CONTRACT_HEADING = "## 契約"
JAPANESE = re.compile(r"[぀-ヿ一-鿿]")
# モジュールの id は src/ の下のパスから拡張子を落としたもの（`game/board`）。
MODULE_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*(?:/[A-Za-z0-9_][A-Za-z0-9_-]*)*")
KINDS = {"unit", "integration"}
CRITERION_KEYS = {"case", "kind", "given", "then"}


def section(text: str, heading: str) -> str | None:
    """`heading` の行から、次の1段か2段の見出しの前まで。無ければ None。

    コードブロックの中の `#` の行は見出しに数えない。
    """
    lines = text.splitlines()
    fenced = False
    for i, line in enumerate(lines):
        if line.startswith("```"):
            fenced = not fenced
            continue
        if fenced or line.strip() != heading:
            continue
        body, inner = [], False
        for rest in lines[i + 1:]:
            if rest.startswith("```"):
                inner = not inner
            elif not inner and re.match(r"#{1,2} ", rest):
                break
            body.append(rest)
        return "\n".join(body).strip()
    return None


def heading_count(text: str, heading: str) -> int:
    """コードブロックの外にある `heading` の行の数。"""
    fenced, count = False, 0
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and line.strip() == heading:
            count += 1
    return count


def without_contract(text: str) -> str:
    """契約のブロックを除いた本文。"""
    return CONTRACT_BLOCK.sub("", text).strip()


def is_japanese(text: str) -> bool:
    """契約のブロックと見出しを除いた本文が、日本語で書かれているか。

    必須の見出し（`## 振る舞い` など）は日本語なので、見出しも数えると、本文が
    英語の文書も通る。
    """
    prose = [line for line in without_contract(text).splitlines()
             if not line.lstrip().startswith("#")]
    return bool(JAPANESE.search("\n".join(prose)))


def norm_sig(sig: str) -> str:
    """署名の比較に使う形。空白の違いは数えない。"""
    return " ".join(sig.split())


def parse_contract(text: str) -> tuple[list[dict], list[dict], list[str]]:
    """契約のブロックを読む。(provides, requires, 問題) を返す。"""
    opened = len(CONTRACT_OPEN.findall(text))
    blocks = CONTRACT_BLOCK.findall(text)
    if opened != 1 or len(blocks) != 1:
        return [], [], [f"has {opened} `ndjson contract` block(s); exactly one is required"]
    provides: list[dict] = []
    requires: list[dict] = []
    problems: list[str] = []
    for n, raw in enumerate(blocks[0].splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as bad:
            problems.append(f"contract line {n} is not JSON: {bad.msg}")
            continue
        if isinstance(value, dict) and "provides" in value:
            sig = value["provides"]
            if set(value) - {"provides", "entry"} or not isinstance(sig, str) \
                    or not sig.strip() or not isinstance(value.get("entry", False), bool):
                problems.append(f'contract line {n}: a provides line is {{"provides": '
                                f'<signature>}}, with "entry": true when something outside '
                                f'this design calls it')
                continue
            provides.append({"sig": norm_sig(sig), "entry": value.get("entry", False)})
        elif isinstance(value, dict) and "requires" in value:
            if set(value) != {"requires", "from"} \
                    or not all(isinstance(value[k], str) and value[k].strip() for k in value):
                problems.append(f'contract line {n}: a requires line is {{"requires": '
                                f'<signature>, "from": <module>}}')
                continue
            requires.append({"sig": norm_sig(value["requires"]), "from": value["from"].strip()})
        else:
            problems.append(f"contract line {n} is neither a provides nor a requires line")
    return provides, requires, problems


def parse_ac(text: str | None, where: str) -> tuple[list[dict], list[str]]:
    """受け入れ条件の JSON を読む。(条件, 問題) を返す。"""
    if text is None:
        return [], [f"{where}: missing; every module has acceptance criteria"]
    try:
        value = json.loads(text)
    except json.JSONDecodeError as bad:
        return [], [f"{where}: is not JSON: {bad}"]
    criteria = value.get("criteria") if isinstance(value, dict) else None
    if not isinstance(criteria, list) or not criteria:
        return [], [f'{where}: needs a non-empty "criteria" list']
    problems: list[str] = []
    kept: list[dict] = []
    for n, c in enumerate(criteria, 1):
        if not isinstance(c, dict) or set(c) != CRITERION_KEYS \
                or not all(isinstance(c[k], str) and c[k].strip() for k in CRITERION_KEYS):
            problems.append(f"{where}: criterion {n} must have exactly case, kind, given "
                            f"and then, each a non-empty string")
            continue
        if c["case"] not in CASES:
            problems.append(f"{where}: criterion {n} case '{c['case']}' is not one of "
                            f"{sorted(CASES)}")
        if c["kind"] not in KINDS:
            problems.append(f"{where}: criterion {n} kind '{c['kind']}' is not one of "
                            f"{sorted(KINDS)}")
        # 人間のレビューを省くには、機械が確かめられる条件が要る。`then` は単独で
        # 確かめる。前提の中の数で通ると、2つの呼び出しの比べ合いが残る。
        if not CONCRETE.search(c["then"]):
            problems.append(f"{where}: the [{c['case']}] criterion {n} states no concrete "
                            f"value in `then`")
        # スタブはすでに契約どおりの形を持つので、形だけを確かめる条件は R4 で止まる。
        if REFLECTION.search(c["given"]) and SHAPE.search(c["then"]):
            problems.append(f"{where}: the [{c['case']}] criterion {n} checks the shape of "
                            f"the code through reflection; a stub already has that shape, "
                            f"so its test passes before anything is implemented (R4)")
        kept.append(c)
    missing = CASES - {c["case"] for c in kept}
    if kept and missing:
        problems.append(f"{where}: has no criterion of case {sorted(missing)}")
    return kept, problems


def module_files(module_id: str) -> list[str]:
    """モジュールを作るファイル。C++ はヘッダとソースの対。"""
    return [f"{LAYOUT['src']}/{module_id}{s}" for s in LANGUAGE["module_suffixes"]]


def module_test_files(module_id: str, integration: bool, unit: bool = True) -> list[str]:
    """モジュールのテストのファイル。ランナーが決め、ソルバーはここに書く。

    単体テスト、統合テストの順で、条件のある種類のものだけを返す。条件の無い
    ファイルを求めると、ソルバーは書くものが無く、TEST_WRITE が空のファイルで
    止まる。テストを選ぶのにファイル名を使う言語（C# のクラス名、C++ のスイート名）
    があるので、名前は言語の決まりに合わせる。
    """
    tests = LAYOUT["tests"]
    parent, _, name = module_id.rpartition("/")
    folder = f"{tests}/{parent}/" if parent else f"{tests}/"
    language = LANGUAGE["name"]
    if language == "python":
        flat = module_id.replace("/", "_")
        names = [f"{tests}/test_{flat}.py", f"{tests}/test_{flat}_integration.py"]
    elif language == "typescript":
        dotted = module_id.replace("/", ".")
        names = [f"{tests}/{dotted}.test.ts", f"{tests}/{dotted}.integration.test.ts"]
    elif language == "csharp":
        names = [f"{folder}{name}Tests.cs", f"{folder}{name}IntegrationTests.cs"]
    else:
        names = [f"{folder}{name}Test.cpp", f"{folder}{name}IntegrationTest.cpp"]
    if not integration:
        return names[:1]
    return names if unit else names[1:]


def safe_id(step_id: str) -> str:
    """モジュールの id を、ファイル名とタグに使える形にする。"""
    return step_id.replace("/", "--")


def shapeless_types(sig: str) -> list[str]:
    """署名の中の、中身を述べていないコンテナ型。

    後のモジュールに渡るのは契約の行だけだ。`-> tuple` は要素の数を言わないので、
    呼び出し側は受け取ったものを分解できず、スタブは何を返せばよいか分からない。
    """
    shapeless = LANGUAGE["shapeless"]
    opener = LANGUAGE["shape_bracket"]
    pattern = LANGUAGE.get("shape_pattern", ANNOTATION)
    return sorted({name for name, bracket in pattern.findall(sig)
                   if name in shapeless and bracket != opener})


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_texts(root: Path, suffix: str) -> dict[str, str]:
    """root の下の `suffix` のファイルを、root からの相対パス（拡張子なし）ごとに読む。"""
    if not root.is_dir():
        return {}
    found: dict[str, str] = {}
    for directory, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix != suffix or path.is_symlink():
                continue
            rel = path.relative_to(root).as_posix()
            found[rel[:-len(suffix)]] = path.read_text(encoding="utf-8")
    return found


def head_files(prefix: str) -> set[str]:
    """最後のコミットにある、`prefix` の下のファイル。"""
    try:
        listing = run(["git", "ls-tree", "-r", "-z", "--name-only", "HEAD", "--", prefix])
    except OSError:
        return set()
    return set(filter(None, listing.stdout.split("\0"))) if listing.returncode == 0 else set()


def design_problems(docs: dict[str, str],
                    acs: dict[str, str]) -> tuple[dict[str, dict], list[str]]:
    """詳細設計仕様書と受け入れ条件を読み、形を確かめる。(モジュール, 問題) を返す。"""
    if not docs:
        return {}, ["there is no module design: write at least one modules/<path>.md"]
    modules: dict[str, dict] = {}
    problems: list[str] = []
    for mid, text in sorted(docs.items()):
        where = f"modules/{mid}.md"
        if not MODULE_ID.fullmatch(mid):
            problems.append(f"{where}: the path does not name a source file under "
                            f"{LAYOUT['src']}/ (letters, digits, '_' and '-' only)")
            continue
        provides, requires, bad = parse_contract(text)
        problems += [f"{where}: {b}" for b in bad]
        for heading in (BEHAVIOUR_HEADING, CONTRACT_HEADING):
            if heading_count(text, heading) != 1:
                problems.append(f"{where}: needs exactly one `{heading}` section")
        if not is_japanese(text):
            problems.append(f"{where}: is not written in Japanese")
        if not bad and not provides:
            problems.append(f"{where}: the contract provides nothing")
        for p in provides:
            bare = shapeless_types(p["sig"])
            if bare:
                problems.append(
                    f"形: {mid} provides `{p['sig']}` with {', '.join(bare)} left "
                    f"unparameterised; a later module is handed this line and nothing "
                    f"else, so the contents have to be in it ({LANGUAGE['shape_example']})")
        criteria, bad_ac = parse_ac(acs.get(mid), f"ac/{mid}.json")
        problems += bad_ac
        if any(c["kind"] == "integration" for c in criteria) and not requires:
            problems.append(f"ac/{mid}.json: has integration criteria, but {mid} requires "
                            f"nothing from another module")
        modules[mid] = {
            "id": mid, "doc": text, "provides": provides, "requires": requires,
            "criteria": criteria, "files": module_files(mid),
            "files_test": module_test_files(
                mid, any(c["kind"] == "integration" for c in criteria),
                unit=any(c["kind"] != "integration" for c in criteria)),
            "design": text_digest(text + "\0" + (acs.get(mid) or "")),
        }
    for mid in sorted(set(acs) - set(docs)):
        problems.append(f"ac/{mid}.json: there is no modules/{mid}.md for it")
    # テストのファイル名は、C# のクラス名や C++ のスイート名になる。重なると、
    # 2つのモジュールのテストが同じ名前で選ばれる。
    owners: dict[str, str] = {}
    for mid, module in modules.items():
        for stem in sorted({Path(f).name for f in module["files_test"]}):
            if stem in owners:
                problems.append(f"{owners[stem]} and {mid} would both have tests named "
                                f"{stem}; rename one of the modules")
            owners[stem] = mid
    return modules, problems


def module_deps(modules: dict[str, dict]) -> dict[str, list[str]]:
    """モジュールごとの、この設計の中の依存先。"""
    return {mid: sorted({r["from"] for r in m["requires"]
                         if r["from"] in modules and r["from"] != mid})
            for mid, m in modules.items()}


def module_order(modules: dict[str, dict]) -> tuple[list[str], list[str]]:
    """依存先が先に来る順。輪があれば、(並べられた分, 輪に関わるモジュール)。"""
    remaining = module_deps(modules)
    order: list[str] = []
    while remaining:
        ready = sorted(mid for mid, deps in remaining.items() if set(deps) <= set(order))
        if not ready:
            return order, sorted(remaining)
        order += ready
        for mid in ready:
            del remaining[mid]
    return order, []


def interface_problems(modules: dict[str, dict]) -> list[str]:
    """インターフェースチェック。振る舞いは確かめず、契約の辻褄だけを見る。

    存在       requires の名前を、from のモジュールか既存の src/ が提供している
    署名の一致 計画したモジュールから求める署名が、提供する署名と同じ
    依存の循環 モジュールの依存に輪が無い
    未使用     どのモジュールも求めない provides が無い（"entry": true を除く）
    居場所     1つの名前を提供するモジュールは1つ
    """
    problems: list[str] = []
    provided = {mid: {declared_name(p["sig"]): p["sig"] for p in m["provides"]}
                for mid, m in modules.items()}
    existing: dict[str, list[str]] | None = None
    head: set[str] | None = None
    for mid, module in sorted(modules.items()):
        for r in module["requires"]:
            name, source = declared_name(r["sig"]), r["from"]
            if source == mid:
                problems.append(f"存在: {mid} requires `{name}` from itself")
            elif source in modules:
                sig = provided[source].get(name)
                if sig is None:
                    problems.append(f"存在: {mid} requires `{name}` from {source}, which "
                                    f"does not provide it")
                elif sig != r["sig"]:
                    problems.append(f"署名の一致: {mid} requires `{r['sig']}` from "
                                    f"{source}, which provides `{sig}`")
            else:
                if head is None:
                    head, existing = head_files(LAYOUT["src"]), existing_declarations()
                paths = [f for f in module_files(source) if f in head]
                lines = [line for path in paths for line in existing.get(path, [])]
                if not paths:
                    problems.append(f"存在: {mid} requires `{name}` from {source}, which is "
                                    f"neither a module of this design nor a file under "
                                    f"{LAYOUT['src']}/")
                elif lines and name not in {declared_name(line) for line in lines}:
                    problems.append(f"存在: {mid} requires `{name}` from {source}, whose "
                                    f"code does not declare it")
    _, cycle = module_order(modules)
    if cycle:
        problems.append(f"依存の循環: these modules depend on one another in a cycle, or "
                        f"on such a cycle: {', '.join(cycle)}")
    used = {(r["from"], declared_name(r["sig"]))
            for m in modules.values() for r in m["requires"]}
    for mid, module in sorted(modules.items()):
        for p in module["provides"]:
            if not p["entry"] and (mid, declared_name(p["sig"])) not in used:
                problems.append(f"未使用: {mid} provides `{declared_name(p['sig'])}`, which no "
                                f"module requires; mark it \"entry\": true if something "
                                f"outside this design calls it")
    owners: dict[str, str] = {}
    for mid, module in sorted(modules.items()):
        for p in module["provides"]:
            name = declared_name(p["sig"])
            if name in owners and owners[name] != mid:
                problems.append(f"居場所: `{name}` is provided by both {owners[name]} and {mid}")
            owners.setdefault(name, mid)
    return problems


def design_digest(modules: dict[str, dict]) -> str:
    """設計全体のハッシュ。クリティックが読んだ設計と、承認する設計を突き合わせる。"""
    return text_digest(json.dumps({mid: m["design"] for mid, m in sorted(modules.items())},
                                  sort_keys=True))


def basic_problems(docs: dict[str, str]) -> list[str]:
    """基本設計仕様書の形。概要の節がちょうど1つあり、日本語で書いてある。"""
    if not docs:
        return ["there is no basic design: write at least one basic/<name>.md"]
    problems = []
    overviews = sum(heading_count(text, OVERVIEW_HEADING) for text in docs.values())
    if overviews != 1:
        problems.append(f"the basic design needs exactly one `{OVERVIEW_HEADING}` section "
                        f"across its files, not {overviews}")
    for name, text in sorted(docs.items()):
        if not is_japanese(text):
            problems.append(f"basic/{name}.md: is not written in Japanese")
    return problems


def basic_overview() -> str:
    """基本設計仕様書の概要の節。"""
    for text in read_texts(DOCS_BASIC, ".md").values():
        found = section(text, OVERVIEW_HEADING)
        if found is not None:
            return found
    return ""


def current_design() -> tuple[dict[str, dict], list[str]]:
    """docs/modules/ と ac/ にある、いまの設計。"""
    return design_problems(read_texts(DOCS_MODULES, ".md"), read_texts(AC, ".json"))


# --------------------------------------------------------------------------
# 設計から下流の計画を作る
# --------------------------------------------------------------------------
#
# 下流の位相はステップの辞書で回る。モジュールをそれに直したものを .runner/tasks.json
# に置く。受け入れ条件を含むので、コミットしない。


def legacy_line(sig: str, files: list[str]) -> str:
    """契約の署名を、スタブを作る道具が読む行の形にする。置き場のファイルを添える。"""
    language = LANGUAGE["name"]
    if language == "python":
        modules = modules_of(files)
        return f"{sig} -- defined in {modules[0]}" if modules else sig
    if language == "typescript":
        return f"{files[0]}: " + re.sub(r"^export\s+(?:declare\s+)?", "", sig)
    return f"{files[0]}: {sig}"


def solver_tools_note() -> str:
    """ソルバーがコマンドを走らせられないなら、そう言う文。"""
    if any(tier in SOLVERS_THAT_RUN_COMMANDS for tier in SOLVER_TIERS):
        return ""
    return """The solver cannot run commands -- not the tests, and not anything else. It
reads files and writes files, and that is all it can do. The runner runs the
tests after the solver has finished and hands the failures to the next attempt.
"""


def module_context(module: dict, modules: dict[str, dict], overview: str) -> str:
    """ソルバーに渡す背景。システムの目的、置き方、自分の設計、依存先の振る舞い。

    ソルバーは docs/ を読めないので、要る部分だけをランナーが渡す。署名は契約の
    節から別に渡す（dep_contracts と render_provides）。
    """
    deps = module_deps(modules)[module["id"]]
    behaviours = "\n\n".join(
        f"### {dep}\n{section(modules[dep]['doc'], BEHAVIOUR_HEADING) or '(not written)'}"
        for dep in deps) or "(none -- this module uses no other module of this design)"
    return f"""## What the system is for
The overview of the basic design, written in Japanese:

{overview or "(not written)"}

## How the code is laid out
{layout_text(LANGUAGE["layout_note"]).strip()}

{solver_tools_note()}
## This module: {module["id"]} ({", ".join(module["files"])})
Its detailed design, written in Japanese. The signatures it provides are listed
under "Signatures this step provides" below.

{without_contract(module["doc"])}

## What the modules it uses do
{behaviours}
"""


def build_tasks(modules: dict[str, dict], stage: dict) -> dict:
    """設計から、下流が回すステップの並びを作る。依存先が先に来る。"""
    order, _ = module_order(modules)
    overview = basic_overview()
    deps = module_deps(modules)
    steps = []
    for mid in order:
        module = modules[mid]
        steps.append({
            "id": mid,
            "kind": "integration" if deps[mid] else "unit",
            "goal": f"Implement the module {mid} so that its design above holds.",
            "depends_on": deps[mid],
            "contracts": {
                "provides": [legacy_line(p["sig"], module["files"])
                             for p in module["provides"]],
                "requires": [legacy_line(r["sig"], module_files(r["from"]))
                             for r in module["requires"]],
                "invariants": [],
            },
            "acceptance": module["criteria"],
            "files_write": module["files"],
            "files_test": module["files_test"],
            "expected_tests": len(module["criteria"]),
            "max_attempts": 3,
            "design": module["design"],
            "context": module_context(module, modules, overview),
        })
    return {"version": 2, "language": stage.get("language") or LANGUAGE["name"],
            "framework": stage.get("framework") or LANGUAGE["framework"],
            "digest": design_digest(modules), "steps": steps}


def load_plan(step_id: str) -> tuple[dict, str]:
    tasks = json.loads(TASKS.read_text(encoding="utf-8"))
    steps = {s["id"]: s for s in tasks["steps"]}
    if step_id not in steps:
        raise Halt("PLAN_LOAD", f"no module {step_id} in the approved design")
    return steps[step_id], steps[step_id].get("context", "")


def green_designs() -> dict[str, str]:
    """緑のモジュールと、緑になったときの設計のハッシュ。台帳から読む。

    REOPEN は、そのモジュールの緑を取り消す。設計が変わったモジュールだけを、
    下流に流し直すためだ。
    """
    done: dict[str, str] = {}
    if not LEDGER.exists():
        return done
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("event") == "GREEN" and rec.get("step"):
            done[rec["step"]] = rec.get("design", "")
        elif rec.get("event") == "REOPEN":
            for mid in rec.get("modules") or []:
                done.pop(mid, None)
    return done


def green_steps() -> set[str]:
    """すでに緑になったモジュールの id。"""
    return set(green_designs())


# --------------------------------------------------------------------------
# 段（RUNNER_SPEC 3 章）
# --------------------------------------------------------------------------
#
# 段は plan/stage.json に書く。どの段にいるかで、プランナーが書けるものと、
# ランナーが受け付けるコマンドが決まる。
#
#   stage      upstream | midstream | downstream | review
#   open       中段で、プランナーが書き直してよいモジュール。None なら全部。
#              [] は下流で止まった後で、人が直すモジュールを選ぶのを待っている
#   critiqued  クリティックが読んだ設計のハッシュ


def read_stage() -> dict:
    try:
        value = json.loads(STAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_stage(**changes) -> dict:
    stage = {**read_stage(), **changes}
    STAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STAGE_FILE.write_text(json.dumps(stage, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    STAGE_FILE.chmod(0o644)
    return stage


def use_stage_language(stage: dict) -> None:
    """段に記録した言語と持ち込む先を使う。"""
    if stage.get("language"):
        load_settings({"language": stage["language"], "framework": stage.get("framework")})


def refuse(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def in_stage(stage: dict, *allowed: str) -> str | None:
    """段が `allowed` のどれでもなければ、拒む理由。"""
    now = stage.get("stage")
    if now in allowed:
        return None
    return (f"refusing: this is for the {' or '.join(allowed)} stage, and the project is "
            f"{'in the ' + now + ' stage' if now else 'not started (run spec import)'}")


def tracked(rel: str) -> bool:
    return bool(run(["git", "ls-files", "--", rel]).stdout.strip())


def commit_paths(paths: list[Path], message: str, what: str) -> None:
    """`paths` の変更だけをコミットして送る。変更が無ければ何もしない。

    作業ツリーにほかの変更（止まったモジュールの試行）が残っていても、それは入れない。
    """
    rel = [p.relative_to(PROJECT).as_posix() for p in paths]
    rel = [r for r in rel if (PROJECT / r).exists() or tracked(r)]
    if not rel:
        return
    run(["git", "add", "-A", "--"] + rel, check=True)
    if run(["git", "diff", "--cached", "--quiet", "--"] + rel).returncode == 0:
        return
    run(["git", "commit", "-q", "-m", message, "--"] + rel, check=True)
    publish(what)


def write_texts(root: Path, files: dict[str, str], suffix: str,
                only: set[str] | None = None) -> None:
    """root の下を `files` にする。`only` があれば、その id のファイルだけを替える。"""
    root.mkdir(parents=True, exist_ok=True)
    current = read_texts(root, suffix)
    for key in sorted(set(current) | set(files)):
        if only is not None and key not in only:
            continue
        path = root / f"{key}{suffix}"
        if key in files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(files[key], encoding="utf-8")
            path.chmod(0o644)
        else:
            path.unlink(missing_ok=True)
    # 空になったディレクトリを、深いものから消す。
    for directory in sorted((Path(d) for d, _, _ in os.walk(root)), reverse=True):
        if directory != root and not any(directory.iterdir()):
            directory.rmdir()


def human_comment() -> str:
    try:
        return HUMAN_COMMENT.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def cmd_spec_import() -> int:
    """人が置いた仕様書を docs/spec/ に取り込む。最初の取り込みで上流を始める。"""
    found: dict[str, bytes] = {}
    if HUMAN_SPEC.is_dir():
        for directory, dirs, files in os.walk(HUMAN_SPEC):
            dirs.sort()
            for name in sorted(files):
                path = Path(directory) / name
                if path.is_file() and not path.is_symlink():
                    found[path.relative_to(HUMAN_SPEC).as_posix()] = path.read_bytes()
    if not found:
        return refuse(f"no specification at {HUMAN_SPEC}; put the files there "
                      f"(`loop go <file>` does)")
    if DOCS_SPEC.is_dir():
        shutil.rmtree(DOCS_SPEC)
    for rel, data in found.items():
        path = DOCS_SPEC / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o644)
    if not read_stage().get("stage"):
        write_stage(stage="upstream", open=None)
    ledger("SPEC_IMPORT", files=sorted(found))
    commit_paths([DOCS_SPEC, STAGE_FILE, LEDGER], "docs: import the specification",
                 "the specification")
    print(f"imported: {', '.join(sorted(found))}")
    return 0


def spec_text() -> str:
    """docs/spec/ の仕様書。文書でないファイルは名前だけを挙げる。"""
    parts = []
    if DOCS_SPEC.is_dir():
        for path in sorted(p for p in DOCS_SPEC.rglob("*") if p.is_file()):
            rel = path.relative_to(DOCS_SPEC).as_posix()
            if path.suffix in (".md", ".txt"):
                parts.append(f"--- {rel} ---\n{path.read_text(encoding='utf-8')}")
            else:
                parts.append(f"--- {rel} --- (not text; it is in docs/spec/)")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# プランナーの経路
# --------------------------------------------------------------------------
#
# プランナーは out/ の下の basic/、modules/、ac/ に書く。ランナーは呼ぶ前に
# 今の文書をそこへ写して置き、プランナーはそれを書き直す。どの段で何を置くかが、
# プランナーの書ける範囲だ。置かなかった部分に書いたものは、読まずに消す。
#
# 写しを置くディレクトリは runner の所有で、スティッキーにしない。プランナーの
# Write は一時ファイルからの置き換えで書くので、スティッキーなら runner の
# ファイルを置き換えられない。planner-run は umask 002 で動くので、プランナーが
# 作ったものもランナーが消せる。

PLANNER_PARTS = {"basic": ".md", "modules": ".md", "ac": ".json"}


def share_with_planner(path: Path, directory: bool) -> None:
    """プランナーが書き直せるように、グループ plannerw に開く。"""
    shutil.chown(path, group="plannerw")
    path.chmod(0o2770 if directory else 0o660)


def clear_planner_out() -> None:
    """out/ を空にする。前の呼び出しの残りを、今回の答えとして読まないためだ。"""
    PLANNER_OUT.mkdir(parents=True, exist_ok=True)
    for entry in PLANNER_OUT.iterdir():
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)   # シンボリックリンクはたどらずに消す
            else:
                entry.unlink()
        except OSError as error:
            raise Halt("PLAN", f"cannot clear {PLANNER_OUT}: {error}",
                       "planner-run must run with umask 002 so the runner can remove "
                       "what the planner wrote")


def prepare_planner_out(parts: dict[str, dict[str, str]]) -> None:
    """out/ を空にし、`parts` の部分ごとに今の文書を写して置く。"""
    clear_planner_out()
    for part, files in parts.items():
        root = PLANNER_OUT / part
        root.mkdir()
        share_with_planner(root, directory=True)
        for key, text in sorted(files.items()):
            path = root / f"{key}{PLANNER_PARTS[part]}"
            for parent in reversed(path.relative_to(root).parents[:-1]):
                if not (root / parent).is_dir():
                    (root / parent).mkdir()
                    share_with_planner(root / parent, directory=True)
            path.write_text(text, encoding="utf-8")
            share_with_planner(path, directory=False)


def prune_planner_out(parts: set[str]) -> list[str]:
    """out/ の中で、この呼び出しに置かなかったものと、拡張子の違うものを消す。

    プランナーは自分が書いたファイルを消せない。消せと言っても従えないので、
    ランナーが消し、次の呼び出しにそう伝える。
    """
    notes = []
    for entry in sorted(PLANNER_OUT.iterdir()):
        if entry.name in parts and entry.is_dir() and not entry.is_symlink():
            continue
        notes.append(f"{entry.name} is not something you may write here; the runner deleted it")
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    for part in sorted(parts):
        root = PLANNER_OUT / part
        for directory, dirs, files in os.walk(root):
            for name in sorted(dirs + files):
                path = Path(directory) / name
                rel = f"{part}/{path.relative_to(root).as_posix()}"
                if path.is_symlink():
                    notes.append(f"{rel} is a symbolic link; the runner deleted it")
                    path.unlink()
                    if name in dirs:
                        dirs.remove(name)
                elif name in files and path.suffix != PLANNER_PARTS[part]:
                    notes.append(f"{rel} is not a {PLANNER_PARTS[part]} file; the runner "
                                 f"deleted it")
                    path.unlink()
    return notes


def call_planner(brief: str) -> str:
    """プランナーにブリーフを1つ渡す。形も理由も call_solver と同じ。"""
    PLANNER_BRIEF.mkdir(parents=True, exist_ok=True)
    brief_path = PLANNER_BRIEF / "plan.md"
    brief_path.write_text(brief, encoding="utf-8")
    shutil.chown(brief_path, group="plannerw")
    brief_path.chmod(0o640)

    limit = TIMEOUTS["planner"]
    try:
        # 上限は前提にせず渡す。planner-run の既定は 900 秒なので、ここで決めて
        # 渡さない値は、決して効かない上限になる。
        proc = run_agent("planner", "PLAN", lambda: run(
            agent_command("planner", PLANNER_RUN, brief_path, limit),
            timeout=limit + BACKSTOP_MARGIN))
    except subprocess.TimeoutExpired:
        raise Halt("PLAN", f"planner still running after {limit + BACKSTOP_MARGIN}s",
                   "planner-run's internal timeout did not fire; check with: "
                   "pgrep -a -u planner")
    out = proc.stdout + proc.stderr
    if proc.returncode == 124:
        raise Halt("PLAN", "planner hit its own timeout in planner-run", out[-4000:])
    if proc.returncode != 0:
        raise Halt("PLAN", f"planner exited {proc.returncode}", out[-4000:])
    return out


def feedback_section(feedback: str) -> str:
    if not feedback:
        return ""
    return f"""
# Your previous attempt was rejected

The files you wrote are still in the current directory. Read them and FIX them
in place -- do not start over. The runner checked them and reported exactly
this:

{feedback}

Every line above is a rule the runner enforces in code. There is no arguing
with them and no partial credit: fix all of them, then stop.
"""


def comment_section(comment: str) -> str:
    if not comment:
        return ""
    return f"""
# What the human said about the last version

The human read the last version and wrote this. It is their decision, and you
do not get to overrule it:

{comment}
"""


def draft_with_retry(brief_for, check, parts: set[str], tag: str) -> list[str]:
    """プランナーを呼び、書いたものを確かめる。通れば空、上限まで落ちたら最後の問題。

    規則を述べる唯一の正式なものは、ランナーが持つ検査のコードだ。だからブリーフが
    規則を漏れなく説明したことに期待せず、検査を走らせ、何が落ちたかを伝える。
    ファイルは out/ に残し、プランナーは見えるものを直す。
    """
    feedback = ""
    for attempt in range(1, LIMITS["revisions"] + 2):
        call_planner(brief_for(feedback))
        notes = prune_planner_out(parts)
        problems = check()
        ledger(tag, attempt=attempt, problems=problems)
        if not problems:
            return []
        if attempt > LIMITS["revisions"]:
            return problems
        feedback = "\n".join(notes + problems)
    return []   # 届かない。ループは必ず戻る


def planner_reads_note() -> str:
    return layout_text(f"""
The code already in the repository is under {PROJECT}/{{SRC}}/. You may read it
with Read, Grep and Glob to learn what exists and how it is written. You cannot
write it, and you never see the tests.
""")


def brief_basic(spec: str, comment: str, feedback: str = "") -> str:
    module_files_note = " and ".join(f"`{s}`" for s in LANGUAGE["module_suffixes"])
    return f"""You are the planner. Write the basic design (基本設計仕様書) of the system the
specification below asks for.

You do not write code and you never will. A person reads what you write, and
decides whether it is right before anything else is designed.

# The specification, written by the human
{spec}
{planner_reads_note()}
{environment_facts(short=True)}
# What you write

The basic design is the files under `basic/` in the current directory. If any
are there, they are the current version: edit them. Otherwise create them,
e.g. `basic/design.md`. Write it in Japanese.

- Exactly one `{OVERVIEW_HEADING}` section across all the files. It says what the
  system is for and how it is put together, and no concrete values: no
  numbers, sizes, names of settings. Every module's solver receives this
  section, and a value written here would compete with the module's own design.
- The requirements you extracted from the specification, each stated so that a
  person can tell whether it is met.
- The design that meets them: the modules and what each is responsible for,
  and how they work together. A module is one source file under
  {LAYOUT["src"]}/ (its {module_files_note} file{"s" if len(LANGUAGE["module_suffixes"]) > 1 else ""}).
  A type with no behaviour of its own -- a struct, an enum, a class that only
  holds data -- is not a module; it belongs to the module that uses it, or to
  one shared types module.
- What this version will not do.

Write nothing outside `basic/`. Do not write the modules' detailed design yet;
that comes after the person approves this.
{comment_section(comment)}{feedback_section(feedback)}
Output nothing but the files. Do not restate the design in your final message.
"""


def brief_modules(spec: str, basic: str, open_: list[str] | None, comment: str,
                  feedback: str = "") -> str:
    suffixes = LANGUAGE["module_suffixes"]
    pairing = ("" if len(suffixes) == 1 else
               f" The {' and '.join(suffixes)} files of the same name are one module "
               f"with one design: `modules/logic/board.md` is "
               f"{' and '.join(f'`{{SRC}}/logic/board{s}`' for s in suffixes)}.")
    rollback = "" if open_ is None else f"""
# Only these modules may change

A module failed downstream, and the person chose the modules whose design is
wrong: {", ".join(open_)}.

Change only modules/<module>.md and ac/<module>.json for those. Every other
file under modules/ and ac/ must stay exactly as it is, and you may not add or
remove a module: the runner compares every other file with the approved design
and rejects any difference. The basic design does not change either.
"""
    return layout_text(f"""You are the planner. Split the basic design below into modules, and write the
detailed design (詳細設計仕様書) and the acceptance criteria of each module.

You do not write code. A separate agent writes each module, one at a time. It
cannot see this design as a whole: it receives the overview of the basic
design, its own module's design and acceptance criteria, and the contract and
behaviour of the modules it uses. Nothing else reaches it.

# The specification, written by the human
{spec}

# The basic design, approved by the human
{basic}
{planner_reads_note()}
{environment_facts(short=True)}{rollback}
# How the code is laid out

{LANGUAGE["layout_note"].strip()}

# What you write, into the current directory

For every module, two files. Those already here are the current version: edit
them in place.

- `modules/<path>.md` -- the module's detailed design. `<path>` is the source
  file's path under {{SRC}}/ without its extension: `{{SRC}}/logic/board{suffixes[0]}`
  is `modules/logic/board.md`.{pairing}
- `ac/<path>.json` -- the module's acceptance criteria.

Write nothing else. A file anywhere else is deleted without being read.

# The detailed design

Write it in Japanese, for a person to read. It has two sections:

    {BEHAVIOUR_HEADING}
    What the module does and why: its responsibility, its rules, what it does
    at the edges, which modules it uses and for what. Do not copy signatures
    here; they are in the contract.

    {CONTRACT_HEADING}
    Exactly one code block with the language `ndjson contract`, one JSON
    object per line, and nothing else that the runner reads.

A contract line is one of:

    {{"provides": "<signature>"}}
    {{"provides": "<signature>", "entry": true}}
    {{"requires": "<signature>", "from": "<module>"}}

- `provides` is what this module declares, one line per name.
- `requires` is what it uses from another module, copied exactly as that
  module's provides line has it. `from` is that module's path, e.g.
  `logic/board`. Code already under {{SRC}}/ that no module of this design
  rewrites is used the same way: `from` names its file without the extension.
- `"entry": true` marks a provides line that something outside this design
  calls: the program's starting point, what a framework calls, what existing
  code calls, a library's public API.

{LANGUAGE["contract_note"]}

# The acceptance criteria

    {{"criteria": [
      {{"case": "normal", "kind": "unit", "given": "...", "then": "..."}},
      ...
    ]}}

- `case` is "normal", "boundary" or "error", and every module has all three.
- `kind` is "unit", or "integration" for a criterion that runs this module
  together with the real modules it requires -- no stand-ins. Only a module
  that requires something may have integration criteria.
- Each criterion becomes exactly one test, written before the code.
- `then` states a concrete value on its own: a number, a quoted literal, an
  exception type, an empty collection ([] {{}} ()), or True / False / None.

# What the runner checks before the person sees it

    存在        every requires line names something its `from` module provides,
                or that the code already under {{SRC}}/ declares
    署名の一致  a requires line is identical to the provides line it names
    依存の循環  no module depends on itself through other modules
    未使用      every provides line is required by some module, or is "entry": true
    形          every container type in a provides line states its contents
                ({LANGUAGE["shape_example"]}); a bare one is rejected
    居場所      one name is provided by one module only

# Four things that are not obvious

1. **A contract line carries shape, not meaning.** The tests run first against
   a stub built from the provides lines. Anything in a provides line that says
   what the code DOES ends up in the stub, and a criterion the stub already
   satisfies stops the module.

2. **The next module is handed the contract line and nothing else.** A return
   type of a bare tuple or list cannot be taken apart by a caller who has only
   that line. Where the shape has meaning, a named type you provide beats a
   parameterised container.

3. **Every test must fail against the stub.** A criterion whose answer is a
   value the stub can produce -- an empty string, zero, an empty list, an
   argument returned unchanged -- or that compares two things the code under
   test produced, passes there and stops the module. Write the expected value
   out.

4. **No criterion checks the code's shape by reflection** -- a type's name, its
   base type, which members it has. The stub already has that shape.
{comment_section(comment)}{feedback_section(feedback)}
Output nothing but the files. Do not restate the design in your final message.
""")


def check_basic_out() -> list[str]:
    return basic_problems(read_texts(PLANNER_OUT / "basic", ".md"))


def check_modules_out(open_: list[str] | None, docs: dict[str, str],
                      acs: dict[str, str]) -> list[str]:
    """プランナーが書いた詳細設計を確かめる。差し戻しでは、指定外の変更を拒む。"""
    new_docs = read_texts(PLANNER_OUT / "modules", ".md")
    new_acs = read_texts(PLANNER_OUT / "ac", ".json")
    problems = []
    if open_ is not None:
        for name, before, after in (("modules", docs, new_docs), ("ac", acs, new_acs)):
            suffix = PLANNER_PARTS[name]
            for mid in sorted(set(before) | set(after)):
                if mid not in open_ and before.get(mid) != after.get(mid):
                    problems.append(f"差し戻し: {name}/{mid}{suffix} changed, but only "
                                    f"{', '.join(open_)} may change")
    modules, shape = design_problems(new_docs, new_acs)
    problems += shape
    if not shape:
        problems += interface_problems(modules)
    return problems


def cmd_basic_draft(language: str | None, framework: str | None) -> int:
    """仕様書から基本設計仕様書を書かせる。人の意見があれば渡す。"""
    stage = read_stage()
    refused = in_stage(stage, "upstream")
    if refused:
        return refuse(refused)
    if language or framework:
        language = language or stage.get("language") or "python"
        load_settings({"language": language, "framework": framework})
        stage = write_stage(language=language, framework=LANGUAGE["framework"])
    elif not stage.get("language"):
        return refuse("refusing: name the language the first time, with --language")
    use_stage_language(stage)
    spec = spec_text()
    if not spec:
        return refuse(f"refusing: there is no specification in {DOCS_SPEC}; run spec import")
    comment = human_comment()
    ledger("BASIC_DRAFT", language=LANGUAGE["name"], framework=LANGUAGE["framework"],
           comment=bool(comment))
    prepare_planner_out({"basic": read_texts(DOCS_BASIC, ".md")})
    problems = draft_with_retry(lambda feedback: brief_basic(spec, comment, feedback),
                                check_basic_out, {"basic"}, "BASIC_DRAFT_CHECK")
    if problems:
        for problem in problems:
            print("  " + problem, file=sys.stderr)
        print(f"the planner could not write an acceptable basic design; "
              f"{PLANNER_OUT} holds the last one", file=sys.stderr)
        return 2
    write_texts(DOCS_BASIC, read_texts(PLANNER_OUT / "basic", ".md"), ".md")
    HUMAN_COMMENT.unlink(missing_ok=True)
    ledger("BASIC_WRITTEN", files=sorted(read_texts(DOCS_BASIC, ".md")))
    commit_paths([DOCS_BASIC, STAGE_FILE, LEDGER], "docs: write the basic design",
                 "the basic design")
    clear_planner_out()
    print(f"basic design written to {DOCS_BASIC}; read it, then run basic approve, "
          f"or write a comment and run basic draft again")
    return 0


def cmd_basic_approve() -> int:
    """基本設計を承認して中段へ流す。"""
    stage = read_stage()
    refused = in_stage(stage, "upstream")
    if refused:
        return refuse(refused)
    problems = basic_problems(read_texts(DOCS_BASIC, ".md"))
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 2
    write_stage(stage="midstream", open=None, critiqued=None)
    ledger("BASIC_APPROVE")
    commit_paths([STAGE_FILE, LEDGER], "stage: approve the basic design", "the stage")
    print("basic design approved; next: modules draft")
    return 0


def interface_record(modules: dict[str, dict], problems: list[str]) -> dict:
    """INTERFACE.json に書く中身。ダッシュボードが依存の図と入口の行を描く。"""
    return {"digest": design_digest(modules), "problems": problems,
            "order": module_order(modules)[0], "depends_on": module_deps(modules),
            "entries": {mid: [p["sig"] for p in m["provides"] if p["entry"]]
                        for mid, m in sorted(modules.items())
                        if any(p["entry"] for p in m["provides"])}}


def cmd_modules_draft() -> int:
    """基本設計からモジュールの詳細設計と受け入れ条件を書かせ、クリティックに1回読ませる。"""
    stage = read_stage()
    refused = in_stage(stage, "midstream")
    if refused:
        return refuse(refused)
    open_ = stage.get("open")
    if open_ == []:
        return refuse("refusing: a module failed downstream; choose the modules whose "
                      "design is wrong with `reopen <module>...` first")
    use_stage_language(stage)
    spec = spec_text()
    basic = "\n\n".join(f"--- basic/{name}.md ---\n{text}"
                        for name, text in read_texts(DOCS_BASIC, ".md").items())
    docs, acs = read_texts(DOCS_MODULES, ".md"), read_texts(AC, ".json")
    comment = human_comment()
    ledger("MODULES_DRAFT", open=open_, comment=bool(comment))
    prepare_planner_out({"modules": docs, "ac": acs})
    problems = draft_with_retry(
        lambda feedback: brief_modules(spec, basic, open_, comment, feedback),
        lambda: check_modules_out(open_, docs, acs), {"modules", "ac"}, "MODULES_DRAFT_CHECK")
    if problems:
        for problem in problems:
            print("  " + problem, file=sys.stderr)
        print(f"the planner could not write an acceptable module design; "
              f"{PLANNER_OUT} holds the last one", file=sys.stderr)
        return 2
    only = None if open_ is None else set(open_)
    write_texts(DOCS_MODULES, read_texts(PLANNER_OUT / "modules", ".md"), ".md", only)
    write_texts(AC, read_texts(PLANNER_OUT / "ac", ".json"), ".json", only)
    modules, _ = current_design()
    INTERFACE.write_text(json.dumps(interface_record(modules, []), ensure_ascii=False,
                                    indent=2) + "\n", encoding="utf-8")
    INTERFACE.chmod(0o644)
    HUMAN_COMMENT.unlink(missing_ok=True)
    write_stage(critiqued=None)
    ledger("MODULES_WRITTEN", modules=sorted(modules))
    commit_paths([DOCS_MODULES, INTERFACE, STAGE_FILE, LEDGER],
                 "docs: write the module design", "the module design")
    clear_planner_out()
    return cmd_modules_critique()


# --------------------------------------------------------------------------
# クリティック
# --------------------------------------------------------------------------
#
# クリティックは詳細設計を1回だけ読み、おかしな点を言う。指摘でプランナーを
# 呼び直すことはしない。呼び直すかは人が決める。受け入れ条件もコードも見せない。
# 実装の合否は VERIFY が決める。


def brief_critique(spec: str, basic: str, modules: dict[str, dict]) -> str:
    designs = "\n\n".join(f"--- modules/{mid}.md ---\n{m['doc']}"
                          for mid, m in sorted(modules.items()))
    return f"""Below are a specification, the basic design made from it, and the detailed
design of every module. Your role and your task follow after them.

# The specification, written by the human
{spec}

# The basic design
{basic}

# The module designs
{designs}

# Your role

You are the critic. You review the module design once. Nothing has been built
yet. A separate agent will build each module from its design alone, tests
first, and a machine has already checked that the contracts fit together.

# What to look for

- **Coverage.** If every module did exactly what its design says, would the
  specification be met? Name anything the specification asks for that no
  module does.
- **Reach.** Start from the entry points (`"entry": true`). What state does the
  system hold before anything happens, what can a person using it actually do,
  and what can they reach from there? Name behaviour that some module carries
  but that nothing reachable ever calls, and anything the system reads that no
  module ever writes.
- **Boundaries.** Responsibilities split so that a module needs something its
  contract does not hand it, or two modules that each assume the other does a
  job.

You see neither the acceptance criteria nor any code. Do not guess at them.

{findings_contract()}"""


def clear_critique() -> None:
    """批評を頼む前に out/ を空にする。

    前の呼び出しの残りのファイルは、今回の答えとして読まれる。「クリティックは
    何も見つけなかった」と「クリティックは走らなかった」は、決して同じに見えては
    ならない。
    """
    CRITIC_OUT.mkdir(parents=True, exist_ok=True)
    for entry in CRITIC_OUT.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            raise Halt("CRITIQUE",
                       f"a directory is in the way in {CRITIC_OUT}: {entry.name}",
                       "Remove it by hand; the runner will not recurse into a "
                       "tree written by another uid.")
        entry.unlink()


def cmd_modules_critique() -> int:
    """いまの詳細設計をクリティックに1回読ませ、指摘を人に渡す。"""
    stage = read_stage()
    refused = in_stage(stage, "midstream")
    if refused:
        return refuse(refused)
    use_stage_language(stage)
    modules, problems = current_design()
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 2
    digest = design_digest(modules)
    basic = "\n\n".join(f"--- basic/{name}.md ---\n{text}"
                        for name, text in read_texts(DOCS_BASIC, ".md").items())
    ledger("CRITIQUE", digest=digest)
    clear_critique()
    call_critic(brief_critique(spec_text(), basic, modules), "modules")
    findings = plain_findings({"modules": read_findings()})
    ledger("FINDINGS", count=len(findings["modules"]),
           titles=[f["title"][:200] for f in findings["modules"]])
    CRITIQUE.write_text(json.dumps({"digest": digest, "findings": findings["modules"]},
                                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    CRITIQUE.chmod(0o644)
    write_critique_for_human(1, findings, waiting=False)
    write_stage(critiqued=digest)
    commit_paths([CRITIQUE, STAGE_FILE, LEDGER], "plan: critique the module design",
                 "the critique")
    # 行頭の `=== critique ` は、loop_now.py が走行ログから指摘を探す目印。
    print("=== critique of the module design ===")
    print(render_findings(findings))
    print("next: modules approve, or write a comment and run modules draft again")
    return 0


# --------------------------------------------------------------------------
# 中段の出口と、差し戻し
# --------------------------------------------------------------------------


def discard_modules(mids: list[str], tasks: dict | None) -> None:
    """緑を取り消すモジュールのテストと、凍結のマニフェストと契約を消す。

    テストはコミットしないので、git の巻き戻しでは消えない。ここで消さないと、
    古い設計のテストが VERIFY のたびに走り続ける。
    """
    steps = {s["id"]: s for s in (tasks or {}).get("steps", [])}
    files = sorted({f for mid in mids
                    for f in steps.get(mid, {}).get("files_test", module_test_files(mid, True))})
    if TESTS.is_dir():
        adopt(TESTS)
        set_writable(tests=True, src=None)
    restore_to_head(files)
    for mid in mids:
        for kind in ("contracts", "freeze"):
            (STATE / kind / f"{safe_id(mid)}.json").unlink(missing_ok=True)


def cmd_modules_approve() -> int:
    """詳細設計を承認して下流へ流す。設計が変わった緑のモジュールは流し直す。"""
    stage = read_stage()
    refused = in_stage(stage, "midstream")
    if refused:
        return refuse(refused)
    if stage.get("open") == []:
        return refuse("refusing: a module failed downstream; choose the modules whose "
                      "design is wrong with `reopen <module>...` first")
    use_stage_language(stage)
    modules, problems = current_design()
    if not problems:
        problems = interface_problems(modules)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 2
    digest = design_digest(modules)
    if stage.get("critiqued") != digest:
        return refuse("refusing: the critic has not read this design; run modules critique")

    try:
        before = json.loads(TASKS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        before = None
    changed = sorted(mid for mid, design in green_designs().items()
                     if modules.get(mid, {}).get("design") != design)
    if changed:
        discard_modules(changed, before)
        ledger("REOPEN", modules=changed, note="the design changed; these run again")
    tasks = build_tasks(modules, stage)
    TASKS.parent.mkdir(parents=True, exist_ok=True)
    TASKS.write_text(json.dumps(tasks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    INTERFACE.write_text(json.dumps(interface_record(modules, []), ensure_ascii=False,
                                    indent=2) + "\n", encoding="utf-8")
    INTERFACE.chmod(0o644)
    FAILURE.unlink(missing_ok=True)
    write_stage(stage="downstream", open=None)
    ledger("MODULES_APPROVE", digest=digest, order=[s["id"] for s in tasks["steps"]])
    commit_paths([STAGE_FILE, INTERFACE, FAILURE, LEDGER], "stage: approve the module design",
                 "the stage")
    print(f"module design approved; next: run --all "
          f"({len(tasks['steps']) - len(green_steps())} module(s) to build)")
    return 0


def clean_tree() -> None:
    """作業ツリーを最後のコミットに戻し、緑でないモジュールのテストを消す。"""
    for fence in (TESTS, SRC):
        fence.mkdir(parents=True, exist_ok=True)
    adopt(TESTS, SRC)
    set_writable(tests=True, src=True)
    kept = LEDGER.read_bytes() if LEDGER.exists() else b""
    run(["git", "reset", "--hard", "HEAD"], check=True)
    run(["git", "clean", "-fdq"], check=True)   # -x は付けない。.venv と .runner は残す
    for fence in (TESTS, SRC):
        fence.mkdir(parents=True, exist_ok=True)
    set_writable(tests=True, src=True)
    if kept:
        LEDGER.write_bytes(kept)
    try:
        tasks = json.loads(TASKS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    done = green_steps()
    pending = [s for s in tasks.get("steps", []) if s.get("id") not in done]
    restore_to_head(sorted({f for s in pending for f in s.get("files_test", [])}))
    for s in pending:
        (STATE / "freeze" / f"{safe_id(s['id'])}.json").unlink(missing_ok=True)


def cmd_reopen(mids: list[str]) -> int:
    """詳細設計を直すモジュールを人が選び、中段に戻す。"""
    stage = read_stage()
    refused = in_stage(stage, "midstream", "downstream", "review")
    if refused:
        return refuse(refused)
    known = set(read_texts(DOCS_MODULES, ".md"))
    unknown = sorted(set(mids) - known)
    if unknown or not mids:
        return refuse(f"refusing: not a module of this design: {', '.join(unknown) or '(none)'}; "
                      f"the modules are {', '.join(sorted(known))}")
    clean_tree()
    write_stage(stage="midstream", open=sorted(set(mids)), critiqued=stage.get("critiqued"))
    ledger("SENT_BACK", modules=sorted(set(mids)))
    commit_paths([STAGE_FILE, LEDGER], f"stage: send {', '.join(sorted(set(mids)))} back "
                 f"to the module design", "the stage")
    print(f"back in the midstream; the planner may change {', '.join(sorted(set(mids)))}. "
          f"next: modules draft, or modules approve to run them again as they are")
    return 0


def failed_modules(step: dict, run_: TestRun | None) -> list[str]:
    """止まったモジュールと、テストが落ちたモジュール。"""
    involved = [step["id"]]
    if run_ is None:
        return involved
    try:
        tasks = json.loads(TASKS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return involved
    for other in tasks.get("steps", []):
        keys = own_test_keys(other)
        if other["id"] not in involved and any(test_owner(f) in keys for f in run_.failed_files):
            involved.append(other["id"])
    return involved


def fail_module(step: dict, halt: Halt, attempt: int, run_: TestRun | None) -> None:
    """下流で止まったことを人に渡し、中段に戻す。直すモジュールは人が選ぶ。"""
    involved = failed_modules(step, run_)
    failed = "\n".join(f"- {k}" for k in (run_.failure_kinds if run_ else [])) \
        or "(記録なし)"
    FAILURE.write_text(f"""# 下流で止まったモジュール: {step["id"]}

- 位相: {halt.phase}
- 理由: {halt.reason}
- 試行: {attempt} / {len(attempt_schedule(step))}
- 関わったモジュール: {", ".join(involved)}

## 落ちたテスト

{failed}

## 詳細

```text
{halt.detail[-4000:]}
```

## 次にすること

詳細設計を直すモジュールを `loop reopen <モジュール>...` で選ぶ。複数選べる。
""", encoding="utf-8")
    FAILURE.chmod(0o644)
    write_stage(stage="midstream", open=[])
    ledger("FAILED", step=step["id"], phase=halt.phase, reason=halt.reason, modules=involved)
    commit_paths([FAILURE, STAGE_FILE, LEDGER], f"run: {step['id']} failed in {halt.phase}",
                 "the failure")
    print(f"\nFAILED: {step['id']} in {halt.phase}; read {FAILURE}", file=sys.stderr)


def cmd_check() -> int:
    """いまの設計を確かめる。何も呼ばない。"""
    stage = read_stage()
    use_stage_language(stage)
    problems = basic_problems(read_texts(DOCS_BASIC, ".md"))
    if stage.get("stage") not in (None, "upstream"):
        modules, shape = current_design()
        problems += shape if shape else interface_problems(modules)
    for problem in problems:
        print(problem)
    print(f"\n{len(problems)} problem(s)" if problems else "the design passes every check")
    return 1 if problems else 0


def cmd_status() -> int:
    """段と、モジュールごとの状態。"""
    stage = read_stage()
    print(f"stage: {stage.get('stage') or '(not started)'}")
    if stage.get("open"):
        print(f"open for the planner: {', '.join(stage['open'])}")
    elif stage.get("open") == []:
        print("waiting: choose the modules to fix with reopen")
    for row in steps_now():
        print(f"  {row['state']:8} {row['id']}")
    return 0


def python_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"{prefix} {node.name}({ast.unparse(node.args)}){returns}"


def python_declarations(text: str, module: str) -> list[str]:
    """Python のモジュールの公開の宣言を、契約と同じ書式で1行ずつ返す。

    本体は返さない。プランナーは条件を書く側で、実装を見て書いた条件は要件では
    なく実装を述べる（BOOTSTRAP 1-1）。クラスは、呼び出し側が使うフィールドと
    メソッドの署名を1行にまとめる。declared_name がクラスの名前を拾えるよう、
    `class` を行の先に置く。構文の壊れたファイルは読めないので、何も返さない。
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    where = f" -- defined in {module}"
    lines = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                lines.append(python_signature(node) + where)
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            decorators = "".join(f"@{ast.unparse(d)} " for d in node.decorator_list)
            bases = [ast.unparse(b) for b in node.bases + node.keywords]
            head = f"{decorators}class {node.name}" + (f"({', '.join(bases)})" if bases else "")
            members = []
            for member in node.body:
                if isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name):
                    if not member.target.id.startswith("_"):
                        members.append(f"{member.target.id}: {ast.unparse(member.annotation)}")
                elif isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if member.name == "__init__" or not member.name.startswith("_"):
                        members.append(python_signature(member))
            lines.append(head + (": " + "; ".join(members) if members else "") + where)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if not node.target.id.startswith("_"):
                lines.append(f"{node.target.id}: {ast.unparse(node.annotation)}{where}")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    lines.append(target.id + where)
    return lines


def ts_statements(text: str) -> list[str]:
    """TypeScript の最上位の文を、コメントを除いて順に返す。"""
    return [statement for _, _, statement in ts_statement_spans(text)]


def ts_statement_spans(text: str) -> list[tuple[int, int, str]]:
    """TypeScript の最上位の文を、元の文字列での範囲とともに順に返す。

    範囲は (始まり, 終わり) で、`text[始まり:終わり]` がその文になる。前に付いた
    コメントは含まない。文の中身の方はコメントを除いてある。

    箱には TypeScript のパーサが無い（vitest は esbuild で型を捨てるだけだ）。
    だから宣言の切れ目だけを数える。文字列、コメント、正規表現のリテラルの中の
    括弧は数えない。文は、深さ 0 の `;`、本体を閉じる `}`、続きの無い改行で
    終わる。

    `}` で終わるのは本体の `{` だけだ。`: {a: number}` のような型や、`=` の後の
    値の `{` では終わらない。そうしないと、戻り値の型がオブジェクトの関数が、
    型の所で切れる。
    """
    statements: list[tuple[int, int, str]] = []
    buf: list[str] = []
    depth = 0
    assigned = False   # 深さ 0 に `=` があった。その後の `{` は値で、本体ではない
    body = False       # 深さ 0 で開いた `{` が本体か
    last = ""          # 直前の空白でない文字
    start = stop = 0   # いまの文の最初の文字と、最後の空白でない文字の次
    started = False    # いまの文に空白でないものがもう入ったか
    i, n = 0, len(text)

    def end() -> None:
        nonlocal assigned, body, started
        statement = "".join(buf).strip()
        if statement:
            statements.append((start, stop, statement))
        buf.clear()
        assigned = body = started = False

    def mark(first: int, after: int) -> None:
        """空白でないものを buf に足したときに、文の範囲を広げる。"""
        nonlocal start, stop, started
        if not started:
            start, started = first, True
        stop = after

    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            buf.append(" ")
            continue
        # `/` は、値が来る位置なら正規表現、そうでなければ割り算。正規表現の中の
        # 引用符を文字列の始まりと読むと、ファイルの残りが1つの文字列になる。
        if c in "'\"`" or (c == "/" and (last == "" or last in "(,=:[!&|?{};")):
            j, in_class = i + 1, False
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if c == "/" and text[j] == "\n":
                    break
                if c == "/" and text[j] == "[":
                    in_class = True
                elif c == "/" and text[j] == "]":
                    in_class = False
                elif text[j] == c and not in_class:
                    break
                j += 1
            mark(i, min(j + 1, n))
            buf.append(text[i:j + 1])
            last, i = c, j + 1
            continue
        if c in "({[":
            if c == "{" and depth == 0:
                body = not assigned and last not in ":|&<,=("
            depth += 1
        elif c in ")}]":
            depth = max(depth - 1, 0)
        elif (c == "=" and depth == 0 and nxt not in "=>"
              and text[i - 1:i] not in ("=", "!", "<", ">")):
            assigned = True
        if not c.isspace():
            mark(i, i + 1)
        buf.append(c)
        if c == "}" and depth == 0 and body:
            end()
        elif c == ";" and depth == 0:
            end()
        elif c == "\n" and depth == 0 and started:
            ahead = text[i + 1:i + 200].lstrip()[:1]
            if last not in "=,(+-*/&|?:<>.[{" \
                    and (not ahead or ahead not in ".?:|&=,)>"):
                end()
        if not c.isspace():
            last = c
        i += 1
    end()
    return statements


def ts_top_level(text: str, token: str) -> int | None:
    """括弧の外にある `token` の位置。`=` は `==` `=>` `<=` `>=` `!=` を除く。"""
    depth = 0
    for i, c in enumerate(text):
        if c in "({[":
            depth += 1
        elif c in ")}]":
            depth -= 1
        elif depth == 0 and text.startswith(token, i):
            if token == "=" and (text[i + 1:i + 2] in ("=", ">")
                                 or text[i - 1:i] in ("=", "!", "<", ">")):
                continue
            return i
    return None


def strip_body(text: str) -> str:
    """末尾の `{ ... }` を除く。本体の無い宣言はそのまま返す。"""
    if not text.endswith("}"):
        return text
    depth = 0
    for i in range(len(text) - 1, -1, -1):
        if text[i] == "}":
            depth += 1
        elif text[i] == "{":
            depth -= 1
            if depth == 0:
                return text[:i].rstrip()
    return text


TS_EXPORT = re.compile(
    r"^export\s+(?:declare\s+)?(?:default\s+)?"
    r"(?P<kind>(?:abstract\s+)?class|(?:async\s+)?function\*?|interface|type|enum|const|let|var)"
    r"\s+[A-Za-z_$][\w$]*")
TS_HIDDEN = re.compile(r"^(?:#|(?:(?:static|readonly|abstract|override|async)\s+)*(?:private|protected)\b)")


def ts_class(raw: str, head: str) -> str:
    """クラスの頭と、公開のメンバーの署名。private と protected と # は外す。"""
    body = raw[raw.index("{") + 1:raw.rindex("}")]
    members = []
    for member in ts_statements(body):
        m = " ".join(member.split()).rstrip(";").rstrip()
        if not m or TS_HIDDEN.match(m):
            continue
        m = re.sub(r"^public\s+", "", m)
        eq, paren = ts_top_level(m, "="), m.find("(")
        if eq is not None and (paren < 0 or eq < paren):
            m = m[:eq].rstrip()        # フィールド。初期値は実装なので外す
        else:
            m = strip_body(m)          # メソッド
        members.append(m)
    return f"{head} {{ {'; '.join(members)} }}" if members else head


def ts_declarations(text: str, path: str) -> list[str]:
    """TypeScript のファイルの export を、契約と同じ書式で1行ずつ返す。

    関数は本体を外す。interface、type、enum は形そのものなので全部を残す。const
    は型の注釈まで。値が関数なら、署名と `=>` まで。クラスは公開のメンバーの
    署名だけ。`export { a } from` のような再 export は、宣言している元の
    ファイルの方で拾う。
    """
    lines = []
    for statement in ts_statements(text):
        raw = statement.strip().rstrip(";").rstrip()
        flat = " ".join(raw.split())
        match = TS_EXPORT.match(flat)
        if not match:
            continue
        decl = re.sub(r"^export\s+(?:declare\s+)?(?:default\s+)?", "", flat)
        kind = match.group("kind").split()[-1]
        if kind.startswith("function"):
            decl = strip_body(decl)
        elif kind in ("const", "let", "var"):
            eq = ts_top_level(decl, "=")
            if eq is not None:
                value = decl[eq + 1:].strip()
                arrow = ts_top_level(value, "=>")
                if arrow is not None and re.match(r"(?:async\s*)?[(<]", value):
                    decl = f"{decl[:eq].rstrip()} = {value[:arrow + 2]}"
                else:
                    decl = decl[:eq].rstrip()
        elif kind == "class" and "{" in raw:
            decl = ts_class(raw, decl[:decl.index("{")].rstrip())
        lines.append(f"{path}: {decl}")
    return lines


def existing_contracts() -> list[str]:
    """最後のコミットの柵の中（LAYOUT["src"]）にある公開の宣言。計画の言語の
    ファイルだけを読む。
    """
    return [line for lines in existing_declarations().values() for line in lines]


def existing_declarations() -> dict[str, list[str]]:
    """existing_contracts の宣言を、それを持つファイルのパスごとに分けたもの。

    作業ツリーではなく HEAD から読む。ステップの途中の書きかけやスタブを、
    すでにあるコードとして見せないためだ。
    """
    try:
        listing = run(["git", "ls-tree", "-r", "-z", "--name-only", "HEAD", "--",
                       LAYOUT["src"]])
    except OSError:
        return {}   # 作業ツリーがまだ無い
    if listing.returncode != 0:
        return {}   # コミットがまだ無い
    suffix = LANGUAGE["source_suffix"]
    by_file: dict[str, list[str]] = {}
    for path in listing.stdout.split("\0"):
        if not in_layout(path, "src") or not path.endswith(suffix):
            continue
        shown = run(["git", "show", f"HEAD:{path}"])
        if shown.returncode != 0:
            continue
        lines: list[str] = []
        if LANGUAGE["name"] == "typescript":
            lines = ts_declarations(shown.stdout, path)
        elif LANGUAGE["name"] == "python":
            modules = modules_of([path])
            if modules:
                lines = python_declarations(shown.stdout, modules[0])
        elif LANGUAGE["name"] == "csharp":
            lines = csharp_declarations(shown.stdout, path)
        if lines:
            by_file[path] = lines
    return by_file


# テストの名前の拾い方。プランナーはテストの本体を読めないので、どのファイルが
# 何を確かめているかを名前で伝える。
TEST_NAMES = {
    "python": re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+(test\w*)[ \t]*\(", re.M),
    "typescript": re.compile(
        r"\b(?:it|test)(?:\.\w+)?(?:\([^()]*\))?\(\s*(['\"`])((?:\\.|(?!\1).)*)\1"),
    "csharp": re.compile(
        r"\[\s*(?:NUnit\.Framework\.)?(?:Test|TestCase|TestCaseSource|UnityTest)\b[^\]]*\]"
        r"(?:\s*\[[^\]]*\])*\s*(?:(?:public|private|internal|protected|static|async)\s+)*"
        r"[\w<>\[\],.]+\s+(\w+)\s*\("),
    "cpp": re.compile(r"\b(?:TEST|TEST_F|TEST_P)\s*\(\s*\w+\s*,\s*(\w+)\s*\)"),
}


def test_names(text: str) -> list[str]:
    """テストのファイルが持つテストの名前。出てきた順で、重複は1つにする。"""
    names = []
    for match in TEST_NAMES[LANGUAGE["name"]].finditer(text):
        names.append(match.group(match.lastindex))
    return list(dict.fromkeys(names))


def existing_tests() -> dict[str, list[str]]:
    """HEAD にあるテストのファイルと、それぞれのテストの名前。

    テストを1つも見つけられないファイル（`__init__.py` や共通の道具）は入れない。
    モジュールのテストはコミットしないので、ここに入るのは設計より前からある
    テストだけだ。
    """
    try:
        listing = run(["git", "ls-tree", "-r", "-z", "--name-only", "HEAD", "--",
                       LAYOUT["tests"]])
    except OSError:
        return {}
    if listing.returncode != 0:
        return {}
    by_file: dict[str, list[str]] = {}
    for path in listing.stdout.split("\0"):
        if not in_layout(path, "tests") or Path(path).suffix not in LANGUAGE["test_suffixes"]:
            continue
        shown = run(["git", "show", f"HEAD:{path}"])
        if shown.returncode != 0:
            continue
        names = test_names(shown.stdout)
        if names:
            by_file[path] = names
    return by_file


# 1つのファイルについて見せるテストの名前の数。
TEST_NAMES_SHOWN = 30


def existing_tests_text() -> str:
    """設計より前からあるテストの、ファイルと名前。無ければ空。

    既存のテストは毎モジュール走り、落ちればモジュールは緑にならない。仕様が
    振る舞いを変えるなら、それを確かめる既存のテストは、どんな実装でも落ちる。
    プランナーがそれを知るために、名前だけを見せる。
    """
    by_file = existing_tests()
    if not by_file:
        return ""

    def names(found: list[str]) -> str:
        shown = [f"        {n}" for n in found[:TEST_NAMES_SHOWN]]
        if len(found) > TEST_NAMES_SHOWN:
            shown.append(f"        ... and {len(found) - TEST_NAMES_SHOWN} more")
        return "\n".join(shown)

    listing = "\n".join(f"    {f}\n{names(found)}" for f, found in by_file.items())
    return f"""
# Tests already in the repository

These test files are in the last commit, written before this design. The runner
runs every one of them on every module, and a module that makes one of them fail
is not green. Only the test names are shown, not the bodies.

{listing}

A design that changes behaviour one of these tests checks cannot go green while
that test stands. Say so in the basic design, so the person can remove or
change the test before the modules are built.
"""


def head_sources(paths: list[str]) -> dict[str, str]:
    """`paths` のうち、最後のコミットにあるファイルの中身。無いものは入れない。"""
    sources = {}
    for path in paths:
        shown = run(["git", "show", f"HEAD:{path}"])
        if shown.returncode == 0:
            sources[path] = shown.stdout
    return sources


def restore_to_head(paths: list[str]) -> None:
    """`paths` を最後のコミットの中身に戻す。コミットに無いものは消す。

    `git checkout -- <paths>` は、コミットに無いパスが1つでも混ざると全体を断り、
    ほかのパスも戻さない。取り込んだ Unity のプロジェクトの S1 では、既存のコードの
    スタブを消したまま TEST_WRITE をやり直し、消えたファイルを柵の外への書き込みと
    読んで止まった。だから、コミットにあるものだけを渡す。
    """
    present = sorted(head_sources(paths))
    for path in paths:
        (PROJECT / path).unlink(missing_ok=True)
    if present:
        run(["git", "checkout", "--"] + present, check=True)


# export の有無を問わない。provides は export を求めるが、既存のファイルでは
# export していない同じ名前の宣言を差し替えることもある。
TS_DECLARED = re.compile(
    r"^(?:export\s+)?(?:declare\s+)?(?:default\s+)?(?:abstract\s+)?(?:async\s+)?"
    r"(?:function\*?|class|interface|type|enum|const|let|var)\s+([A-Za-z_$][\w$]*)")


def ts_declared_names(flat: str) -> list[str]:
    """1つの文が宣言する名前すべて。`const a = 1, b = 2` なら a と b。"""
    match = TS_DECLARED.match(flat)
    if not match:
        return []
    names = [match.group(1)]
    if re.match(r"(?:export\s+)?(?:declare\s+)?(?:const|let|var)\b", flat):
        depth = 0
        for i, c in enumerate(flat):
            if c in "({[":
                depth += 1
            elif c in ")}]":
                depth -= 1
            elif c == "," and depth == 0:
                more = re.match(r"\s*([A-Za-z_$][\w$]*)\s*[:=]", flat[i + 1:])
                if more:
                    names.append(more.group(1))
    return names


def ts_merge_stub(original: str, pieces: dict[str, str]) -> tuple[str, str] | None:
    """既存のファイルの、名前ごとの宣言だけをスタブの宣言に差し替える。

    返すのは (差し替えた全文, 差し替えなかった部分)。後者は、import を足すか
    どうかを決めるのに使う。ファイルにまだ無い名前は末尾に足す。1つの文が2つ
    以上の名前を宣言している（`export const a = 1, b = 2`）なら、1つだけを
    差し替えると残りが消えるので None を返し、ソルバーに回す。
    """
    spans: dict[str, tuple[int, int]] = {}
    for start, stop, statement in ts_statement_spans(original):
        names = ts_declared_names(" ".join(statement.split()))
        if not any(name in pieces for name in names):
            continue
        if len(names) > 1:
            return None
        if names[0] not in spans:
            spans[names[0]] = (start, stop)
    text, rest = original, original
    for name, (start, stop) in sorted(spans.items(), key=lambda kv: -kv[1][0]):
        text = text[:start] + pieces[name] + text[stop:]
        rest = rest[:start] + rest[stop:]
    added = [piece for name, piece in pieces.items() if name not in spans]
    if added:
        text = text.rstrip() + chr(10) * 2 + chr(10).join(added) + chr(10)
    return text, rest


def top_level_units(text: str, path: str, provided: set[str]) -> dict[str, str] | None:
    """ファイルの最上位の文のうち、`provided` の名前を宣言しないものの一覧。

    キーは比べるための形、値は人に見せる1行目。import は入れない。スタブは新しい
    型のために import を足すことがあり、足りない import はコンパイルで必ず分かる。
    Python は ast.dump で比べるので、空白とコメントの違いは数えない。読めなければ
    None。C# は型の頭とメンバーを単位にする（csharp_units）。
    """
    if path.endswith(".cs"):
        return csharp_units(text, provided)
    if path.endswith((".h", ".hpp", *CPP_SOURCE_SUFFIXES)):
        # C++ の既存コードはまだ読まない。比べるものが無いので、何も止めない。
        return {}
    units: dict[str, str] = {}
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return None
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names = {node.name}
            elif isinstance(node, ast.Assign):
                names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = {node.target.id}
            else:
                names = set()
            if names and names <= provided:
                continue
            units[ast.dump(node)] = ast.unparse(node).splitlines()[0][:100]
        return units
    for _, _, statement in ts_statement_spans(text):
        # 末尾の `;` は、あっても無くても同じ文だ。
        flat = " ".join(statement.split()).rstrip(";").rstrip()
        if flat.startswith("import "):
            continue
        # 名前の一部だけを提供する文は、差し替えれば必ず変わるので比べる側に残す。
        names = ts_declared_names(flat)
        if names and set(names) <= provided:
            continue
        units[flat] = flat[:100]
    return units


def stub_kept_the_rest(step: dict, originals: dict[str, str]) -> list[str]:
    """既存のファイルで、スタブが provides の名前のほかを変えていないこと。

    スタブが変えてよいのは、このステップが提供する名前の宣言だけだ。ほかの関数を
    消せば、それを使う既存のテストが VERIFY で落ちる。そのときには、どの位相の
    誰が消したのかがもう分からない。だからここで、書いた直後に比べる。
    """
    provided = {declared_name(p) for p in step["contracts"]["provides"]} \
        | csharp_listed_members(step["contracts"]["provides"])
    problems = []
    for path, before in sorted(originals.items()):
        target = PROJECT / path
        after = target.read_text(encoding="utf-8") if target.is_file() else ""
        kept_before = top_level_units(before, path, provided) or {}
        kept_after = top_level_units(after, path, provided)
        if kept_after is None:
            problems.append(f"{path}: no longer parses")
            continue
        for key, label in kept_before.items():
            if key not in kept_after:
                problems.append(f"{path}: changed or removed: {label}")
    return problems


# --------------------------------------------------------------------------
# C# の既存コードを読む
#
# 箱には C# のパーサが無い（Roslyn を呼ぶには、ビルドを1回待つ）。TypeScript と
# 同じく、宣言の切れ目だけを数える。C# では宣言が型の中にあるので、単位は型と
# そのメンバーだ。
# --------------------------------------------------------------------------

# テストのビルドで常に定義されるシンボル。dotnet test は Debug でビルドする。
CSHARP_BASE_DEFINES = frozenset({"DEBUG", "TRACE", "NETSTANDARD", "NETSTANDARD2_1",
                                 "NETSTANDARD2_0_OR_GREATER", "NETSTANDARD2_1_OR_GREATER"})
CSHARP_MODIFIER_WORDS = frozenset({
    "public", "private", "protected", "internal", "static", "virtual", "override",
    "abstract", "sealed", "readonly", "extern", "unsafe", "new", "async", "partial",
    "const", "volatile", "event", "implicit", "explicit"})
CSHARP_TYPE_HEAD = re.compile(
    r"^(?P<mods>(?:(?:" + "|".join(sorted(CSHARP_MODIFIER_WORDS | {"ref"})) + r")\s+)*)"
    r"(?P<kind>class|struct|interface|enum|record)\s+@?(?P<name>[A-Za-z_]\w*)(?P<rest>.*)$")
CSHARP_DIRECTIVE = re.compile(r"[ \t]*#[ \t]*(\w+)[ \t]*([^\n]*)")
CSHARP_NAME_BEFORE = re.compile(r"(~?[A-Za-z_]\w*)\s*(<[^()]*>)?\s*$")


def csharp_defines() -> set[str]:
    """テストのビルドで定義されるシンボル。Unity の参照があれば、その定義も足す
    （dotnet_projects と同じもの）。"""
    defines = set(CSHARP_BASE_DEFINES)
    if unity_refs():
        try:
            defines |= {line.strip() for line in (UNITY_REFS / "defines.txt")
                        .read_text(encoding="utf-8").splitlines() if line.strip()}
        except OSError:
            pass
    return defines


def csharp_condition(expr: str, defines: set[str]) -> bool:
    """`#if` の条件を評価する。読めなければ ValueError。"""
    tokens = re.findall(r"\|\||&&|==|!=|[!()]|\w+|\S", expr)
    pos = 0

    def peek() -> str:
        return tokens[pos] if pos < len(tokens) else ""

    def take() -> str:
        nonlocal pos
        pos += 1
        return tokens[pos - 1] if pos <= len(tokens) else ""

    def primary() -> bool:
        token = take()
        if token == "!":
            return not primary()
        if token == "(":
            value = either()
            if take() != ")":
                raise ValueError(expr)
            return value
        if token in ("true", "false"):
            return token == "true"
        if re.fullmatch(r"[A-Za-z_]\w*", token):
            return token in defines
        raise ValueError(expr)

    def equality() -> bool:
        value = primary()
        while peek() in ("==", "!="):
            op, other = take(), primary()
            value = (value == other) if op == "==" else (value != other)
        return value

    def both() -> bool:
        value = equality()
        while peek() == "&&":
            take()
            value = equality() and value
        return value

    def either() -> bool:
        value = both()
        while peek() == "||":
            take()
            value = both() or value
        return value

    value = either()
    if pos != len(tokens):
        raise ValueError(expr)
    return value


def csharp_scan(text: str, defines: set[str]) -> tuple[str, str, list[tuple[int, int]]] | None:
    """C# のソースを、構造を数えられる形にする。(code, shape, 無効な範囲)。

    code と shape は元と同じ長さで、位置がそのまま元の位置になる。code はコメント、
    プリプロセッサの行、条件が偽の範囲を空白にしたもの。shape はさらに文字列と
    文字のリテラルを空白にしたもので、括弧と `;` を数えるのに使う。無効な範囲は、
    `#if` の条件が偽で飛ばした行の範囲。

    条件は、テストのビルドと同じシンボルで評価する。Unity の参照は UNITY_EDITOR の
    系統を外してあるので、`#if UNITY_EDITOR` の中はテストから見えない。無効な範囲は
    字句として正しいとは限らない（C# の仕様）ので、行ごとに飛ばす。

    閉じない `#if`、読めない条件、閉じないコメントなら None。
    """
    n = len(text)
    code, shape = list(text), list(text)
    inactive: list[tuple[int, int]] = []
    stack: list[list[bool]] = []    # [どれかの枝を取ったか, いまの枝が有効か]
    defines = set(defines)
    region: int | None = None       # いま飛ばしている範囲の始まり

    def blank(a: int, b: int, *targets: list[str]) -> None:
        for target in targets:
            for k in range(a, b):
                if target[k] != "\n":
                    target[k] = " "

    def starts_literal(i: int) -> bool:
        return text[i] in "\"'" or bool(re.match(r'(?:@\$?|\$@?)"', text[i:i + 3]))

    def literal_end(i: int) -> int:
        verbatim = interpolated = False
        while text[i] in "@$":
            verbatim |= text[i] == "@"
            interpolated |= text[i] == "$"
            i += 1
        quote, j = text[i], i + 1
        while j < n:
            c = text[j]
            if c == "\\" and not verbatim:
                j += 2
                continue
            if c == quote:
                if verbatim and text[j + 1:j + 2] == quote:
                    j += 2
                    continue
                return j + 1
            if interpolated and c == "{":
                if text[j + 1:j + 2] == "{":
                    j += 2
                    continue
                j = hole_end(j + 1)
                continue
            if c == "\n" and not verbatim:
                return j        # 閉じない文字列は行で終える
            j += 1
        return n

    def hole_end(j: int) -> int:
        """補間文字列の `{ ... }` の、閉じる `}` の次。中の文字列も飛ばす。"""
        depth = 0
        while j < n:
            c = text[j]
            if starts_literal(j):
                j = literal_end(j)
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                if depth == 0:
                    return j + 1
                depth -= 1
            j += 1
        return n

    if text.startswith("﻿"):
        blank(0, 1, code, shape)
    i = 0
    while i < n:
        if i == 0 or text[i - 1] == "\n":
            eol = text.find("\n", i)
            eol = n if eol < 0 else eol
            directive = CSHARP_DIRECTIVE.match(text, i, eol)
            if directive:
                if region is not None:
                    inactive.append((region, i))
                    region = None
                word = directive.group(1)
                arg = directive.group(2).split("//")[0].strip()
                on = all(entry[1] for entry in stack)
                try:
                    if word == "if":
                        value = csharp_condition(arg, defines)
                        stack.append([value, value])
                    elif word == "elif":
                        entry = stack[-1]
                        entry[1] = not entry[0] and csharp_condition(arg, defines)
                        entry[0] = entry[0] or entry[1]
                    elif word == "else":
                        entry = stack[-1]
                        entry[1], entry[0] = not entry[0], True
                    elif word == "endif":
                        stack.pop()
                    elif word == "define" and on:
                        defines.add(arg)
                    elif word == "undef" and on:
                        defines.discard(arg)
                except (ValueError, IndexError):
                    return None
                blank(i, eol, code, shape)
                i = eol + 1     # 改行ごと進む。空の行では eol が i と同じだ
                continue
            if not all(entry[1] for entry in stack):
                if region is None:
                    region = i
                blank(i, eol, code, shape)
                i = eol + 1
                continue
        c, nxt = text[i], text[i + 1:i + 2]
        if c == "/" and nxt == "/":
            eol = text.find("\n", i)
            eol = n if eol < 0 else eol
            blank(i, eol, code, shape)
            i = eol
        elif c == "/" and nxt == "*":
            end = text.find("*/", i + 2)
            if end < 0:
                return None
            blank(i, end + 2, code, shape)
            i = end + 2
        elif starts_literal(i):
            end = literal_end(i)
            blank(i, end, shape)
            i = end
        else:
            i += 1
    if stack:
        return None
    if region is not None:
        inactive.append((region, n))
    return "".join(code), "".join(shape), inactive


def csharp_match(shape: str, i: int, hi: int) -> int | None:
    """shape[i] の `{` を閉じる `}` の位置。"""
    depth = 0
    for j in range(i, hi):
        if shape[j] == "{":
            depth += 1
        elif shape[j] == "}":
            depth -= 1
            if depth == 0:
                return j
    return None


def csharp_split(shape: str, lo: int, hi: int) -> list[tuple[int, int, int | None, int | None]] | None:
    """shape[lo:hi] の宣言を順に切る。(始まり, 終わり, 本体の `{`, それを閉じる `}`)。

    宣言は、深さ 0 の `;` か、本体を閉じる `}` で終わる。`=` か `=>` の後の `{` は
    値で、本体ではない（`int[] a = { 1 };`、`=> new Board { W = 1 };`）。本体の後に
    `= 値;` が続けば、プロパティの初期値としてそこまで含める。
    """
    units = []
    i = lo
    while i < hi:
        while i < hi and shape[i].isspace():
            i += 1
        if i >= hi:
            break
        start, depth, assigned = i, 0, False
        open_: int | None = None
        close: int | None = None
        while i < hi:
            c = shape[i]
            if c == "{" and depth == 0 and not assigned and open_ is None:
                close = csharp_match(shape, i, hi)
                if close is None:
                    return None
                open_, i = i, close + 1
                j = i
                while j < hi and shape[j].isspace():
                    j += 1
                if j < hi and shape[j] == "=":
                    i = j           # 初期値。`;` まで続ける
                    continue
                if j < hi and shape[j] == ";":
                    i = j + 1
                break
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
                if depth < 0:
                    return None
            elif c == "=" and depth == 0:
                assigned = True
            elif c == ";" and depth == 0:
                i += 1
                break
            i += 1
        units.append((start, i, open_, close))
    return units


def csharp_after_attributes(shape: str, i: int, hi: int) -> int:
    """属性（`[SerializeField]`）と空白を飛ばした位置。"""
    while True:
        while i < hi and shape[i].isspace():
            i += 1
        if i >= hi or shape[i] != "[":
            return i
        depth = 0
        while i < hi:
            if shape[i] == "[":
                depth += 1
            elif shape[i] == "]":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        i += 1


def csharp_norm(text: str) -> str:
    """比べるための形。空白を詰め、記号の前後の空白を除く。"""
    return re.sub(r"\s*([(),<>\[\]?.:])\s*", r"\1", " ".join(text.split()))


def csharp_member_parts(header: str) -> dict:
    """メンバーの宣言の頭（属性、本体、`=>` より後を除いたもの）を分ける。

    返すのは mods（修飾子の集合）、kind（"method"、"operator"、"indexer"。どれでも
    なければ None で、プロパティかフィールドかは呼び出し側が本体の有無で決める）、
    name と names（フィールドは `int a, b` の全部）、type（戻り値かフィールドの型。
    コンストラクタは空）、after（メソッドの名前より後。型引数と引数）、sig（修飾子を
    除いた署名を csharp_norm で詰めたもの。比べるのに使う）。
    """
    rest = " ".join(header.split()).rstrip(";").strip()
    mods: list[str] = []
    while (m := re.match(r"([a-z]+)\s+", rest)) and m.group(1) in CSHARP_MODIFIER_WORDS:
        mods.append(m.group(1))
        rest = rest[m.end():]
    parts = {"mods": mods, "kind": None, "name": "", "names": [], "type": "", "after": "",
             "sig": ""}
    if re.search(r"\boperator\b", rest):
        return {**parts, "kind": "operator", "name": "operator", "sig": csharp_norm(rest)}
    # 引数の括弧は `=` より前にある。後ろの括弧は初期値（`= new Board(0, 0)`）だ。
    eq = ts_top_level(rest, "=")
    depth = angle = 0
    for i, c in enumerate(rest[:eq]):
        if c == "<" and depth == 0:
            angle += 1
        elif c == ">" and depth == 0:
            angle -= 1
        elif c == "(":
            if depth == 0 and angle == 0:
                before = CSHARP_NAME_BEFORE.search(rest[:i])
                if before and before.group(1) not in CSHARP_MODIFIER_WORDS:
                    close, inner = i, 0
                    for close in range(i, len(rest)):
                        inner += {"(": 1, ")": -1}.get(rest[close], 0)
                        if inner == 0:
                            break
                    name = before.group(1)
                    return {**parts, "kind": "method", "name": name, "names": [name],
                            "type": rest[:before.start()].strip(),
                            "after": rest[before.start(1) + len(name):close + 1].strip(),
                            "sig": csharp_norm(rest[:close + 1])}
            depth += 1
        elif c == ")":
            depth -= 1
    if re.search(r"\bthis\s*\[", rest):
        return {**parts, "kind": "indexer", "name": "this", "names": ["this"],
                "sig": csharp_norm(rest)}
    # フィールドかプロパティ。`int a = 1, b` の2つ目からは、深さ 0 の `,` の後の名前。
    pieces, depth, current = [], 0, ""
    for c in rest:
        if c in "([{<":
            depth += 1
        elif c in ")]}>":
            depth -= 1
        if c == "," and depth == 0:
            pieces.append(current)
            current = ""
            continue
        current += c
    pieces.append(current)
    first = pieces[0]
    eq = ts_top_level(first, "=")
    first = first[:eq] if eq is not None else first
    named = re.search(r"([A-Za-z_]\w*)\s*$", first)
    if not named:
        return parts
    names = [named.group(1)] + [m.group(1) for piece in pieces[1:]
                                if (m := re.match(r"\s*([A-Za-z_]\w*)\s*(?:=|$)", piece))]
    return {**parts, "name": names[0], "names": names,
            "type": first[:named.start()].strip(), "sig": csharp_norm(first)}


def csharp_spec(spec: str) -> tuple[str, dict]:
    """契約のメンバー（型の名前を除いたもの）の種類と、csharp_member_parts の結果。"""
    parts = csharp_member_parts(spec)
    if parts["kind"]:
        return parts["kind"], parts
    if "{" in spec or "=>" in spec:
        return "property", csharp_member_parts(re.split(r"\{|=>", spec, 1)[0])
    return "field", parts


def csharp_outline(text: str) -> dict | None:
    """C# のファイルの型とメンバーの位置。読めなければ None。

    返すのは code と shape（csharp_scan）、inactive（無効な範囲）、usings（using の
    範囲）、others（型でも using でもない最上位の宣言の範囲）、types。型は
    namespace の中まで降りて集め、入れ子の型はメンバーとして扱う。型もメンバーも、
    start は属性の前、sig は属性の後、stop は終わりの次。open と close は本体の
    `{` と `}`、arrow は `=>` の位置（無ければ None）。
    """
    scanned = csharp_scan(text, csharp_defines())
    if scanned is None:
        return None
    code, shape, inactive = scanned
    types: list[dict] = []
    usings: list[tuple[int, int]] = []
    others: list[tuple[int, int]] = []

    def head_of(start: int, stop: int, open_: int | None) -> tuple[int, str]:
        sig = csharp_after_attributes(shape, start, stop)
        end = open_ if open_ is not None else stop
        return sig, " ".join(code[sig:end].split()).rstrip(";").rstrip()

    def members_of(lo: int, hi: int) -> list[dict] | None:
        units = csharp_split(shape, lo, hi)
        if units is None:
            return None
        members = []
        for start, stop, open_, close in units:
            sig, head = head_of(start, stop, open_)
            member = {"start": start, "sig": sig, "stop": stop, "open": open_,
                      "close": close, "arrow": None}
            nested = CSHARP_TYPE_HEAD.match(head)
            if nested:
                name = nested.group("name")
                members.append({**member, "kind": "type", "name": name, "names": [name],
                                "parts": {"mods": nested.group("mods").split()}})
                continue
            end = open_ if open_ is not None else stop
            arrow = ts_top_level(shape[sig:end], "=>")
            eq = ts_top_level(shape[sig:end], "=")
            if arrow is not None and (eq is None or arrow < eq):
                member["arrow"] = sig + arrow
                end = sig + arrow
            parts = csharp_member_parts(code[sig:end])
            kind = parts["kind"] or ("property" if open_ is not None or member["arrow"] is not None
                                     else "field")
            members.append({**member, "kind": kind, "name": parts["name"],
                            "names": parts["names"], "parts": parts})
        return members

    def walk(lo: int, hi: int, namespace: str) -> bool:
        units = csharp_split(shape, lo, hi)
        if units is None:
            return False
        for start, stop, open_, close in units:
            sig, head = head_of(start, stop, open_)
            space = re.fullmatch(r"namespace\s+([\w.]+)", head)
            declared = CSHARP_TYPE_HEAD.match(head)
            if space:
                if open_ is None:
                    # ファイル単位の namespace（C# 10。Promete のコードはこの形で書く）。
                    # ファイルの残りがその中にある。ほかの namespace の中には書けない。
                    return not namespace and walk(stop, hi, space.group(1))
                if not walk(open_ + 1, close, ".".join(filter(None, [namespace, space.group(1)]))):
                    return False
            elif re.match(r"(?:global\s+)?using\b", head):
                usings.append((start, stop))
            elif declared and open_ is not None:
                kind = declared.group("kind")
                members = [] if kind == "enum" else members_of(open_ + 1, close)
                if members is None:
                    return False
                types.append({"name": declared.group("name"), "kind": kind,
                              "mods": declared.group("mods").split(),
                              "rest": declared.group("rest"), "namespace": namespace,
                              "start": start, "sig": sig, "stop": stop, "open": open_,
                              "close": close, "members": members})
            else:
                others.append((start, stop))
        return True

    if not walk(0, len(text), ""):
        return None
    return {"code": code, "shape": shape, "inactive": inactive, "usings": usings,
            "others": others, "types": types}


def csharp_file_namespace(path: str) -> str | None:
    """作業ツリーにある C# のファイルの名前空間。無ければフォルダから決める。"""
    target = PROJECT / path
    if target.is_file():
        outline = csharp_outline(target.read_text(encoding="utf-8"))
        if outline and outline["types"]:
            return outline["types"][0]["namespace"]
    return csharp_namespace(path)


def csharp_accessors(outline: dict, member: dict) -> str:
    """プロパティの公開のアクセサ。`{ get; set; }` の形。"""
    if member["open"] is None:
        return "{ get; }"          # `int X => ...;`
    heads = []
    code, shape = outline["code"], outline["shape"]
    for start, stop, open_, _ in csharp_split(shape, member["open"] + 1, member["close"]) or []:
        end = open_ if open_ is not None else stop
        arrow = ts_top_level(shape[start:end], "=>")
        end = start + arrow if arrow is not None else end
        head = " ".join(code[start:end].split()).rstrip(";").strip()
        if head and not re.search(r"\b(?:private|protected|internal)\b", head):
            heads.append(head + ";")
    return "{ " + " ".join(heads) + " }"


def csharp_declarations(text: str, path: str) -> list[str]:
    """C# のファイルの public な型とメンバーを、契約と同じ書式で1行ずつ返す。

    型は頭だけを1行にし（`class Board : MonoBehaviour`）、public なメンバーを
    `static int Board.Score(Board board)` の形で1行ずつ続ける。名前が型とメンバーの
    2つの単位を持つので、requires はメンバーを名指しできる。enum と interface は
    形そのものなので、中身ごと1行にする。本体と初期値は返さない（BOOTSTRAP 1-1）。

    名前空間があれば、行の末尾に `-- namespace X` を付ける。既存のファイルは今の
    名前空間を保つので、フォルダから決めた名前と違いうる。その型を使うコードは、
    これで `using` を書く。

    演算子、インデクサ、入れ子の型は契約の名前で表せないので返さない。
    """
    outline = csharp_outline(text)
    if outline is None:
        return []
    code = outline["code"]
    lines = []

    def signature(owner: str, member: dict) -> str:
        parts = member["parts"]
        mods = " ".join(m for m in parts["mods"] if m != "public")
        name = f"{owner}.{member['name']}" if owner else member["name"]
        if member["kind"] == "method":
            text_ = f"{mods} {parts['type']} {name}{parts['after']}"
        elif member["kind"] == "property":
            text_ = f"{mods} {parts['type']} {name} {csharp_accessors(outline, member)}"
        else:
            return ""
        return " ".join(text_.split())

    for t in outline["types"]:
        if "public" not in t["mods"]:
            continue
        where = f" -- namespace {t['namespace']}" if t["namespace"] else ""
        mods = " ".join(m for m in t["mods"] if m not in ("public", "partial"))
        head = " ".join(f"{mods} {t['kind']} {t['name']}{t['rest']}".split())
        if t["kind"] == "enum":
            body = " ".join(code[t["open"] + 1:t["close"]].split())
            lines.append(f"{path}: {head} {{ {body} }}{where}")
            continue
        if t["kind"] == "interface":
            members = [signature("", m) for m in t["members"]]
            body = " ".join(f"{m};" if not m.endswith("}") else m for m in members if m)
            lines.append(f"{path}: {head} {{ {body} }}{where}")
            continue
        lines.append(f"{path}: {head}{where}")
        for m in t["members"]:
            if "public" not in m["parts"]["mods"]:
                continue
            if m["kind"] == "field":
                mods = " ".join(x for x in m["parts"]["mods"] if x != "public")
                lines += [f"{path}: " + " ".join(f"{mods} {m['parts']['type']} {t['name']}.{n}"
                                                 .split()) + where for n in m["names"]]
            elif m["kind"] in ("method", "property"):
                lines.append(f"{path}: {signature(t['name'], m)}{where}")
    return lines


# 短くした行に残す修飾子。呼び方（型から呼ぶか、値を書けるか）を決めるものだけ。
CSHARP_SHORT_MODIFIERS = {"static", "const", "readonly", "abstract", "virtual", "override"}
CSHARP_PARAMETER_MODIFIERS = {"this", "ref", "out", "in", "params"}


def csharp_short_declaration(line: str) -> str:
    """csharp_declarations のメンバーの行から、引数と戻り値の型を外す。

    `static int Board.Score(Board board, int bonus = 0)` は `static Board.Score(board, bonus)`
    になる。型の頭、enum、interface はそのまま返す。declared_name は元の行と同じ
    名前を返すので、この行を requires に写しても L3 を通り、ソルバーには
    existing_requirements が HEAD の署名を渡す。取り込んだ Unity のプロジェクトでは、
    bootstrap のブリーフの大半がこの一覧で、呼び出しのたびにそれを読み直していた。
    """
    name = declared_name(line)
    if "." not in name:
        return line
    path, sep, rest = line.partition(": ")
    if not sep:
        path, rest = "", line
    body, dash, where = rest.partition(" -- ")
    found = re.search(re.escape(name) + r"(?=\s*(?:\(|\{|;|=|<|$))", body)
    if not found:
        return line
    mods = [w for w in body[:found.start()].split() if w in CSHARP_SHORT_MODIFIERS]
    after = body[found.end():].lstrip()

    def closing(text: str, open_: str, close: str) -> int:
        depth = 0
        for i, c in enumerate(text):
            if c == open_:
                depth += 1
            elif c == close:
                depth -= 1
                if depth == 0:
                    return i
        return len(text)

    tail = ""
    if after.startswith("<"):
        stop = closing(after, "<", ">") + 1
        tail, after = after[:stop], after[stop:].lstrip()
    if after.startswith("("):
        inner = after[1:closing(after, "(", ")")]
        params, depth, start, quote = [], 0, 0, ""
        for i, c in enumerate(inner + ","):
            if quote:
                if c == quote and inner[i - 1] != "\\":
                    quote = ""
            elif c in "\"'":
                quote = c
            elif c in "<([":
                depth += 1
            elif c in ">)]":
                depth -= 1
            elif c == "," and depth == 0:
                param = inner[start:i].split("=")[0].split()
                if param:
                    params.append(" ".join([w for w in param[:-1]
                                            if w in CSHARP_PARAMETER_MODIFIERS] + [param[-1]]))
                start = i + 1
        tail += f"({', '.join(params)})"
    elif after.startswith("{"):
        tail += " " + after[:closing(after, "{", "}") + 1]
    short = " ".join(mods + [name + tail])
    return (f"{path}: " if path else "") + short + (f" -- {where}" if dash else "")


def csharp_balanced(text: str) -> bool:
    """差し替える範囲の中で、`#if` と `#endif` の数が合うか。"""
    return (len(re.findall(r"(?m)^[ \t]*#[ \t]*if\b", text))
            == len(re.findall(r"(?m)^[ \t]*#[ \t]*endif\b", text)))


def csharp_merge_stub(original: str, entries: list[dict], known: dict[str, str],
                      unity: bool) -> str | None:
    """既存の C# のファイルで、provides のメンバーの本体だけをスタブにする。

    署名が契約と同じメンバーは、本体（`{ ... }` か `=> ...;`）だけを
    CSHARP_STUB_BODY に替える。属性、修飾子、コメント、ほかのメンバーは残る。
    プロパティはアクセサの本体を替え、自動プロパティとフィールドは残す。ファイルに
    無いメンバーは型の末尾に足し、それが口にするほかの名前空間の型には using を足す。

    次のときは None でソルバーに回す。型がファイルに無い。同じ名前のメンバーは
    あるが、署名が契約と同じものがちょうど1つではない（署名を変えるステップか、
    オーバーロード）。本体の無いメソッド（abstract など）。enum か interface の中身が
    契約と違う。ファイルが読めない。

    `entries` は generate_csharp_stub の型ごとのまとまり、`known` は型の名前から
    名前空間への対応。
    """
    outline = csharp_outline(original)
    if outline is None:
        return None
    shape = outline["shape"]
    edits: list[tuple[int, int, str]] = []
    added: list[str] = []
    file_namespace = ""
    for entry in entries:
        found = [t for t in outline["types"] if t["name"] == entry["name"]]
        if len(found) != 1:
            return None
        t = found[0]
        file_namespace = t["namespace"]
        if entry["typed"]:
            if entry["kind"] != t["kind"]:
                return None
            if entry["bases"] and csharp_norm(entry["bases"]) not in csharp_norm(t["rest"]):
                return None
            if t["kind"] in ("enum", "interface"):
                inside = outline["code"][t["open"] + 1:t["close"]]
                if re.sub(r"[\s;,]", "", entry["body"]) != re.sub(r"[\s;,]", "", inside):
                    return None
                continue

        new: list[str] = []
        for spec in entry["specs"]:
            kind, parts = csharp_spec(spec)
            named = [m for m in t["members"] if parts["name"] and parts["name"] in m["names"]]
            if not named:
                new.append(csharp_stub_member(spec))
                continue
            same = [m for m in named if m["kind"] == kind
                    and m["parts"]["sig"] == parts["sig"]
                    and (kind != "method"
                         or ("static" in m["parts"]["mods"]) == ("static" in parts["mods"]))]
            if len(same) != 1:
                return None
            m = same[0]
            if kind == "method":
                if m["open"] is not None:
                    edits.append((m["open"], m["close"] + 1, CSHARP_STUB_BODY))
                elif m["arrow"] is not None:
                    edits.append((m["arrow"], m["stop"], CSHARP_STUB_BODY))
                else:
                    return None
            elif kind == "property":
                if m["open"] is None:
                    edits.append((m["arrow"], m["stop"], f"=> {CSHARP_STUB_EXPRESSION};"))
                    continue
                for start, stop, open_, close in csharp_split(shape, m["open"] + 1, m["close"]) or []:
                    arrow = ts_top_level(shape[start:stop], "=>")
                    if open_ is not None:
                        edits.append((open_, close + 1, CSHARP_STUB_BODY))
                    elif arrow is not None:
                        edits.append((start + arrow, stop, f"=> {CSHARP_STUB_EXPRESSION};"))

        # 入れ子の型は丸ごと足す。同じ名前のものがもうあれば、中身を合わせる手立てが
        # 無いのでソルバーに回す。
        for nested in entry.get("nested", []):
            if any(nested["name"] in m["names"] for m in t["members"]):
                return None
            new.append(nested["block"])

        if new:
            # 型の末尾、閉じる `}` の行の前に、ほかのメンバーと同じ字下げで足す。
            close = t["close"]
            line_start = original.rfind("\n", 0, close) + 1
            before = original[line_start:close]
            outer = re.match(r"[ \t]*", before).group(0)
            indent = outer + "    "
            if t["members"]:
                first = t["members"][0]["start"]
                lead = original[original.rfind("\n", 0, first) + 1:first]
                if lead and not lead.strip():
                    indent = lead
            block = ("\n" if t["members"] else "") + "".join(
                f"{indent}{m.replace(chr(10), chr(10) + indent)}\n" for m in new)
            if before.strip():
                edits.append((close, close, "\n" + block + outer))
            else:
                edits.append((line_start, line_start, block))
            added += new

    if added:
        text = "\n".join(added)
        present = set()
        for start, stop in outline["usings"]:
            used = re.fullmatch(r"using\s+([\w.]+)\s*;?", " ".join(outline["code"][start:stop].split()))
            if used:
                present.add(used.group(1))
        wanted = {ns for name, ns in known.items()
                  if ns and ns != file_namespace and not file_namespace.startswith(ns + ".")
                  and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text)}
        if unity and CSHARP_UNITY_TYPES.search(text):
            wanted.add("UnityEngine")
        wanted -= present
        if wanted:
            directives = "".join(f"using {ns};\n" for ns in sorted(wanted))
            if outline["usings"]:
                at = outline["usings"][-1][1]
                edits.append((at, at, "\n" + directives.rstrip("\n")))
            else:
                at = 1 if original.startswith("﻿") else 0
                edits.append((at, at, directives + "\n"))

    edits.sort(key=lambda e: (e[0], e[1]))
    for (a, b, _), (c, _, _) in zip(edits, edits[1:]):
        if b > c:
            return None
    if not all(csharp_balanced(original[a:b]) for a, b, _ in edits):
        return None
    text = original
    for a, b, replacement in reversed(edits):
        text = text[:a] + replacement + text[b:]
    return text


def csharp_listed_members(provides: list[str]) -> set[str]:
    """型の行の本体に並んだメンバーの名前（`Board.Width`）。"""
    names = set()
    for line in provides:
        m = re.match(r"^[\w./-]+\.cs\s*:\s*(.+)$", line.split(" -- ")[0].split(chr(8212))[0].strip())
        declared = CSHARP_TYPE_LINE.match(m.group(1).strip()) if m else None
        if declared and declared.group("kind") in ("class", "struct"):
            for member in csharp_members(declared.group("body") or ""):
                names |= {f"{declared.group('name')}.{n}" for n in csharp_spec(member)[1]["names"]}
    return names


def csharp_units(text: str, provided: set[str]) -> dict[str, str] | None:
    """top_level_units の C# 版。単位は型の頭とメンバー。

    クラスを1つの単位にすると、1つのメソッドの差し替えでクラス全体が変わった
    ことになる。だから型の頭（属性、修飾子、基底）と、メンバーの1つずつを比べる。
    `provided` の名前（`Board.Score` か型の `Board`）のメンバーは比べない。enum と
    interface は形そのものなので、型ごと1つの単位にする。コメントとプリプロセッサの
    行は数えない。条件が偽の範囲は、そのまま文字列で比べる（UNITY_EDITOR の中も、
    Unity のエディタでは動く）。ただし比べないメンバーの中にあるものは除く。using は
    比べない。
    """
    outline = csharp_outline(text)
    if outline is None:
        return None
    code = outline["code"]
    units: dict[str, str] = {}
    skipped: list[tuple[int, int]] = []

    def add(prefix: str, a: int, b: int) -> None:
        flat = " ".join(code[a:b].split())
        units[prefix + flat] = flat[:100]

    for t in outline["types"]:
        whole = t["kind"] in ("enum", "interface")
        if t["name"] in provided and whole:
            skipped.append((t["start"], t["stop"]))
            continue
        if whole:
            add("", t["start"], t["stop"])
            continue
        if t["name"] not in provided:
            add("", t["start"], t["open"] + 1)
        for m in t["members"]:
            if m["names"] and all(f"{t['name']}.{n}" in provided for n in m["names"]):
                skipped.append((m["start"], m["stop"]))
                continue
            add(f"{t['name']}.", m["start"], m["stop"])
    for a, b in outline["others"]:
        add("", a, b)
    for a, b in outline["inactive"]:
        if any(s <= a and b <= e for s, e in skipped):
            continue
        flat = " ".join(text[a:b].split())
        units["#inactive " + flat] = flat[:100]
    return units


def tool_version(binary: Path, cwd: Path = PROJECT) -> str:
    """ツールが名乗る版の1行目。走らなければそう書く。"""
    try:
        proc = run([str(binary), "--version"], cwd=cwd)
        return (proc.stdout + proc.stderr).strip().splitlines()[0]
    except (OSError, IndexError):
        return "(not installed)"


def test_runner_version() -> str:
    """テストのコマンドの版。C# は、テストを走らせるときと同じく build の global.json が
    選ぶ SDK の版を出す。箱でなければ（build が無ければ）、ほかの言語と同じに聞く。"""
    if LANGUAGE["name"] == "csharp" and DOTNET_BUILD.is_dir():
        try:
            (DOTNET_BUILD / "global.json").write_text(dotnet_global_json(), encoding="utf-8")
            return tool_version(LANGUAGE["test_runner"], DOTNET_BUILD)
        except OSError:
            pass
    return tool_version(LANGUAGE["test_runner"])


def python_facts() -> tuple[str, str, str]:
    """Python の設計に伝える、インタプリタと画面についての事実。

    書き写さずに集める。これは効き目が大きい。「窓が開く」のような条件は TclError
    で落ちるテストになり、それは本物の赤で、RED_GATE をきれいに通る。ソルバーが何を
    書いても緑にならないので、モジュールはすべての段の試行を使って中段に戻り、
    その原因はどれにも見えるところに無い。
    """
    interpreter = PROJECT / ".venv" / "bin" / "python"

    def imports(module: str) -> bool:
        try:
            return run([str(interpreter), "-c", f"import {module}"]).returncode == 0
        except OSError:
            return False

    runtime = f"Interpreter: {tool_version(interpreter)}"
    toolkit = ("GUI toolkit: "
               + ("tkinter imports" if imports("tkinter")
                  else "tkinter does NOT import"))
    screen = "" if os.environ.get("DISPLAY", "") else """There is no screen here and there will not be one. Code that opens a window
raises an error about the display, so an acceptance criterion about what appears
on screen becomes a test that fails and that no implementation can fix.

Every criterion you write has to be checkable by pytest with no display. If the
requirements ask for a user interface, put whatever constructs it in its own
step, keep that step as thin as you can, and write its criteria in terms of the
functions it calls and the state it passes on -- not in terms of what is drawn.
A human checks the screen afterwards; the runner never can.
"""
    return runtime, toolkit, screen


def dom_facts() -> tuple[str, str, str]:
    """ブラウザのページに持ち込む計画に伝える、DOM についての事実。"""
    runtime = f"Runtime: {tool_version(Path('node'))}"
    toolkit = ("User interface: the DOM, via happy-dom. Every test file is "
               "given a `document` with no display behind it.")
    screen = """There is no screen, and there does not need to be one. happy-dom builds a
document in memory, so a test can construct the interface, read what it says,
click a button and assert what changed. THE USER INTERFACE IS CHECKABLE HERE --
do not push it out of reach of the tests.

This is worth saying plainly because the previous attempt at this was written
for a toolkit that could not be driven without a screen. The plan quite
reasonably confined the interface to one function nobody could test, and what
shipped was a window in which every button was disabled from the first frame:
ten steps green, forty-two tests passing, and nothing the player could press.
Write criteria about what is on the screen and what happens when it is used.
`element.click()` works, and so does reading `textContent` and `disabled`.
"""
    return runtime, toolkit, screen


def dom_page_text() -> str:
    """根の index.html を、説明ではなく全文で見せる。無ければ何も言わない。

    文章の説明は、それに向けて書くプランナーには足りたが、出来上がった計画を読む
    クリティックには足りなかった。最初の本番の批評は、index.html を作るものも
    start() を呼ぶものも無いと2度報告した。計画は index.html を作らない。作るのは
    環境で、誰もクリティックにそれを伝えていなかった。5件の指摘のうち2件が、
    ずっと根にあったファイルについてのものだった。
    """
    page = PROJECT / "index.html"
    if not page.is_file():
        return ""
    # 開発サーバで開けることも環境の持ち物だ。run 8 の S10 は、Vite が index.html を
    # 変換した結果に /src/main.ts が含まれることを確かめる条件を持っていた。Vite は
    # インラインのモジュールスクリプトを別のモジュールに切り出すので、含まれない。
    # どの実装でも変わらず、ソルバーは時間切れまで考えた。開けることは箱の検査が
    # 確かめ、計画には start が何を組み立てるかだけを書かせる。
    return ("""
`index.html` at the root, which the environment provides and no step writes.
It is what a person opens, and it is already wired:

"""
            f"{page.read_text(encoding='utf-8')}") + layout_text("""
Opening the page through a development server (Vite, for example) is also the
environment's job. The server serves index.html, and index.html loads `start`
from {SRC}/main.ts; that path is checked when the machine is provisioned. If the
requirements say the page must open from a dev server, a step that exports
`start` satisfies it. Write criteria about what `start` puts in the document.

Do not write criteria that start a development server, transform index.html, or
check what a server sends back. That output is decided by the server, not by
any code a step writes: no implementation can change it, and the step spends
every attempt on it.
""")


def unity_facts() -> tuple[str, str]:
    """Unity に持ち込む計画に伝える、UI とエンジンについての事実。(1行の要約, 段落)。

    Unity の参照があるとき（loop-unity-refs）の中身は、thm で probe-unity が測った
    結果だ。エンジン本体の無い .NET 8 では、C# だけで書かれた計算は動き、エンジンの
    ネイティブに降りるものは SecurityException で落ちる。MonoBehaviour を new する
    ことすらできない。そのテストは赤ではなく壊れた呼び出しになり、R5 が拒む。
    それを計画を書く前に知らせる。
    """
    if not unity_refs():
        return ("User interface: none. There is no display and no UI toolkit.",
                """There is no screen here. Every criterion has to be checkable by calling code
and comparing what it returns. If the requirements ask for a user interface,
keep it in a thin step whose criteria are about the values it passes on, not
what is drawn. A human checks the screen afterwards.
""")
    try:
        version = (UNITY_REFS / "version.txt").read_text(encoding="utf-8").strip()
    except OSError:
        version = "(unknown version)"
    return (f"Unity: compiled against the reference assemblies of Unity {version} and "
            f"this project's packages. The engine itself is not here.",
            """The code is compiled against Unity's assemblies with the symbols Unity defines
for the player, so it may use UnityEngine types. The tests, however, run on
plain .NET 8 with no engine behind them. Measured on this machine, inside a
test:

    works:   plain C# classes and structs, static methods, collections;
             Vector2 / Vector3 arithmetic; Mathf; Color
    throws:  creating a GameObject, a MonoBehaviour (even with `new`) or a
             ScriptableObject; Debug.Log; Random; Time; Quaternion.Euler;
             JsonUtility -- anything that calls into the engine
             (SecurityException: ECall methods must be packaged into a
             system module)

So every criterion must be checkable by calling code that is none of those
things. Put the behaviour a criterion checks in a plain class or a static
method that takes and returns plain values, and let a MonoBehaviour call it.
The code that runs under a test must not call Debug.Log, Random, Time or any
other engine function either; pass such values in as parameters.

Do not write criteria about a MonoBehaviour, a scene, a prefab or what appears
on screen. The test cannot create them: it fails with an exception rather than
an assertion, and RED_GATE rejects the step (R5). A human checks those in
Unity afterwards.
""")


def promete_facts() -> tuple[str, str]:
    """Promete に持ち込む計画に伝える、エンジンについての事実。(1行の要約, 段落)。

    動くと書いたものは、Promete 自身のテスト（NodeTests、NextFrameTests、
    ScenelessRunTests）がヘッドレスで確かめているものだけだ。箱での実測は
    probe-promete が出す。測っていないものは、使えないものとして扱わせる。
    """
    entries = csharp_entry_files()
    listing = "\n".join(f"    {path}" for path in entries) or "    (none yet)"
    return ("Promete 2.1.0: the package is here and the code compiles against it. There is "
            "no window and no GPU; tests use Promete's headless backend.",
            f"""Files of top-level statements -- the entry, `Program.cs` that builds the app
and loads the first scene -- are left out of the build. The person keeps them
working by hand. Left out right now:

{listing}

""" + """There is no screen and no GPU here. The code is compiled against Promete, and
the tests run on plain .NET 10. Promete has a headless backend for exactly
this, so a test can do more than call plain C#:

    works:   Vector, VectorInt, Rect, RectInt and Angle arithmetic; building a
             node tree with Container and reading Count, Parent, Location and
             Size; an app built with `PrometeApp.Create().BuildWithHeadless()`,
             whose `OnUpdate()` advances one frame (an action passed to
             `app.NextFrame(...)` runs at the start of the next one)
    treat as unavailable: loading a texture, a font or a sound from a file;
             drawing; real keyboard, mouse or gamepad input

So put the behaviour a criterion checks in plain classes, or in nodes and
scenes driven frame by frame through a headless app, and pass input in as
plain values -- which key is held, where the pointer is. A test that needs a
texture, a font, a sound or a real key press fails with an exception rather
than an assertion, and RED_GATE rejects the step (R5). What is drawn and heard
is checked by a person afterwards.
""" + promete_types_text())


def promete_types_text() -> str:
    """Promete の型の一覧。メンバーはソルバーが参照から引く（library_refs_section）。"""
    types = library_types()
    if not types:
        return ""
    return f"""
Promete's public types, by namespace. Use these names; do not invent others.
The solver looks up their members in {library_refs()}, so a criterion or a
goal may name a type here without spelling out its members.

{types}
"""


# DXライブラリの主な関数。画面、入力、音を扱う薄いファイルを書くのに足りるところまでに
# 絞る。プランナーが詳細設計に写してソルバーに渡す。人が `loop refs dxlib` で
# DxLib.h を送っていれば、ソルバーはほかの関数をそこから引く（library_refs_section）。
DXLIB_SHEET = """DXLib (DX Library) in brief. Every call returns -1 on failure unless noted.
TRUE and FALSE are ints.

  Entry and frame loop:
    #include "DxLib.h"
    int WINAPI WinMain(HINSTANCE, HINSTANCE, LPSTR, int) {
        ChangeWindowMode(TRUE);            // before DxLib_Init
        SetGraphMode(640, 480, 32);        // before DxLib_Init
        if (DxLib_Init() == -1) return -1;
        SetDrawScreen(DX_SCREEN_BACK);
        while (ProcessMessage() == 0) {    // non-zero when the window closes
            ClearDrawScreen();
            /* update, then draw */
            ScreenFlip();
        }
        DxLib_End();
        return 0;
    }
  Drawing (colors come from GetColor):
    unsigned int GetColor(int r, int g, int b)
    DrawBox(x1, y1, x2, y2, color, fillFlag)      DrawCircle(x, y, r, color, fillFlag)
    DrawLine(x1, y1, x2, y2, color)               DrawPixel(x, y, color)
    DrawString(x, y, "text", color)               DrawFormatString(x, y, color, "%d", n)
    SetFontSize(size)                             GetDrawStringWidth("text", length)
    int LoadGraph("path")  (handle)               DrawGraph(x, y, handle, transFlag)
    DrawRotaGraph(x, y, extRate, angle, handle, transFlag)   DeleteGraph(handle)
    SetDrawBlendMode(DX_BLENDMODE_ALPHA, 0..255) / (DX_BLENDMODE_NOBLEND, 0)
  Input:
    CheckHitKey(KEY_INPUT_SPACE)  1 while the key is held (KEY_INPUT_LEFT, _Z, ...)
    GetHitKeyStateAll(char buf[256])  buf[KEY_INPUT_X] is 1 while held
    GetJoypadInputState(DX_INPUT_KEY_PAD1) & PAD_INPUT_UP / _DOWN / _LEFT / _RIGHT / _1
    GetMousePoint(&x, &y)         GetMouseInput() & MOUSE_INPUT_LEFT
  Sound:
    int LoadSoundMem("path")      PlaySoundMem(handle, DX_PLAYTYPE_BACK or DX_PLAYTYPE_LOOP)
    StopSoundMem(handle)          CheckSoundMem(handle)  1 while playing
  Time and random:
    GetNowCount()  milliseconds since start      WaitTimer(ms)
    GetRand(max)   0 to max inclusive            SRand(seed)

None of these exist in the box. Key presses are levels, not edges: a press is
"held now and not held last frame", which the frame loop has to remember."""


def cpp_uses_dxlib(left_out: dict[str, str]) -> bool:
    """このプロジェクトが DXライブラリを使うか。外したファイルか仕様書で見分ける。"""
    if any(header.lower().endswith("dxlib.h") for header in left_out.values()):
        return True
    try:
        spec = spec_text()
    except OSError:
        return False
    return bool(re.search(r"dx\s*lib|DXライブラリ", spec, re.I))


def cpp_facts() -> tuple[str, str]:
    """C++ の計画に伝える、画面とプラットフォームについての事実。(1行の要約, 段落)。"""
    _, left_out = cpp_sources()
    dxlib = cpp_uses_dxlib(left_out)
    summary = ("Platform: DXLib (DX Library) on Windows. It is not here; only standard C++ "
               "is built and tested." if dxlib else
               "Platform: none. Only standard C++ is built and tested.")
    if left_out:
        listing = "\n".join(f"    {path}  (includes {header})"
                            for path, header in sorted(left_out.items()))
    else:
        listing = "    (none yet)"
    text = layout_text(f"""There is no screen, no window and no Windows here. The code is built with g++
on Linux, and only standard C++ builds. A source file under {{SRC}}/ is built
here only if every header it includes -- directly, or through the project's own
headers -- is a standard header or a file in the repository. A file that
includes anything else (DxLib.h, windows.h, a library that is not in the
repository) is left out of the build, and nothing in it can be reached by a
test. Left out right now:

{listing}

So every criterion must be checkable by calling standard C++. Put the behaviour
a criterion checks in plain classes and functions that take and return plain
values -- positions, scores, states, which key is held -- and keep them in files
that include only standard headers and each other. The platform code calls
them: it reads input, passes it in, and draws what comes back.

A platform file -- the code that draws, reads input or plays sound -- may be a
module of its own. Nothing here builds or runs that file; a person builds it in
Visual Studio and looks at the screen. Keep such a module thin, and do not
write criteria about what it draws or plays. A test that includes it does not
build, and the module is rejected.

A new source file must also be added to the person's Visual Studio project by
hand. Name every new module in the basic design so the person can.
""")
    if dxlib:
        text += "\n" + DXLIB_SHEET + "\n"
        refs = library_refs()
        text += (f"\nThe whole of DxLib.h is in {refs}; the solver looks up any other function "
                 "there, so a goal may name one that the summary above leaves out.\n"
                 if refs else
                 "\nDxLib.h itself is not here. Use only the functions above; the person "
                 "can send the header with `loop refs dxlib <DxLib.h>`.\n")
    return summary, text


def environment_facts(short: bool = False) -> str:
    """プロジェクトが実際にどうなっているかを、機械から読み取る。

    `short` なら、C# のメンバーの行から引数と戻り値の型を外す
    （csharp_short_declaration）。

    木、インタプリタ、テストのコマンドについての事実は、書き写さずに集める。
    書き写した規則は、書いた者が覚えていた分しか揃わない。
    """
    skip = {".git", ".venv", ".runner", "plan", "docs", "ac", "__pycache__",
            ".pytest_cache", "node_modules"}
    # 箱はプロジェクトの言語を知らないので、すべての言語の環境のファイルを根に置く。
    # ほかの言語のものを見せると、的外れな設計と指摘が出る。Python の設計に
    # index.html を見せると「人が開く index.html からこのコードに届かない」と
    # 指摘される。プロジェクトの言語と持ち込む先のものだけを見せる。
    for files in [base["environment_files"] for base in LANGUAGE_BASES.values()] + [
            overlay.get("environment_files", frozenset())
            for overlays in FRAMEWORKS.values() for overlay in overlays.values()]:
        skip |= files - LANGUAGE["environment_files"]
    # 根の直下と、柵の2つのディレクトリの中だけを並べる。取り込んだリポジトリの
    # 残り（Unity なら Assets/ の下の画像や音や .meta）は、どのモジュールも書かず、
    # 並べるとブリーフが数万トークンになる。
    def shown(child: Path) -> bool:
        parts = child.relative_to(PROJECT).parts
        return not any(part in skip for part in parts) and child.suffix != ".meta"

    def entry(child: Path) -> str:
        rel = child.relative_to(PROJECT).as_posix()
        return f"  {rel}/" if child.is_dir() else f"  {rel}"

    listing = [entry(child) for child in sorted(PROJECT.iterdir())
               if shown(child)] if PROJECT.is_dir() else []
    if SRC.is_dir():
        listing += [entry(child) for child in sorted(SRC.rglob("*")) if shown(child)]
    tree = "\n".join(listing) or "  (empty apart from the directories above)"

    # 根に置かれたファイルは環境の持ち物で、どのモジュールも書かない。最初の
    # モジュールより前に完成しているので、中身だけを確かめる条件はスタブの時点で
    # 通り、RED_GATE の R4 で必ず止まる。名前は書き写さず、実際に置かれているもの
    # から取る。
    provided = sorted(child.name for child in PROJECT.iterdir()
                      if child.is_file() and child.name not in skip) \
        if PROJECT.is_dir() else []
    provided_text = ""
    if provided:
        provided_text = layout_text(f"""
These files at the root belong to the environment: {", ".join(provided)}.
No module writes them, and each is already in its final form before the first
module is built. So a criterion that checks only one of them -- what it says,
what it imports, that it exists -- is already true against the stub. RED_GATE
runs every test before anything is implemented, sees that one pass, and stops
the module (R4). Every criterion has to stay false until the module's own code
under {{SRC}}/ is written. Where one of these files matters, test the code it
calls, not the file.
""")
        if "index.html" in provided:
            provided_text += """For the page: call `start` on an element and check what it puts there. Do not
write a criterion about the text of index.html.
"""

    # 取り込んだリポジトリには、最初のモジュールより前からコードがある。署名を
    # まとめて渡す。プランナーは本体を src/ から読める。
    existing = existing_contracts()
    shortened = short and LANGUAGE["name"] == "csharp"
    if shortened:
        existing = [csharp_short_declaration(line) for line in existing]
    existing_text = ""
    if existing:
        existing_text = layout_text("""
# What the code already declares

The repository already has code under {SRC}/. These are its public
declarations, read from the last commit. They exist before the first module is
built.

A module that uses one of these writes a requires line for it, with `from`
naming its file under {SRC}/ without the extension. A module of this design
whose path is that file rewrites it: its stub replaces only the names it
provides, and every other declaration in the file keeps its real, working code
while the tests first run. So a criterion that is already true because of code
the module does not change passes against the stub, and RED_GATE stops the
module (R4). Write each criterion so that it calls a name the module provides.

Do not write criteria to show that the rest still works. The tests already in
the repository run on every module, and a module that breaks one of them is not
green. That is how unchanged behaviour is kept.

""")
        if shortened:
            existing_text += """Members are shown without their parameter and return types:
`static Board.Score(board)` is a static method that takes one argument. The
runner matches requires lines against these by name, and hands the solver the
full signature from the code.

"""
        existing_text += "\n".join(f"    {line}" for line in existing) + "\n"
    existing_text += existing_tests_text()

    # テストが何に届くかを決めるファイル。言語ごとに名前は違うが仕事は同じで、
    # どちらでもプランナーに見せる必要がある。import のパスに逆らう設計は負ける。
    #
    # C# にはそのファイルが根に無い。ランナーが書くテストの csproj がその仕事を
    # するので、それを見せる。Unity の参照の一覧は長いので、見せる前に数にまとめる。
    if LANGUAGE["wiring"]:
        wiring = PROJECT / LANGUAGE["wiring"]
        wiring_heading = f"{wiring.name} at the root, which the test runner loads automatically:"
        wiring_text = wiring.read_text(encoding="utf-8") if wiring.exists() else "(none)"
    elif LANGUAGE["name"] == "cpp":
        wiring_heading = ("The CMake project the runner writes and builds before every test "
                          "run (outside the repository; no module writes it):")
        wiring_text = cpp_cmake_lists()
    else:
        wiring_heading = ("The test project the runner writes and builds (outside the "
                          "repository; no module writes it):")
        try:
            wiring_text = dotnet_projects()[DOTNET_BUILD / "tests" / "Tests.csproj"]
            refs = wiring_text.count("<Reference ")
            wiring_text = re.sub(r"(\s*<Reference [^\n]*)+",
                                 f"\n    <!-- {refs} Unity reference assemblies -->"
                                 if refs else "", wiring_text)
        except (OSError, KeyError, ValueError):
            wiring_text = "(not available: the .NET toolchain is not installed)"

    display = os.environ.get("DISPLAY", "")
    argv, _ = test_argv(["<the module's test files>"], STATE / "report.xml")
    command = " ".join(a.replace(str(PROJECT) + "/", "") for a in argv)

    # ソルバーが何をできるかも、書き写さずに使うバックエンドから決める。
    # claude と local はファイルの読み書きしかできないので、設計に「このコマンドで
    # 確かめよ」と書くと、ソルバーは実行を試みて断られ、試行の一部をそれに使う。
    solver_tools = solver_tools_note()
    if solver_tools:
        solver_tools = "\n" + solver_tools + """So do not write, in a design or anywhere else, that the solver runs the tests,
checks its work by running something, or installs anything.
"""

    runtime, toolkit, screen = LANGUAGE["facts"]()
    page_text = LANGUAGE["page"]() if LANGUAGE["page"] else ""

    return f"""# The environment, as it actually is right now

Language: {LANGUAGE["label"]}
{runtime}
Test runner: {test_runner_version()}

Graphical display: {f"DISPLAY={display}" if display else "NONE. DISPLAY is not set"}
{toolkit}

{screen}
The runner executes the tests itself, as:
    {command}
{solver_tools}
The project root and everything under {LAYOUT["src"]}/, except plan/, docs/, ac/,
.git/ and the frozen toolchain:

{tree}
{provided_text}{existing_text}
{wiring_heading}

{wiring_text}
{page_text}
"""


def findings_contract() -> str:
    """クリティックが書くものと、書いてよい唯一のファイル名。

    あえて、構造を持った枠の中に文章を入れる。ダッシュボードは指摘を数えて並べ、
    人は手を打つために指摘を読む。数だけでは「設計が仕様に逆らっている」を運べず、
    自由な文章は数えられない。
    """
    return f"""# What to write

Write exactly one file, named `{FINDINGS_NAME}`, in the current directory.
Write no other file: a name you were not given is a name the runner deletes
without reading.

    {{
      "findings": [
        {{
          "title": "one line, the claim itself",
          "evidence": "which modules, contracts or passages show it. Be specific
                       enough that someone can check you. Quote them.",
          "machine_would_notice": false
        }}
      ]
    }}

**Write `title` and `evidence` in Japanese.** A human reads the findings and
decides whether the planner writes the design again. Quote names and passages
exactly as they appear in the design.

`machine_would_notice` is false when no test written from the design would fail
while the problem stood. Say so plainly when that is the case -- it is the
difference between a design that is incomplete and one that cannot be caught.

Order the findings by how badly they hurt the person who asked for this.

**If you find nothing, write the file with an empty `findings` list.** Finding
nothing is an answer; writing no file is not, and the runner treats a missing
or unreadable file as a critique that never happened.

You cannot approve anything. Nothing you write makes any of this pass -- the
gates decide that, and a human looks at the result afterwards. Your only power
is to say that something is wrong, so do not soften a finding to be agreeable
and do not invent one to seem useful.

Output nothing but the file. Do not restate the findings in your final message.
"""


def call_critic(brief: str, mode: str) -> str:
    """クリティックにブリーフを1つ渡す。

    ブリーフが何を含み、何を含まないかに注意する。仕様書と設計は運ぶ。設計の
    批評には設計が要るからだ。受け入れ条件、テスト、台帳、何が通ったかについては
    何も運ばない。そしてクリティックはどのみちそれに届かない。docs/ と ac/ は
    0700 runner、tests/ と src/ は 2770 runner:solverw で、クリティックはどちらの
    グループにもいない。すでに通ったものが見えるクリティックは、通ったと報告する。
    """
    CRITIC_BRIEF.mkdir(parents=True, exist_ok=True)
    brief_path = CRITIC_BRIEF / f"{mode}.md"
    brief_path.write_text(brief, encoding="utf-8")
    shutil.chown(brief_path, group="criticw")
    brief_path.chmod(0o640)

    limit = TIMEOUTS["critic"]
    try:
        proc = run_agent("critic", "CRITIQUE", lambda: run(
            agent_command("critic", CRITIC_RUN, brief_path, limit),
            timeout=limit + BACKSTOP_MARGIN), detail=mode)
    except subprocess.TimeoutExpired:
        raise Halt("CRITIQUE", f"critic still running after {limit + BACKSTOP_MARGIN}s",
                   "critic-run's internal timeout did not fire; check with: "
                   "pgrep -a -u critic")
    out = proc.stdout + proc.stderr
    if proc.returncode == 124:
        raise Halt("CRITIQUE", "critic hit its own timeout in critic-run", out[-4000:])
    if proc.returncode != 0:
        raise Halt("CRITIQUE", f"critic exited {proc.returncode}", out[-4000:])
    return out


def read_findings() -> list[dict]:
    """クリティックが書いたファイルから、指摘を読む。

    読めないものは、無いものではなく必ずエラーにする。parse_junit が壊れた
    レポートを失敗ゼロと読まないのと同じ理由だ。読めない批評は計画について何も
    言っておらず、それを「問題なし」と扱うと、起きてもいない確認で作業が通る。
    """
    removed = [p.name for p in CRITIC_OUT.iterdir() if p.name != FINDINGS_NAME]
    for name in removed:
        entry = CRITIC_OUT / name
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
    if removed:
        ledger("CRITIQUE_PRUNED", removed=sorted(removed))

    path = CRITIC_OUT / FINDINGS_NAME
    if not path.is_file():
        raise Halt("CRITIQUE", f"the critic wrote no {FINDINGS_NAME}",
                   "A critique that produced no file has said nothing. Re-run it, "
                   "or read the brief for what it was asked to write.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise Halt("CRITIQUE", f"{FINDINGS_NAME} is not readable JSON: {error}",
                   path.read_text(encoding="utf-8", errors="replace")[:2000])
    findings = value.get("findings") if isinstance(value, dict) else None
    if not isinstance(findings, list):
        raise Halt("CRITIQUE", f"{FINDINGS_NAME} has no `findings` list")
    return [f for f in findings if isinstance(f, dict)]


def render_findings(by_mode: dict[str, list[dict]]) -> str:
    """人間とプランナーが読む形の指摘。"""
    lines = []
    for mode, findings in by_mode.items():
        lines.append(f"## {mode}")
        if not findings:
            lines.append("(nothing found)")
        for n, finding in enumerate(findings, 1):
            title = str(finding.get("title", "(untitled)")).strip()
            evidence = str(finding.get("evidence", "")).strip()
            unseen = finding.get("machine_would_notice") is False
            lines.append(f"{n}. {title}")
            if finding.get("rewritten"):
                lines.append("   REWRITTEN BY THE HUMAN")
            if unseen:
                lines.append("   NO GATE WOULD CATCH THIS")
            if evidence:
                lines.append("   " + evidence.replace("\n", "\n   "))
        lines.append("")
    return "\n".join(lines)


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def plain_findings(by_mode: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """人に見せて書き換えさせる形。クリティックの書いた余計なキーは落とす。"""
    return {mode: [{"title": str(f.get("title", "")).strip(),
                    "evidence": str(f.get("evidence", "")).strip(),
                    "machine_would_notice": f.get("machine_would_notice")}
                   for f in findings]
            for mode, findings in by_mode.items()}


def write_critique_for_human(round_no: int, by_mode: dict[str, list[dict]],
                             waiting: bool) -> None:
    """人が読んで直す写しを書く。waiting なら、人が直してよい。

    一時ファイルから置き換える。human/in は sticky だが、ランナーはディレクトリの
    所有者なので、保守ユーザーが作り直したファイルでも置き換えられる。
    """
    value = {"round": round_no, "waiting": waiting,
             "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "modes": by_mode}
    temporary = CRITIQUE_FOR_HUMAN.with_name(CRITIQUE_FOR_HUMAN.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    temporary.chmod(0o660)
    os.replace(temporary, CRITIQUE_FOR_HUMAN)


def read_settings() -> dict:
    """SETTINGS_FILE の走行の設定。無ければ空。

    知らないキーは拒む。黙って無視される設定になるからだ。
    """
    try:
        text = SETTINGS_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as bad:
        raise SystemExit(f"{SETTINGS_FILE}: not JSON: {bad}")
    if not isinstance(value, dict):
        raise SystemExit(f"{SETTINGS_FILE}: must be a JSON object")
    unknown = sorted(set(value) - SETTINGS_KEYS)
    if unknown:
        raise SystemExit(f"{SETTINGS_FILE}: unknown keys {unknown}; "
                         f"allowed are {sorted(SETTINGS_KEYS)}")
    return value


def load_settings(tasks: dict) -> None:
    """上限、やり直しの方針、ソルバーの段、言語を当てる。対象は存在するキーだけ。"""
    TIMEOUTS.update({k: int(v) for k, v in (tasks.get("timeouts") or {}).items()
                     if k in TIMEOUTS})
    LIMITS.update({k: int(v) for k, v in (tasks.get("limits") or {}).items()
                   if k in LIMITS})
    POLICY.update({k: str(v) for k, v in (tasks.get("policy") or {}).items()
                   if k in POLICY})
    if POLICY["retry"] not in RETRY_MODES:
        raise SystemExit(f"policy.retry must be one of {list(RETRY_MODES)}, "
                         f"not {POLICY['retry']!r}")
    tiers = tasks.get("solver_tiers")
    if tiers:
        # 取り除かずに拒む。黙って消えたバックエンドの名前があると、ループは
        # 節約するはずだった段だけで走り、台帳はそれを書かないことでしか示さない。
        bad = [t for t in tiers
               if not (isinstance(t, str) and re.fullmatch(r"[a-z0-9]+", t))]
        if bad:
            raise SystemExit(f"solver_tiers: not usable as a backend name: {bad}")
        SOLVER_TIERS[:] = list(tiers)

    # 言語。知らない値は既定に落とさず拒む。"js" を求めて黙って Python になった
    # 計画は、誤ったテストランナーで誤った拡張子に対して確かめられ、どの関門も
    # 読んでもいないファイルについて自信をもって報告する。
    # 持ち込む先も同じ理由で拒む。言語を言わない計画は、いまの言語の先を選ぶ。
    language = tasks.get("language")
    framework = tasks.get("framework")
    if language is not None or framework is not None:
        language = LANGUAGE["name"] if language is None else language
        if language not in LANGUAGES:
            raise SystemExit(f"language must be one of {sorted(LANGUAGES)}, "
                             f"not {language!r}")
        if framework is not None and framework not in FRAMEWORKS[language]:
            raise SystemExit(f"framework for {language} must be one of "
                             f"{sorted(FRAMEWORKS[language])}, not {framework!r}")
        use_language(language, framework)


def publish(what: str) -> None:
    """リポジトリを bare の origin に写す。失敗しても止めない。

    これが買うものは狭いが本物だ。`reset` と `clean` は作業ツリーに働き、bare
    リポジトリに届いたコミットはその手の外にある。だから緑のステップは、その後に
    何も起きないことに頼らずに済む。

    バックアップにはならない。repo.git はほかのすべてと同じ VHDX にある。機械の
    外に写すのはホストの仕事で、SSH で引く。ホストの仕事にしているのは意図的だ。
    サンドボックスは生成したコードが走る場所なので、その中に外へ届く資格情報を
    置いてはならない。

    ここでの失敗は報告してまたぐ。ステップが緑なのはテストが通ったからで、写しが
    コミットを受け取ったからではない。届かない写しのためにループを止めると、
    実際に成功した作業を捨てることになる。
    """
    failures = []
    # ブランチは --force を付けずに送る。ランナーの中に履歴を書き換えるものは無い
    # （`reset` は最後の緑である HEAD に戻る）。だから早送りできないのは、この
    # コードの知らない何かが起きたということで、それを上書きするのは誤った答えだ。
    branch = run(["git", "push", "--quiet", "origin", "HEAD"])
    if branch.returncode != 0:
        failures.append((branch.stdout + branch.stderr).strip()[-400:])
    # タグは動く。モジュールを reset して走らせ直すと、`git tag -f module-<id>` が
    # 指す先を変える。だからタグだけは強制して送る。
    tags = run(["git", "push", "--quiet", "--force", "origin", "--tags"])
    if tags.returncode != 0:
        failures.append((tags.stdout + tags.stderr).strip()[-400:])

    if not failures:
        ledger("PUBLISH", what=what)
        return
    ledger("PUBLISH_FAILED", what=what, detail="\n".join(failures))
    print(f"warning: could not push {what} to origin; the commit is local to "
          f"the working repository only", file=sys.stderr)
    for line in failures:
        print("  " + line.replace("\n", "\n  "), file=sys.stderr)


def complete_green(step_id: str, goal: str, attempts: int, design: str = "") -> None:
    """GREEN の証拠を、それが述べるコードと一緒にコミットする。

    台帳の事実は、その結果を記録する実装と同じコミットに入れる。GREEN を
    モジュールのコミットの後に追記すると、その記録は次のモジュールのコミットに
    入る。最後のモジュールには次が無いので、ホストの写しは常に1つ遅れて見える。

    `design` は緑になったときの設計のハッシュ。設計が変わったモジュールを、
    中段の出口で見分けるのに使う（green_designs）。テストは .gitignore で外して
    あるので、`git add -A` はコードと台帳だけを入れる。
    """
    ledger("GREEN", step=step_id, attempts=attempts, design=design)
    run(["git", "add", "-A"], check=True)
    run(["git", "commit", "-q", "-m", f"{step_id}: green"], check=True)
    run(["git", "tag", "-f", f"module-{safe_id(step_id)}"], check=True)
    publish(f"module {step_id}")


def has_all_green(lines: list[str], plan_digest: str) -> bool:
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") == "ALL_GREEN" and record.get("plan_sha256") == plan_digest:
            return True
    return False


def completion_written(plan_digest: str) -> bool:
    """コミットされたかに関係なく、作業中のファイルに記録があるか。"""
    try:
        return has_all_green(LEDGER.read_text(encoding="utf-8").splitlines(), plan_digest)
    except OSError:
        return False


def complete_run(done: set[str], tasks: dict) -> None:
    """完了を bare リポジトリに、したがって GUI に見えるようにし、最終確認の段へ移す。

    記録を書くこととコミットすることは2つの手順で、その間で失敗しうる。だから
    作業中のファイルに記録があれば書き足さず、コミットだけをやり直す。
    """
    digest = str(tasks.get("digest") or hashlib.sha256(
        json.dumps(tasks, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest())
    if not completion_written(digest):
        ledger("ALL_GREEN", steps=sorted(done), plan_sha256=digest)
    if read_stage().get("stage") != "review":
        write_stage(stage="review")
    commit_paths([LEDGER, STAGE_FILE], "run: all modules green", "run completion")


def check_baseline() -> None:
    """最初のモジュールの前に、リポジトリにすでにあるテストが緑であること。

    VERIFY はスイート全体の緑を求める。取り込んだ時点で落ちているテストや、
    スキップされるテストがあると、どのモジュールも緑にならない。それは実装の失敗に
    見え、ソルバーは試行を使い切る。設計を直しても直らない。直せるのは人だけなので、
    中段に戻さずに止める。

    テストのファイルが無ければ何もしない。新しいプロジェクトはいつもそうだ。
    """
    if not TESTS.is_dir() or not any(f.suffix in LANGUAGE["test_suffixes"]
                                     for f in source_files(TESTS)):
        return
    baseline = pytest_run("baseline", [TESTS.relative_to(PROJECT).as_posix()])
    ledger("BASELINE", tests=baseline.tests, failures=baseline.failures,
           errors=baseline.errors, skipped=baseline.skipped)
    if not (baseline.failures or baseline.errors or baseline.skipped):
        return
    detail = list(baseline.failure_details)
    if baseline.skipped_names:
        detail.append("skipped:\n  " + "\n  ".join(baseline.skipped_names))
    raise Halt(
        "PLAN_LOAD",
        f"the tests already in the repository are not green before the first module "
        f"({baseline.failures} failed, {baseline.errors} errored, {baseline.skipped} "
        f"skipped); every module is checked against the whole suite, so none could "
        f"go green. Fix or remove them, commit, and run again",
        ("\n".join(detail) or ANSI.sub("", baseline.output))[-4000:])


def own_test_keys(step: dict) -> set[str]:
    """モジュール自身のテストのファイルを、テストランナーが報告する綴りすべてで。

    2つのテストランナーはレポートでファイルを違う形で呼ぶ。pytest はドット区切りの
    モジュールのパス（tests.test_models）、vitest は渡したままのパス
    （tests/engine.test.ts）だ。C# の classname は「名前空間.クラス」で、ファイルの
    パスを持たない。クラスの名前はファイル名と同じ決まりなので、ファイル名で突き
    合わせる（test_owner）。C++ はスイートの名前で突き合わせる。
    """
    return {Path(p).with_suffix("").as_posix().replace("/", ".")
            for p in step["files_test"]} | set(step["files_test"]) \
        | set(csharp_test_classes(step["files_test"])) \
        | set(cpp_test_suites(step["files_test"]))


def run_step(step_id: str) -> int:
    """モジュールを1つ、TEST_WRITE から GREEN まで回す。止まったら中段に戻す。"""
    NOW["step"], NOW["attempt"] = step_id, None
    tasks = json.loads(TASKS.read_text(encoding="utf-8"))
    load_settings({**tasks, **read_settings()})

    step, context = load_plan(step_id)
    ledger("PLAN_LOAD", step=step_id, files_write=step["files_write"], files_test=step["files_test"])

    if touched_paths():
        raise Halt("PLAN_LOAD", "the working tree is dirty; refusing to start",
                   "\n".join(sorted(touched_paths())))
    TESTS_BEFORE.clear()
    TESTS_BEFORE.update(tests_snapshot())

    # try の外で確かめる。落ちているのはこの設計の前からあるテストで、設計を
    # 直しても直らない。
    if not green_steps():
        check_baseline()

    attempt = 0
    last_run: TestRun | None = None
    try:
        # --- TEST_WRITE -------------------------------------------------
        #
        # ここでやり直すのは、テストファイルがコンパイルできなかったときだけだ。
        # それはソルバーの誤りで、ほかの誰にも直せない。設計を直しても構文エラーは
        # 直らない。run 8 の S1 は2回これをやった。どちらも条件から単一引用符の
        # テスト名にアポストロフィを写したもので、2回目はブリーフがまさにそれを
        # 警告していた。ソルバーが無視できる指示は、コンパイラ自身の言葉を渡す
        # やり直しより価値が低い。
        #
        # もう1つ、ランナーが書いたスタブに対してテストが自分の誤りで落ちたときも
        # やり直す（crashed_tests）。スタブは契約から機械的に書いたもので、落ちたのは
        # テストの側だ。
        #
        # ほかの理由ではやり直さない。意図したことだ。落ちるテストや、スタブに
        # 対して通るテストは、頼んだ内容の問題で、もう一度頼んでも同じ誤解を引き
        # 直すために払うだけだ。
        broken = crashed = ""
        for write_attempt in range(1, LIMITS["test_writes"] + 1):
            set_writable(tests=True, src=False)
            # files_test のうち HEAD にあるものは、設計より前からあるテストで、この
            # モジュールが丸ごと書き直す。テストのファイル名はモジュールごとに違う。
            replaced = sorted(head_sources(step["files_test"]))
            if replaced and write_attempt == 1:
                ledger("TESTS_REPLACED", step=step_id, files=replaced)
            said = call_solver("TEST_WRITE",
                               brief_test_write(step, context, broken, replaced, crashed))
            assert_touched("TEST_WRITE", step["files_test"])
            assert_written("TEST_WRITE", step["files_test"], said)
            ledger("TEST_WRITE", step=step_id, ok=True, attempt=write_attempt)

            # --- STUB ---------------------------------------------------
            set_writable(tests=False, src=True)
            # 既存のファイルは、provides の名前だけを差し替える。丸ごと書き直すと、
            # ステップに関係の無い関数まで消える。
            originals = head_sources(step["files_write"])
            written = generate_stub(step, dep_contract_lines(step), originals)
            said = ""
            if written is None:
                said = call_solver("STUB", brief_stub(step, sorted(originals)))
                ledger("STUB", step=step_id, ok=True, by="solver")
            else:
                for rel, text in written.items():
                    path = PROJECT / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
                ledger("STUB", step=step_id, ok=True, by="runner",
                       files=sorted(written))
            assert_touched("STUB", step["files_test"] + step["files_write"])
            assert_written("STUB", step["files_write"], said)
            changed = stub_kept_the_rest(step, originals)
            if changed:
                raise Halt("STUB",
                           "the stub changed existing code outside the names this "
                           "step provides", "\n".join(changed))

            red = pytest_run(f"red-{write_attempt}", step["files_test"])
            broken = chr(10).join(k for k in red.failure_kinds
                                  if k.startswith("<did not compile"))
            crashed = "" if broken or written is None else crashed_tests(red)
            if not (broken or crashed):
                break
            if crashed:
                # 最後の試行なら、ファイルを残したまま RED_GATE に渡す。R5 で止まり、
                # 落ちたテストは FAILURE.md に載る。
                if write_attempt == LIMITS["test_writes"]:
                    break
                ledger("TEST_WRITE", step=step_id, ok=False, attempt=write_attempt,
                       reason="tests crashed against the stub")
                set_writable(tests=True, src=True)
                restore_to_head(step["files_test"] + step["files_write"])
                continue

            # コンパイルの確認はスタブの前ではなく後でなければならない。
            # TEST_WRITE の時点ではテスト対象のモジュールがまだ無いので、
            # まったく正しいテストファイルでも import を解決できず、構文エラーと
            # まったく同じに見える。早く確かめると、正しい作業を捨ててもう一度
            # 頼むことになる。それは、この確認で防ぎたかった問題より悪い。
            ledger("TEST_WRITE", step=step_id, ok=False, attempt=write_attempt,
                   reason=broken)
            # レポートを読む場所だけでなく、ここでも色を取り除く。vitest は変換
            # エラーを「1文字ずつ」色コードで包むので、`it('given cli` が40個の
            # エスケープシーケンスになって届き、末尾を切り出すと先頭の行と列が
            # 落ちる。ソルバーはそれを3回渡され、読めないものは直せなかった。
            # それは、TypeScript を書けないモデルとまったく同じに見えた。
            broken = broken + chr(10) + chr(10) + ANSI.sub("", red.output)[-2000:]
            set_writable(tests=True, src=True)
            restore_to_head(step["files_test"] + step["files_write"])
        else:
            raise Halt("TEST_WRITE",
                       f"the tests still do not compile after "
                       f"{LIMITS['test_writes']} attempt(s)", broken[:4000])

        # --- RED_GATE (RUNNER_SPEC 4-1, R1..R5) --------------------------
        last_run = red
        expected = step["expected_tests"]
        # R1 より先に R2 を見る。順番に意味がある。テストファイルがコンパイル
        # できないとき、件数が合わないのは何も走らなかった「から」だ。
        # 「collected 1, expected 12」と言うと、原因を隠して症状だけを述べる
        # ことになる。設計の誤りに見え、構文エラーを直せないプランナーに
        # 差し戻される。
        if red.errors:                                                     # R2
            raise Halt("RED_GATE", f"R2: {red.errors} test(s) errored instead of failing",
                       (chr(10).join(red.failure_details)
                        or ANSI.sub("", red.output))[-4000:])
        if red.tests != expected:                                          # R1
            raise Halt("RED_GATE", f"R1: collected {red.tests} tests, expected {expected}",
                       red.output[-4000:])
        if red.skipped:                                                    # R3
            raise Halt("RED_GATE", f"R3: {red.skipped} test(s) were skipped", red.output[-4000:])
        if red.passed_names:                                               # R4
            raise Halt(
                "RED_GATE",
                "R4: some tests already pass against the stub, so they never "
                "demonstrated the behaviour they claim to check",
                "passing: " + ", ".join(red.passed_names))
        bad = sorted({k for k in red.failure_kinds if not LANGUAGE["red_kinds"].match(k)})
        if bad:                                                            # R5
            crashes = [detail for kind, detail in zip(red.failure_kinds, red.failure_details)
                       if not LANGUAGE["red_kinds"].match(kind)]
            raise Halt("RED_GATE",
                       "R5: failures are not assertions -- the calls themselves are broken",
                       ("types seen: " + ", ".join(bad) + chr(10) * 2
                        + chr(10).join(crashes)).strip()[:4000])
        ledger("RED_GATE", step=step_id, tests=red.tests, failures=red.failures, ok=True)

        # --- FREEZE -----------------------------------------------------
        manifest = freeze_tests()
        (STATE / "freeze").mkdir(parents=True, exist_ok=True)
        (STATE / "freeze" / f"{safe_id(step_id)}.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")
        ledger("FREEZE", step=step_id, files=len(manifest))

        # --- IMPL / VERIFY ----------------------------------------------
        tests_text = frozen_tests_text(step)
        red_baseline = "\n".join(red.failure_kinds)
        last_failure = red_baseline
        retry_mode = step.get("retry", POLICY["retry"])
        schedule = attempt_schedule(step)

        # このモジュール自身のテストファイル。ドットの形だけにすると、TypeScript
        # では自身の落ちたテストが一致せず、すべてソルバーに回帰として伝わる。
        own_tests = own_test_keys(step)

        timeouts = 0
        while attempt < len(schedule):
            backend = schedule[attempt]
            handover = attempt > 0 and backend != schedule[attempt - 1]
            attempt += 1
            NOW["attempt"] = attempt
            set_writable(tests=None, src=True)   # tests/ は凍結したまま。set_writable を参照

            # 修理ではない試行には、きれいな木を用意する。ここには2つの別のものが
            # 来る。`resample` は、前の試行の修理ではなく独立した抽選を求める。
            # 引き継ぎは「別の」バックエンドが始めることを意味し、前任の書きかけを
            # 継ぐことは、ステップを引き継ぐことの意味ではない。
            #
            # 失敗の文面は、引き継ぎでは残り、resample では残らない。その違いが
            # 両方を持つ理由だ。resample は新しい抽選であろうとし、引き継ぎは、
            # 分かっていることをすべて渡されたより良いソルバーであろうとする。
            if attempt > 1 and (handover or retry_mode == "resample"):
                dropped = discard_attempt(step["files_write"])
                ledger("DISCARD", step=step_id, attempt=attempt, backend=backend,
                       reason="handover" if handover else "resample", files=dropped)
                if not handover:
                    last_failure = red_baseline

            try:
                call_solver("IMPL", brief_impl(step, context, tests_text, last_failure),
                            backend=backend)
            except SolverTimeout:
                # 試行を1回使っただけで、モジュールは止めない。残りの試行があれば
                # 次へ進み、使い切れば下の else がいつもどおり中段に戻す。
                last_failure = absorb_timeout(step, attempt, backend, manifest,
                                              last_failure)
                timeouts += 1
                continue

            # 何よりも先に凍結のトリップワイヤを見る。テストが変わっていれば、
            # 通ったかについて走行が言うことは、何の意味も持たない。
            for rel, digest in manifest.items():
                if sha256(PROJECT / rel) != digest:
                    raise Halt("VERIFY", f"frozen test was modified: {rel}")

            assert_touched("VERIFY", step["files_test"] + step["files_write"])

            # このステップのテストだけでなく、スイート「全体」を走らせる。
            # ステップが終わるのは、自身のテストが通り、かつすでに通っていた
            # テストがすべてまだ通るときだ。ベンチマークが FAIL_TO_PASS と
            # PASS_TO_PASS と呼ぶ2つの半分だ。前半しか確かめないと、ステップが
            # 前のステップを壊しても、ループは緑と呼んで先へ進む。それを検知する
            # ものは無く、スイート全体がたまたま壊れていなかっただけになる。
            #
            # これは関門に判断を足さない。「通っていたものが落ちたか」は測定で、
            # この設計で関門が訊いてよいのは、その種類の問いだけだ。
            green = pytest_run(f"verify-{attempt}", [TESTS.relative_to(PROJECT).as_posix()])
            last_run = green
            regressions = [f for f in green.failed_files if test_owner(f) not in own_tests]
            # skipped を記録に入れる。緑は後から台帳だけで証明できなければ
            # ならず、「失敗なし」はテストが走ったことを証明しないからだ。
            ledger("VERIFY", step=step_id, attempt=attempt, backend=backend,
                   tests=green.tests,
                   failures=green.failures, errors=green.errors,
                   skipped=green.skipped, green=green.green,
                   regressions=regressions)
            if green.green:
                break
            # 標準出力ではなくアサーションを渡す。vitest の junit の報告は、
            # 書いたレポートのパスしか出さず、レポートは .runner（0700 runner）
            # の下にあるので、ソルバーはそれも読めない。標準出力を渡すと、
            # 一度も見せていない失敗を直せと頼むことになる。
            last_failure = (chr(10).join(green.failure_details)
                            or ANSI.sub("", green.output)[-3000:])
            if green.skipped and not (green.failures or green.errors):
                # pytest はこの走行を成功と呼ぶので、出力だけではソルバーは
                # 手がかりを何も持てない。
                last_failure = (
                    "The suite reported no failures, but "
                    f"{green.skipped} test(s) never ran:\n  "
                    + "\n  ".join(green.skipped_names)
                    + "\n\nA skipped test is not a passing test, so this step "
                      "is not green. The tests are frozen and cannot be edited -- "
                      "whatever condition makes them skip has to stop being "
                      "true.\n\n" + last_failure)
            if regressions:
                # はっきり言う価値がある。すぐ思いつく直し方は、ソルバーには
                # できないものだからだ。tests/ は凍結しているので、前のテストは
                # 編集できず、残るのは実装だけだ。
                last_failure = (
                    "Tests that passed before this step are now failing:\n  "
                    + "\n  ".join(regressions)
                    + "\n\nThey are frozen and cannot be edited. Your "
                      "implementation has to stop breaking them, while still "
                      "satisfying this step.\n\n" + last_failure)
        else:
            broke = [f for f in last_run.failed_files
                     if test_owner(f) not in own_tests] if last_run else []
            tried = ", ".join(dict.fromkeys(schedule))
            reason = (f"still failing after {attempt} attempts"
                      + (f" across {tried}" if len(SOLVER_TIERS) > 1 else ""))
            if timeouts:
                # 人に届く理由に残す。全部が時間切れなら、実装が難しいのではなく、
                # モジュールが大きすぎるか、条件が満たせないことを疑う。
                reason += f"; {timeouts} of them ran out of time"
            if broke:
                # 理由の中で別に名指しする。これは FAILURE.md を通って人に届き、
                # 「このモジュールは前のモジュールを壊さずには作れない」は
                # 「このモジュールはまだできていない」とは別の問題だからだ。2組の
                # 受け入れ条件の矛盾かもしれず、それはソルバーが解くものではない。
                reason += "; and it now breaks " + ", ".join(broke)
            raise Halt("IMPL", reason, last_run.output[-4000:] if last_run else "")

        # --- GREEN ------------------------------------------------------
        contracts_dir = STATE / "contracts"
        contracts_dir.mkdir(parents=True, exist_ok=True)
        (contracts_dir / f"{safe_id(step_id)}.json").write_text(
            json.dumps(step["contracts"], ensure_ascii=False, indent=2), encoding="utf-8")

        complete_green(step_id, step["goal"], attempt, step.get("design", ""))
        print(f"\nmodule {step_id}: GREEN in {attempt} attempt(s)")
        return 0

    except Halt as halt:
        # テストやビルドの出力に、箱が書き込みを拒んだ跡があれば、設計の問題ではない。
        check_infra(halt.phase, f"{halt.reason}\n{halt.detail}")
        fail_module(step, halt, attempt, last_run)
        return 2


# --------------------------------------------------------------------------
# 外側のループ（RUNNER_SPEC 8-1、`run --all`）
# --------------------------------------------------------------------------


# 外側のループを止めるもの。どれもリポジトリか台帳についての客観的な事実で、
# うまくいっているかについての判断は1つも無い。止まる条件は前もって固定する。
# 「迷ったら訊く」は訊く頻度をモデルに委ね、習慣ですべてを承認するところで終わる。
BUDGET_SPENT = "the wall-clock budget is spent"
INFRA_REFUSED = "the sandbox refused a write"


def cmd_run_all(budget_minutes: int = 0) -> int:
    """依存の順に、緑でないモジュールを走らせる。

    緑なら次へ進む。止まったら run_step が中段に戻しているので、そこで終わる。
    直すモジュールを選ぶのは人だ。全部が緑なら、最終確認の段へ移す。
    """
    stage = read_stage()
    refused = in_stage(stage, "downstream")
    if refused:
        return refuse(refused)
    deadline = time.monotonic() + budget_minutes * 60 if budget_minutes else None

    def stop(reason: str, code: int) -> int:
        ledger("RUN_ALL_STOP", reason=reason)
        print(f"\nstopped: {reason}", file=sys.stderr)
        return code

    tasks = json.loads(TASKS.read_text(encoding="utf-8"))
    load_settings({**tasks, **read_settings()})
    ledger("RUN_ALL_START", budget_minutes=budget_minutes or "none")

    while True:
        done = green_steps()
        remaining = [s for s in tasks["steps"] if s["id"] not in done]
        if not remaining:
            complete_run(done, tasks)
            print(f"\nall {len(done)} module(s) green")
            return 0

        step_id = remaining[0]["id"]
        if deadline is not None and time.monotonic() > deadline:
            return stop(f"{BUDGET_SPENT}; {len(remaining)} module(s) left, next is {step_id}", 2)

        print(f"\n=== {step_id} ({len(done)} done, {len(remaining)} to go) ===")
        try:
            if run_step(step_id) == 0:
                continue
        except InfraBroken as broken:
            print(f"INFRA [{broken.phase}] {broken.evidence}", file=sys.stderr)
            return stop(f"{INFRA_REFUSED} in {step_id}; the design was not sent back", 6)
        return stop(f"{step_id} failed and is back in the midstream; read {FAILURE}", 2)


def cmd_reset(step_id: str) -> int:
    """止まったモジュールを走らせ直せるよう、木を最後の緑に戻す。

    止まったモジュールは、tests/ を凍結したまま、作業ツリーに変更を残し、次の試行は
    始まるのを拒む。手で戻すには chmod、git reset、git clean を正しい順に行う必要が
    ある。間違えやすく、しかも前の試行のファイルを黙って次に持ち込む形で間違える。

    順番に意味がある。chmod の前に引き取る（ランナーは所有しないものを chmod
    できない）。git の前に chmod する（FREEZE は tests/ を読み取り専用にし、git は
    書けないディレクトリの中のファイルを消せない）。テストのコードはコミットしない
    ので、git では消えない。緑でないモジュールのテストは、名指しして消す。

    柵のディレクトリは最後に作り直す。取り込んだブランチでは、柵に追跡された
    ファイルが1つも無いことがある。そのとき `git clean` はソルバーの書いた
    ファイルごと柵を消し、次のモジュールは柵が無いまま始まろうとして落ちる。

    台帳は git の管理下にある（GREEN がコミットする）ので、`reset --hard` は
    台帳を最後の緑まで戻し、捨てようとしている試行の記録を一緒に消す。だから
    読んでおいて書き戻す。
    """
    clean_tree()
    ledger("RESET", step=step_id, note="tree restored to HEAD; the tests and the freeze "
                                       "manifest of every module that is not green discarded")
    print(f"module {step_id}: reset to {run(['git', 'log', '--oneline', '-1']).stdout.strip()}")
    return 0


# solver が読めてはならないディレクトリ。設計と受け入れ条件、台帳、凍結の
# マニフェストと契約、git の履歴。solver は1モジュール分の情報をブリーフで受け取り、
# それ以上は受け取らない。それは、このモードについての主張だ。
PRIVATE_DIRS = (PLAN, STATE, DOCS, AC, PROJECT / ".git")


def fence_is_open() -> bool:
    """runner 以外が非公開のディレクトリを読めるなら、走るのを拒む。

    柵が開いたままなのを見つけたので、これがある。`20-layout.sh` が plan/ と
    .runner/ を 0755 で作り、`40-perms.sh` が後から 0700 に締めていたので、
    モードが正しいのは両方が順に走ったときだけだった。新しい題材のために後者を
    流さずにプロジェクトを作り直し、10ステップのあいだ、受け入れ条件のすべて、
    仕様の全体、凍結した契約のすべてが solver から読める状態で走った。

    プロビジョニングのスクリプトは構築時に1回走るだけで、それに気づけない。
    ランナーは毎回走るので、確認はここに置く。

    直さずに拒む。ランナーはこれらのディレクトリを所有しており、自分で chmod
    できる。しかしモードが誤っているなら、前の走行ですでに情報が読めたかもしれ
    ない。それはその走行の価値についての事実で、黙って片付けるのではなく、
    人間を止めるべきだ。
    """
    open_dirs = []
    for path in PRIVATE_DIRS:
        if not path.exists():
            continue
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            open_dirs.append(f"  {path} is {mode:04o}, expected 0700")
    if not open_dirs:
        return False

    print("refusing to run: the solver can read what it must not.", file=sys.stderr)
    print("\n".join(open_dirs), file=sys.stderr)
    print("\nFix with:  chmod 700 " + " ".join(str(p) for p in PRIVATE_DIRS),
          file=sys.stderr)
    print("Then consider what earlier runs were measured under.", file=sys.stderr)
    return True


def ensure_private_dirs() -> None:
    """非公開のディレクトリのうち、まだ無いものを 0700 で作る。

    docs/ と ac/ は最初の段で初めて要る。umask 002 のまま mkdir すると 0775 に
    なり、次の走行を fence_is_open が拒む。
    """
    if not PROJECT.is_dir():
        return
    for path in (PLAN, DOCS, AC):
        if not path.exists():
            path.mkdir(mode=0o700)
            path.chmod(0o700)


def main() -> int:
    # 柵の中に runner が作るディレクトリを、グループ solverw で書けるようにする。
    # 走行の systemd-run も dash の sudo も umask を 022 にし、git が作り直した
    # src/Managers が 2755 になって、ソルバーの Write がすべて EACCES で落ちた。
    os.umask(0o002)
    parser = argparse.ArgumentParser(description="loop runner v1")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # 上流。仕様書を取り込み、基本設計を書かせ、人が承認する。
    spec_cmd = sub.add_parser("spec", help="the specification the human wrote")
    spec_sub = spec_cmd.add_subparsers(dest="sub_cmd", required=True)
    spec_sub.add_parser("import", help=f"copy {HUMAN_SPEC} into docs/spec/ and commit it; "
                                       f"the first import starts the upstream stage")
    basic_cmd = sub.add_parser("basic", help="the basic design (upstream)")
    basic_sub = basic_cmd.add_subparsers(dest="sub_cmd", required=True)
    draft_cmd = basic_sub.add_parser(
        "draft", help=f"have the planner write docs/basic/ from the specification, "
                      f"taking the human's comment in {HUMAN_COMMENT} into account")
    draft_cmd.add_argument("--language", default=None, choices=sorted(LANGUAGES),
                           help="the language the code is written in; required the "
                                "first time, and fixed after that")
    draft_cmd.add_argument("--framework", default=None,
                           choices=sorted({name for names in FRAMEWORKS.values()
                                           for name in names}),
                           help="where the code is taken to (unity, dxlib, ...); it must "
                                "belong to --language (default: the language's own)")
    basic_sub.add_parser("approve", help="approve the basic design and move to the midstream")

    # 中段。詳細設計を書かせ、クリティックに1回読ませ、人が承認する。
    modules_cmd = sub.add_parser("modules", help="the module design (midstream)")
    modules_sub = modules_cmd.add_subparsers(dest="sub_cmd", required=True)
    modules_sub.add_parser(
        "draft", help="have the planner write docs/modules/ and ac/, check the "
                      "interfaces, and have the critic read the design once")
    modules_sub.add_parser("critique", help="have the critic read the current design once")
    modules_sub.add_parser("approve", help="approve the module design and move downstream")
    reopen_cmd = sub.add_parser(
        "reopen", help="send modules back to the midstream; only their design may change")
    reopen_cmd.add_argument("modules", nargs="+", metavar="MODULE",
                            help="the module, as its path under src without the extension")
    sub.add_parser("check", help="check the design on disk without calling anyone")
    sub.add_parser("status", help="print the stage and the state of every module")

    # 下流。
    run_cmd = sub.add_parser("run", help="build one module, or every module with --all")
    run_cmd.add_argument("step_id", nargs="?", metavar="MODULE",
                         help="the module to build; omit it and pass --all instead")
    run_cmd.add_argument("--all", action="store_true",
                         help="build every module that is not green yet, in dependency "
                              "order, until one fails or all are green")
    run_cmd.add_argument("--budget", type=int, default=0, metavar="MINUTES",
                         help="with --all: stop between modules once this much wall "
                              "clock has passed (default: no limit)")
    reset_cmd = sub.add_parser("reset", help="discard a halted module and return to the last green")
    reset_cmd.add_argument("step_id", metavar="MODULE")

    args = parser.parse_args()

    if os.geteuid() == 0:
        print("refusing to run as root: this must run as `runner`", file=sys.stderr)
        return 1

    if fence_is_open():
        return 1
    ensure_private_dirs()

    # どのコマンドの中の作業かを、いまの作業の説明に使う。終わるときは、どの
    # 経路で終わっても「何もしていない」に戻す。
    NOW["command"] = " ".join(v for v in (args.cmd, getattr(args, "sub_cmd", None),
                                          "--all" if getattr(args, "all", False) else None)
                              if v)
    atexit.register(report_now, None)

    try:
        if args.cmd == "spec":
            return cmd_spec_import()
        if args.cmd == "basic":
            if args.sub_cmd == "draft":
                return cmd_basic_draft(args.language, args.framework)
            return cmd_basic_approve()
        if args.cmd == "modules":
            if args.sub_cmd == "draft":
                return cmd_modules_draft()
            if args.sub_cmd == "critique":
                return cmd_modules_critique()
            return cmd_modules_approve()
        if args.cmd == "reopen":
            return cmd_reopen(args.modules)
        if args.cmd == "check":
            return cmd_check()
        if args.cmd == "status":
            return cmd_status()
        if args.cmd == "reset":
            return cmd_reset(args.step_id)
        if args.all:
            if args.step_id:
                print("run takes a module or --all, not both", file=sys.stderr)
                return 1
            return cmd_run_all(budget_minutes=args.budget)
        if not args.step_id:
            print("run needs a module, or --all", file=sys.stderr)
            return 1
        refused = in_stage(read_stage(), "downstream")
        if refused:
            return refuse(refused)
        return run_step(args.step_id)
    except Halt as halt:
        print(f"HALT [{halt.phase}] {halt.reason}\n{halt.detail}", file=sys.stderr)
        return 2
    except QuotaExhausted as quota:
        # 設計にもソルバーにも落ち度は無く、枠が戻れば同じところからやり直せる。
        waited = QUOTA["waits"] * QUOTA["wait_seconds"] // 60
        print(f"QUOTA [{quota.phase}] {quota.who} is still out of quota ({quota.kind}) "
              f"after waiting {waited} minutes. The design was not sent back.\n"
              f"When the limit has reset: if a module was in progress, run "
              f"`reset <module>` first, then run the same command again.",
              file=sys.stderr)
        return 5
    except InfraBroken as broken:
        # ソルバーにも設計にも直せないので、中段に戻さない。
        print(f"INFRA [{broken.phase}] the sandbox refused a write. The design was not "
              f"sent back.\n  {broken.evidence}\n"
              f"Fix the modes, then run `reset <module>` and the same command again.",
              file=sys.stderr)
        return 6
    except FileNotFoundError as missing:
        # ほぼ必ず、下流の計画がまだ無い。トレースバックを出さず、そう言う。
        print(f"missing: {missing.filename}", file=sys.stderr)
        if str(missing.filename or "").endswith("tasks.json"):
            print("there is no approved module design yet. Run: modules approve",
                  file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
