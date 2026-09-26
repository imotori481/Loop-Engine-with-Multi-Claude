#!/usr/bin/env python3
"""ループのランナー。v1 で、捨てる前提で書いてある。

    sudo -u runner python3 /srv/loop/runner/loop.py run <step-id>

RUNNER_SPEC が述べる、関門を強制する層。エージェントではなく普通の
プログラムで、そこが要点だ。関門に価値があるのは、判断をしないからだ。
失敗が「惜しい」かどうかを決めるものは、ここには何も無い。

位相（RUNNER_SPEC 3 章）:

    PLAN_LOAD -> TEST_WRITE -> STUB -> RED_GATE -> FREEZE -> IMPL <-> VERIFY -> GREEN

REVIEW_GATE は v1 では実装していない。人間の手順で、最初の通し実行を止めて
しまうからだ。その不在は暗黙にせず、毎ステップ台帳に書く。レビューなしで出た
緑を、レビューを通った緑と取り違えないためだ。

標準ライブラリだけを使う。`runner` として動き、runner はリポジトリを所有し、
エージェントを起動するための sudo の例外を持つ。
"""

from __future__ import annotations

import argparse
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
TESTS = PROJECT / "tests"
SRC = PROJECT / "src"
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

# 人間の経路で、プランナーには書けない。プロジェクトを始める要件がここに届き、
# 後にはエスカレーションへの答えも届く。どちらも人だけが言ってよいことだ。
# /srv/loop/planner/out と対になっていて、形は同じ、向きは逆、グループは別。
HUMAN_IN = LOOP / "human" / "in"
REQUIREMENTS = HUMAN_IN / "REQUIREMENTS.md"

# プランナーが書いてよい3つのファイル（BOOTSTRAP「書いてよいのは次の3つだけ」）。
# out/ に書く名前から、プロジェクトの中の置き場への対応表にしてある。この表が
# 許可リストだ。4つ目の項目が無いので、提案が4つ目のファイルを名指しすることは
# できない。
#
# SYSTEM_SPEC.md はリポジトリの根ではなく plan/ の下に置く。BOOTSTRAP の
# ファイル表からの逸脱で、意図したものだ。根は solver から読める（そこで作業
# するので読めなければならない）。仕様が根にあれば、ブリーフの横に2つ目の入力
# 経路ができ、契約を1つ渡されたステップの中からシステム全体の設計が読めてしまう。
# plan/ は 0700 runner なので、そこに置けば何も失わずにその経路を閉じられる。
SYSTEM_SPEC = PLAN / "SYSTEM_SPEC.md"
PROPOSAL_FILES = {
    "SYSTEM_SPEC.md": SYSTEM_SPEC,
    "CONTEXT.md": PLAN / "CONTEXT.md",
    "tasks.json": PLAN / "tasks.json",
}
# ……そして、プランナーが書いてよいが、どこにも適用されない唯一のもの。
# 「これは (b) か (c) なので、自分が決めることではない」と言うための手段だ。
ESCALATE_NAME = "ESCALATE.md"

LEDGER = PLAN / "ledger.jsonl"
ESCALATION = PLAN / "ESCALATION.md"
# プランナーの「自分が決めることではない」が、プロジェクトに届いた後の置き場。
# `planner/out/` はリポジトリの外にあるので、そこに書かれたものはどこにも
# コミットされず、どこにも写されず、たまたま `plan apply` を走らせた端末にしか
# 見えない。2つのファイルを分けるのは、意味も答え方も違うからだ。ESCALATION.md
# は `plan propose` が答える問いで、片方が黙ってもう片方になってはならない。
PLANNER_ESCALATION = PLAN / "PLANNER_ESCALATION.md"
# plan refine の途中でプランナーがエスカレーションしたときの控え。提案は
# 改訂前に戻すので、エスカレーションの本文はここにしか残らない。
REFINE_ESCALATION = STATE / "refine-escalation.md"

# tests/ はプロジェクトの venv の pytest でだけ走らせる。素の
# `python3 -m pytest` にすると、solver 自身の ~/.local のパッケージが sys.path に
# 戻る。「詰まったら pip install」を効かなくしている唯一の仕組みがそれだ
# （RUNNER_SPEC 1-3）。
PYTEST_ARGS = ["-q", "-p", "no:cacheprovider", "--strict-markers"]

# RUNNER_SPEC 11 章が未決にしている値。出発点としての固定値で、どれも
# tasks.json の最上位の "timeouts" で計画ごとに上書きできる。
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
# 当たるのは計画の失敗でもソルバーの失敗でもない。それを Halt にすると
# エスカレーションになり、エスカレーションはプランナーを呼んで、同じ枠を
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
    """待ち切っても枠が戻らなかった。Halt ではないので、エスカレーションしない。"""

    def __init__(self, who: str, phase: str, kind: str):
        super().__init__(f"{who} is still out of quota ({kind}) in {phase}")
        self.who = who
        self.phase = phase
        self.kind = kind

# RUNNER_SPEC 6-2。BOOTSTRAP 1-4 はステップの中の「試行」に上限を掛けるが、
# その外のループについては何も言わない。だからプランナーが (a) を繰り返し答える
# ことに上限が無い。そして (a) は、プランナーが許された唯一の答えなので、
# 何度でも手を伸ばす答えだ。
#
# 仕様の規則: ステップの2回目のエスカレーションからは (a) は選べず、(b) か (c)
# だけが残り、どちらも人間のものだ。だからプランナーが答えるエスカレーションは
# ステップごとにちょうど1回。上限と同じ理由で、最上位の "limits" で計画ごとに
# 上書きできる。
#
# "revisions" は、別の失敗に対する別の上限だ。プランナーはリンタの判定を渡されて
# 直すよう頼まれる。ある違反を別の違反と取り替えるエージェントは、上限が無ければ
# それを永遠に続ける。3 は、配置を誤解しただけの計画が収束するには十分で、
# 規則を満たせない計画がお金を使い続けないだけ少ない。
# "attempts" は全ステップの max_attempts を計画全体で上書きする値で、0 は
# 「各ステップのままにする」を意味する。適切な回数は「誰が試行の代金を払うか」で
# 決まり、プランナーは計画を書く時点でそれを知りえないので、この口がある。
# サブスクリプションのソルバーなら 3 が正しく、ローカルのソルバーなら試行の
# 原価は実時間だけなので、10回でも、エスカレーションが買うプランナー呼び出し
# 1回より安い。
LIMITS = {"escalations": 1, "revisions": 3, "attempts": 0, "critiques": 2,
          "test_writes": 3}

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
# 計画ごと（"policy"）とステップごと（"retry"）に上書きできる。
POLICY = {"retry": "repair"}
RETRY_MODES = ("repair", "resample")

# 試すソルバーのバックエンドを、順に並べる。次の段がそのステップを初めて見る
# 前に、各段は試行を全部使う。ランナーは名前を solver-run に渡し、実際の
# コマンドへの対応は solver-run が持つ。だからこれは、ランナーや計画が何を
# 走らせるかを選ぶ手段ではない。
#
# 普通は1つ。2つにすると、安いかローカルのバックエンドが仕事をし、枠の限られた
# ほうはそれが終えられなかったステップにだけ使う。順番は見た目以上に大事だ。
# 使い切った先にあるのはエスカレーションで、それはプランナーの呼び出しを1回
# 使う。だから2段目のソルバーは余計な出費ではなく、次に起こりうる2つのうち
# 安いほうだ。
#
# 既定値は計画ではなく箱の性質なので、tasks.json ではなくここに置く。
# BOOTSTRAP はプランナーに「誰が実装するかは関知しない」と伝えている。
# だからブートストラップした計画は solver_tiers を持たず、この既定値で回る。
# 既定は Claude Code だけ。local と codex は、計画が solver_tiers で名前を
# 挙げたときだけ使う。
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
    計画の問題ではない。run 8 の S10 では、残り1回の試行を使わずにエスカレーション
    になり、上限を使い切って人間で止まった。
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
            for child in source_files(path):
                child.chmod(0o664)
        else:
            path.chmod(0o2755)
            for child in source_files(path):
                child.chmod(0o444)


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


# plan/ と .runner/ は 0700 runner。solver はそこに書けないので、その下の変更は
# 定義上ランナー自身のものだ。いま追記した台帳、いま作った junit のレポート。
# それを solver の書き込みに数えると、どのステップも自分の帳簿付けで落ちる。
RUNNER_OWNED = ("plan/", ".runner/")


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


def assert_written(phase: str, required: list[str]) -> None:
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
        raise Halt(phase, "the files this phase had to produce are missing or empty",
                   "expected: " + ", ".join(missing))


def assert_touched(phase: str, allowed: list[str]) -> None:
    """solver が書いたのは、ステップが許した場所だけであること。

    これがしないことに注意する。solver に何を変えたかは訊かない。その問いには
    git が答え、solver は git に届かない（.git は 0700 runner）。
    """
    allowed_set = set(allowed)
    actual = touched_paths()
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
# 書き込みの柵、台帳、エスカレーションの規則、L14 の語彙以外の全リンタ規則。
# 規則はもともと言語に依存していなかった。Python の形をしていたのは表現だけだ。
#
# 素の JavaScript ではなく TypeScript にした理由は L14 にある。契約は次の
# ステップに渡すものの形を述べなければならず、型の構文を持たない言語では
# できない。vitest は .ts を esbuild で変換し、別のビルド手順も tsc も無い。
# 型は「読まれる」ためにある。契約を書くプランナーと、それを読むソルバーが読む。
LANGUAGES = {
    "python": {
        "label": "Python",
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
        "layout_note": """Put the package inside src/, e.g. `src/yourpkg/models.py`, and import it as
`from yourpkg.models import Thing` -- `src` is on sys.path, so the `src.`
prefix does not appear in imports. Say this in CONTEXT.md; the solver has no
other way to learn it.""",
    },
    "typescript": {
        "label": "TypeScript",
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
        "layout_note": """Every source file is `.ts` under src/, e.g. `src/idlegame/models.ts`, and
every test file is `.ts` under tests/. Import with a RELATIVE path and no
extension in the specifier is wrong here -- write the extension:
`import { Thing } from "../src/idlegame/models.ts"`. vitest resolves it and
esbuild strips the types; there is no build step and no tsc, so a type is
something the next step READS, not something a compiler checks.

THE PAGE ALREADY EXISTS AND YOU DO NOT WRITE IT. `index.html` sits at the
repository root, which is outside the write fence, so it belongs to the
environment rather than to any step. It is four lines and it does exactly one
thing:

    import { start } from "/src/main.ts";
    start(document.getElementById("app"));

So the plan MUST end with a step whose files_write includes `src/main.ts`, and
that module MUST export `start(root: HTMLElement): void`. Nothing else about
the page is yours to decide. Everything `start` does is ordinary code under
src/: it is under the fence, the tests can reach it, and its criteria are
written against what it puts in the document -- what the element contains,
which buttons exist, which of them are disabled, and what changes when one is
clicked.

Say all of this in CONTEXT.md; the solver has no other way to learn it.""",
    },
}

# tasks.json の最上位の "language" で選ぶ。既定は Python。これができる前に
# 書かれた計画は、すべて Python を前提にしているからだ。
LANGUAGE = dict(LANGUAGES["python"])

# pytest は失敗の本文を必ず "<file>:<line>: <ExceptionName>" で終える。例外の
# クラスが実際に読めるのはその最後の行だ。failure_kind を参照。
# vitest は変換エラーに色を付け、そのエスケープコードのせいで、引用した先
# （ブリーフ、エスカレーション、台帳）のどこでも文が読めなくなる。
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


def test_argv(files_test: list[str], xml_path: Path) -> tuple[list[str], dict[str, str]]:
    """判定を出すコマンドと、それに要る環境変数。

    走らせる部分と分けてあるので、テストは実行せずに何が実行されるかを確かめ
    られる。agent_command があるのと同じ理由だ。
    """
    if LANGUAGE["source_suffix"] == ".ts":
        # `watch` ではなく `run`。vitest の既定は対話的で、永遠に止まったランナーは
        # 終わらないステップとまったく同じに見える。
        # 実行ファイルは npx ではなく、凍結したツールチェーンから絶対パスで取る。
        # npx は取ってくることも厭わないからだ。
        return ([str(VITEST), "run", *files_test,
                 "--reporter=junit", f"--outputFile={xml_path}"],
                {"CI": "1", "NO_COLOR": "1"})
    # バイトコードは作らない。.pyc は書いた者の所有になり、tests/ の下に solver
    # 所有のものがあると、ランナーはそこのモードを当て直せなくなる。
    return ([str(PYTEST), *files_test, *PYTEST_ARGS, "--junitxml", str(xml_path)],
            {"PYTHONDONTWRITEBYTECODE": "1"})


def pytest_run(tag: str, files_test: list[str]) -> TestRun:
    """RUNNER_SPEC 4 章: 判定は終了コードではなく、必ず junit の XML から読む。

    渡すものは関門ごとに違い、それは意図したものだ。RED_GATE にはこのステップの
    テストファイルだけを渡す。その仕事は、これらのテストがこの実装が無いために
    落ちることを確かめることで、スイートの残りまで数えると R1 が意味を失う。
    VERIFY には tests/ 全体を渡す。その仕事はもう半分、これらがいま通り、かつ
    通っていたものが1つも落ちていないことを確かめることだからだ。"""
    STATE.mkdir(parents=True, exist_ok=True)
    xml_path = STATE / f"pytest-{tag}.xml"
    argv, env = test_argv(files_test, xml_path)
    # vitest は既にあるレポートに追記するので、前の試行の古いレポートが今回の
    # 分と一緒に数えられてしまう。
    xml_path.unlink(missing_ok=True)
    try:
        proc = run(argv, env=env, timeout=TIMEOUTS["test"])
    except subprocess.TimeoutExpired:
        # 失敗ではなくエラーとして報告する。実際そうで、スイートは判定をまったく
        # 出していない。RED_GATE では R2 がそのまま拒み、VERIFY は失敗した試行と
        # 数えて、その理由をソルバーに伝える。
        seconds = TIMEOUTS["test"]
        return TestRun(0, 0, 1, 0, [f"<timeout: no verdict after {seconds}s>"], [],
                       f"The test run did not terminate within {seconds}s. The most "
                       f"likely cause is a loop in the implementation that never exits.")
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
        # 最初のエスカレーションは、まさにそういう見た目だった。
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
    """
    usage = data.get("usage") or {}
    models = sorted(data.get("modelUsage") or {})
    tokens_in = sum(int(usage.get(k) or 0) for k in (
        "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    tokens_out = int(usage.get("output_tokens") or 0)
    seconds = (data.get("duration_ms") or 0) / 1000
    cost = data.get("total_cost_usd")
    echo = (f"who={who} phase={phase} model={','.join(models) or '-'} "
            f"in={tokens_in} out={tokens_out} sec={seconds:.0f}"
            + (f" usd={cost:.2f}" if isinstance(cost, (int, float)) else "")
            + (" ERROR" if data.get("is_error") else ""))
    ledger("USAGE", echo=echo, who=who, phase=phase,
           models=models,
           usage=data.get("usage"),
           cost_usd=cost,
           duration_ms=data.get("duration_ms"),
           turns=data.get("num_turns"),
           is_error=data.get("is_error"))


def run_agent(who: str, phase: str, invoke) -> subprocess.CompletedProcess:
    """エージェントを1回呼ぶ。枠が無くて失敗したときだけ、待ってやり直す。

    それ以外の結果は、成功も失敗もそのまま返す。判断は呼び出し側に残す。
    TimeoutExpired もそのまま上に通す。出力が JSON なら、消費量を台帳に残し、
    結果の文だけを返す。
    """
    for round_no in range(1, QUOTA["waits"] + 2):
        proc, data = unwrap_result(invoke())
        if data is not None:
            record_usage(who, phase, data)
        kind = quota_problem(proc.stdout + proc.stderr) if proc.returncode != 0 else None
        if kind is None:
            return proc
        if round_no > QUOTA["waits"]:
            break
        ledger("QUOTA_WAIT", who=who, phase=phase, kind=kind, round=round_no,
               of=QUOTA["waits"], seconds=QUOTA["wait_seconds"])
        time.sleep(QUOTA["wait_seconds"])
    ledger("QUOTA_EXHAUSTED", who=who, phase=phase, kind=kind)
    raise QuotaExhausted(who, phase, kind)


def call_solver(phase: str, brief: str, backend: str | None = None) -> str:
    """ソルバーにブリーフを1つだけ渡し、ほかには何も渡さない。

    ブリーフは /srv/loop/brief（runner:solverw 0750）に書く。ソルバーは読めるが
    書けず、存在する経路はそれだけだ。ここから 0700 runner の plan/ への道は
    無い。だから「ソルバーは tasks.json を読んではならない」は、誰かが守る必要の
    ある規則ではない。
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


def dep_contract_lines(step: dict) -> list[str]:
    """このステップが依存するすべてのステップの provides の行を、平らに並べる。

    dep_contracts は同じものをブリーフ用に描く。そこではステップごとのまとまりに
    価値がある。スタブの生成に要るのは名前と置き場だけなので、行で渡す。
    """
    lines: list[str] = []
    for dep in step.get("depends_on", []):
        path = STATE / "contracts" / f"{dep}.json"
        if not path.exists():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        provides = value.get("provides") if isinstance(value, dict) else value
        if isinstance(provides, list):
            lines.extend(str(p) for p in provides)
    return lines


def dep_contracts(step: dict) -> str:
    parts = []
    for dep in step.get("depends_on", []):
        path = STATE / "contracts" / f"{dep}.json"
        if not path.exists():
            raise Halt("PLAN_LOAD", f"step {step['id']} depends on {dep}, which has no contract yet")
        parts.append(f"From {dep}:\n{path.read_text(encoding='utf-8')}")
    return "\n\n".join(parts) if parts else "(none -- this step depends on nothing)"


def render_acceptance(step: dict) -> str:
    return "\n".join(
        f"{i}. [{a['case']}] given {a['given']} then {a['then']}"
        for i, a in enumerate(step["acceptance"], 1)
    )


def render_provides(step: dict) -> str:
    return "\n".join(step["contracts"]["provides"])


def render_invariants(step: dict) -> str:
    inv = step["contracts"].get("invariants") or []
    return "\n".join(f"- {i}" for i in inv) if inv else "(none stated)"


def brief_test_write(step: dict, context: str, broken: str = "") -> str:
    # goal は渡さない。テストは受け入れ条件から作るもので、これからソルバーに
    # 頼む実装の説明から作るものではない。
    acceptance = render_acceptance(step)
    return f"""Write tests. Do not write an implementation.

# Project context
{context}

# Contracts you may rely on
{dep_contracts(step)}

# Invariants that must hold
{render_invariants(step)}

# Acceptance criteria -- write exactly {step["expected_tests"]} tests, at least one per criterion
{acceptance}

# Signatures under test
{render_provides(step)}

# Files you may create or modify
{chr(10).join(step["files_test"])}

Write nothing outside those paths. The implementation does not exist yet, so
every test you write must fail when run against a stub that returns a wrong
value of the right type. Do not weaken a test to make it pass, and do not
create the module under test.
{naming_note()}{compile_failure_section(broken)}"""


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


def naming_note() -> str:
    """名指しする価値のある罠を1つ伝える。条件そのものが作る罠だからだ。

    上の条件は英語の文で、英語の文にはアポストロフィがある。それをテスト名に
    写すのは自然なことで、単一引用符の文字列の中ではそこで文字列が終わる。run 8 の
    S1 はテストを12件書き、1件も走らなかった:
    `it('... then the result's resource is exactly 1',`。

    ファイルはコンパイルできず、vitest はそれをファイル名を持つ合成の失敗テスト
    1件として報告した。ランナーは「1件、期待は12件」と見て、構文エラーを
    プランナーに送るところだった。報告の側も直してあるが、ブリーフで取り除ける
    罠は取り除いたほうがよい。
    """
    if LANGUAGE["source_suffix"] != ".ts":
        return ""
    return """
Name your tests with DOUBLE quotes or backticks, never single quotes. The
criteria above are prose and contain apostrophes ("the result's resource"),
and one of those inside a single-quoted name ends the string: the file stops
compiling and not one of your tests runs.
"""


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


def generate_stub(step: dict, requires: list[str]) -> dict[str, str] | None:
    """スタブ全体を返す。None なら、やはりソルバーに頼む。

    None は恥ずべき失敗ではない。これが言語の2つ目の、より劣った構文解析器に
    ならずに済むのは None のおかげだ。見慣れないものは、前から扱っていた経路に
    戻す。
    """
    if LANGUAGE["source_suffix"] != ".ts":
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
    inherited = parse_contracts(requires) or []
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
        needed: dict[str, set] = {}

        for d in mine:
            if d["kind"] in ("interface", "type"):
                # interface の本体の後にセミコロンは要らず、型の別名には要る。
                # `}` の後に書いても正しいが、人が開くかもしれないファイルでは
                # 読みにくい。
                line = f"export {d['kind']} {d['name']}{d['rest']}".rstrip().rstrip(";")
                body.append(line if line.endswith("}") else line + ";")
                continue
            if d["kind"] == "const":
                kind = d["rest"].split(":", 1)[-1].strip() if ":" in d["rest"] else "unknown"
                body.append(f"export const {d['name']}: {kind} = "
                            f"{sentinel_for(kind, types, keys)};")
                continue
            signature = TS_SIGNATURE.match(d["rest"].strip())
            if signature is None:
                return None
            returns = signature.group("returns")
            value = sentinel_for(returns, types, keys)
            body.append(
                f"export function {d['name']}({signature.group('args')}): {returns} {{"
                + (f"{chr(10)}  return {value};{chr(10)}}}" if value else f"{chr(10)}}}"))

        # import: このファイルが口にし、別のファイルが宣言している名前すべて。
        text = chr(10).join(body)
        for name, source in elsewhere.items():
            if name in declared_here or not re.search(rf"(?<!\w){name}(?!\w)", text):
                continue
            needed.setdefault(relative_module(path, source), set()).add(name)
        for d in declarations:
            if d["file"] == path or d["name"] in declared_here:
                continue
            if re.search(rf"(?<!\w){d['name']}(?!\w)", text):
                needed.setdefault(relative_module(path, d["file"]), set()).add(d["name"])

        imports = [f'import {{ {", ".join(sorted(names))} }} from "{module}";'
                   for module, names in sorted(needed.items())]
        files[path] = (chr(10).join(imports) + chr(10) * 2 if imports else "") \
            + text + chr(10)
    return files


def relative_module(importer: str, target: str) -> str:
    """`importer` が `target` をどう指すか。TypeScript が求める書き方で返す。"""
    rel = os.path.relpath(Path(target).with_suffix("").as_posix(),
                          Path(importer).parent.as_posix()).replace(os.sep, "/")
    return rel if rel.startswith(".") else "./" + rel


def brief_stub(step: dict) -> str:
    # 署名だけを渡す。goal も acceptance も invariants も渡さない。形ではなく意味を
    # 運ぶものがここにあると、それは忠実に実装され、それが覆う条件は一度も落ちる
    # のを見られないまま RED_GATE を通る（2026-08-18 にステップ S1 で痛い目を見て
    # 分かった）。
    return f"""Create stubs only.

# Signatures to provide
{render_provides(step)}

# Files you may create or modify
{chr(10).join(step["files_write"])}

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


def brief_impl(step: dict, context: str, tests_text: str, last_failure: str) -> str:
    return f"""Make the tests pass.

# Project context
{context}

# Goal
{step["goal"]}

# Signatures you must provide
{render_provides(step)}

# Invariants that must hold
{render_invariants(step)}

# Contracts you may rely on
{dep_contracts(step)}

# The tests (frozen -- read-only, and they will not be accepted if modified)
{tests_text}

# How the tests are failing right now
{last_failure or "(nothing recorded)"}

# Files you may create or modify
{chr(10).join(step["files_write"])}

Change nothing outside those paths. The tests are the specification: if a test
looks wrong, say so in your final message rather than editing it -- editing it
will be detected and the step will stop.
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
# エスカレーション
# --------------------------------------------------------------------------


def escalation_count(step_id: str) -> int:
    """このステップがすでに何回エスカレーションしたか。台帳から読む。

    メモリの中の数ではなく台帳から数える。上限の要点は、プロセスを終わらせる
    出来事（VM の停止、reset、明日再開する走行）を越えて残ることだからだ。
    `reset` が台帳を巻き戻さないのもこのためだ。reset が消す記録から数える上限は、
    上限にならない。
    """
    if not LEDGER.exists():
        return 0
    n = 0
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("event") == "ESCALATED" and rec.get("step") == step_id:
            n += 1
    return n


def escalate(step: dict, halt: Halt, attempt: int, run_: TestRun | None) -> None:
    failed = "\n".join(f"- {k}" for k in (run_.failure_kinds if run_ else [])) or "(none recorded)"
    n = escalation_count(step["id"]) + 1          # 今回の分を含む
    cap = LIMITS["escalations"]

    # RUNNER_SPEC 6-2 は、この制約をファイルそのものに書くことを求めている。読む者が
    # 規則を忘れていても成り立つようにだ。cmd_run_all でも強制しており、そちらは
    # 訊かない。
    if n > cap:
        constraint = f"""- This is escalation {n} of at most {cap} for this step, so **(a) is no
  longer available**. Only (b) rewriting the acceptance criteria, or (c)
  rebuilding from further upstream -- and both of those are the human's
  decision, not the planner's (RUNNER_SPEC 6-2).
- If (c) is chosen, name every green step that has to be discarded."""
    else:
        constraint = f"""- The acceptance criteria for this step must NOT be weakened.
- This is escalation {n} of at most {cap} for this step. Permitted responses are
  (a) tighten the goal, or escalate. Rewriting the acceptance criteria is case
  (b) and is a decision for the human, not the planner (RUNNER_SPEC 6-2)."""

    ESCALATION.write_text(
        f"""# ESCALATION: step {step["id"]}

## Facts
- phase: {halt.phase}
- reason: {halt.reason}
- attempt: {attempt} of {len(attempt_schedule(step))}
- solver backends: {", ".join(SOLVER_TIERS)}
- escalation: {n} of {cap}

## Failing tests
{failed}

## Detail
```
{halt.detail[-4000:]}
```

## Constraints on the planner
{constraint}
""",
        encoding="utf-8",
    )
    ledger("ESCALATED", step=step["id"], phase=halt.phase, reason=halt.reason,
           escalation_no=n)
    print(f"\nESCALATION written to {ESCALATION}", file=sys.stderr)


# --------------------------------------------------------------------------
# ステップ
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 計画のリンタ（RUNNER_SPEC 8 章）
# --------------------------------------------------------------------------

# 契約の行が宣言するものの名前。言語ごとの形を持ち、そうでなければならない
# 理由は、L3 が名前を比べるからだ。「このステップが依存するものの中に、求める
# ものを提供するものはあるか」。
#
# Python では def と class の両方を見る。データモデルを導入するステップは
# `class GameState(...)` を提供し、`def` だけを照合すると、後のステップの
# `requires` がすべて、リンタに見えない名前について L3 で落ちる。実際の計画が
# 初めてデータモデルのステップを持ったときに見つかった。
#
# Python だけの版は、TypeScript で L3 を「満たせない」ものにし、両側が違う
# 落ち方をしたので気づけなかった。provides の側はパターンに合わない行を「捨て」、
# 空の集合を作った。requires の側は行全体に戻り、それをその空の集合から探した。
# requires の項目はすべて、常に違反で、どの計画も答えようがなかった。run 8 の
# bootstrap は、このパターンが見えるまでに試行2回と56分を使った。1回目の
# フィードバックはよくある食い違いに見え、プランナーはそれを同じ失敗へと「直した」。
#
# Python の文法で書いた規則が、有料の試行を使わせたのは、L14 の語彙、L15 の
# 名前の境界に続いて3度目だ。規則は一度も誤っていなかった。誤っていたのは表現だ。
PROVIDES_PATTERNS = {
    ".py": re.compile(r"(?:def|class)" + chr(92) + r"s+([A-Za-z_]" + chr(92) + r"w*)"),
    ".ts": re.compile(
        r"(?:function|class|interface|type|enum|const|let)" + chr(92) + r"s+([A-Za-z_]" + chr(92) + r"w*)"),
}


def declared_name(line: str) -> str:
    """契約の行が宣言する名前。何も宣言していなければ行全体。

    L3 の両側で、名前が取れないときの扱いを同じにしてある。その対称性が修正の
    中身だ。合わない行を `provides` から捨てて `requires` には残すと、規則が
    厳しくなるのではなく、答えようのないものになる。
    """
    pattern = PROVIDES_PATTERNS.get(LANGUAGE["source_suffix"])
    match = pattern.search(line) if pattern else None
    return match.group(1) if match else line.strip()
# L8 の「具体的」: 数、引用符で囲んだリテラル、例外の型。形容詞だけでできた
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

CASES = {"normal", "boundary", "error"}
REQUIRED_KEYS = {
    "id": str, "kind": str, "goal": str, "depends_on": list, "acceptance": list,
    "contracts": dict, "files_write": list, "files_test": list,
    "expected_tests": int, "max_attempts": int, "review_gate": bool,
}


def modules_of(files_write: list[str]) -> list[str]:
    """ステップ自身のファイルが作る、import できるモジュール名。

    src/incgame/engine.py     -> incgame.engine       (Python)
    src/incgame/__init__.py   -> incgame
    src/idlegame/engine.ts    -> idlegame/engine      (TypeScript)
    src/idlegame/index.ts     -> idlegame

    2つの言語は同じ考えを違う綴りで書く。区切り文字と、「ディレクトリそのもの」を
    意味する名前だ。L15 のほかの部分は何も変わらない。
    """
    suffix = LANGUAGE["source_suffix"]
    separator = LANGUAGE["module_separator"]
    index = LANGUAGE["index_name"]
    names = []
    for f in files_write:
        if not f.startswith("src/") or not f.endswith(suffix):
            continue
        parts = f[len("src/"):-len(suffix)].split("/")
        if parts[-1] == index:
            parts = parts[:-1]
        if parts:
            names.append(separator.join(parts))
    return names


def adopt_language(tasks: dict) -> None:
    """判定する計画から言語を取る。

    いくつかの規則は1つの言語の文法で表されているので、計画を判定するとは、その
    計画の言語で判定することだ。それを呼び出し側に任せたら、3回忘れられた。
    `plan apply` は既定の言語で走り、`modules_of` が `.py` を見つけられないせいで
    TypeScript の計画の契約22件をすべて拒んだ。直後に `validate` も同じことをした。
    どちらも正しい計画で、文法が違っていた。

    呼び出し側が毎回設定を覚えておかなければならないグローバル変数は、誰かが
    設定し忘れるグローバル変数だ。答えは計画が持っているので、計画を読む関数が
    それを読む。
    """
    language = tasks.get("language") if isinstance(tasks, dict) else None
    if language in LANGUAGES:
        LANGUAGE.clear()
        LANGUAGE.update(LANGUAGES[language])


def validate_plan(tasks: dict) -> list[str]:
    """違反をすべて返す。プランナーの出力は、従う前に確かめる。計画の側で唯一の
    客観的な関門で、どのモデルも呼ばずに走る。"""
    adopt_language(tasks)
    problems: list[str] = []
    steps = tasks.get("steps")
    if not isinstance(steps, list) or not steps:
        return ["L1: tasks.json has no non-empty `steps` array"]

    # L1 -- 形
    for i, s in enumerate(steps):
        where = f"step[{i}]" if not isinstance(s.get("id"), str) else f"step {s['id']}"
        for key, typ in REQUIRED_KEYS.items():
            if key not in s:
                problems.append(f"L1: {where} is missing `{key}`")
            elif not isinstance(s[key], typ) or (typ is int and isinstance(s[key], bool)):
                problems.append(f"L1: {where}.{key} must be {typ.__name__}")
        if s.get("kind") not in ("skeleton", "unit", "integration"):
            problems.append(f"L1: {where}.kind must be 'skeleton', 'unit' or 'integration'")
        if "retry" in s and s["retry"] not in RETRY_MODES:
            problems.append(f"L1: {where}.retry must be one of {list(RETRY_MODES)}")
        for a in s.get("acceptance", []):
            if not isinstance(a, dict) or {"case", "given", "then"} - a.keys():
                problems.append(f"L1: {where} has an acceptance entry without case/given/then")
            elif a["case"] not in CASES:
                problems.append(f"L1: {where} acceptance case '{a['case']}' is not one of {sorted(CASES)}")
        if not isinstance(s.get("contracts", {}).get("provides"), list):
            problems.append(f"L1: {where}.contracts.provides must be a list of signature strings")

    if problems:
        return problems  # 後の規則は、形が成り立っていることを前提にする

    ids = [s["id"] for s in steps]
    seen: set[str] = set()
    provides_by_step = {
        s["id"]: {declared_name(p) for p in s["contracts"]["provides"]}
        for s in steps
    }

    for s in steps:
        sid = s["id"]

        # L2 -- 依存は前だけを指す。だから循環は起こりえない
        for dep in s["depends_on"]:
            if dep not in seen:
                problems.append(f"L2: step {sid} depends on {dep}, which is not an earlier step")
        seen.add(sid)

        # L3 -- 求めるものはすべて、依存先のどれかが提供している
        available = set().union(*(provides_by_step[d] for d in s["depends_on"] if d in provides_by_step)) \
            if s["depends_on"] else set()
        for req in s["contracts"].get("requires", []):
            name = declared_name(req)
            if name not in available:
                problems.append(f"L3: step {sid} requires `{name}`, which no dependency provides")

        # L5 -- ファイルは書くか試すかのどちらかで、両方にはならない
        overlap = set(s["files_write"]) & set(s["files_test"])
        if overlap:
            problems.append(f"L5: step {sid} lists {sorted(overlap)} in both files_write and files_test")

        # L12 -- 書き込みの柵が覆うのは src/ と tests/ だけ
        #
        # set_writable() が chmod するのはちょうどその2つのディレクトリで、adopt()
        # がたどるのもその2つだ。ほかの場所に書くステップは、まったく柵の中に
        # いない。ワークスペースの根はグループで書ける（ソルバーのパッチの道具が
        # 要る）ので、ソルバーは src/ の隣にパッケージを作り、コードを書けない
        # はずの位相でそこに書ける。assert_touched は後でそれを捕まえるが、それでは
        # 主な仕組みがトリップワイヤになり、そこに作られたファイルは solver の
        # 所有のまま残る。ランナーはそれを chmod も片付けもできない。
        #
        # 最初の計画はふつうこれを誤る。「パッケージはリポジトリの根に置く」は
        # 普通の Python の配置だからだ。だからブリーフの助言ではなく、リンタが
        # 持つ規則にする。
        for f in s["files_write"]:
            if not f.startswith("src/"):
                problems.append(f"L12: step {sid} writes {f}, which is outside src/")
        for f in s["files_test"]:
            if not f.startswith("tests/"):
                problems.append(f"L12: step {sid} tests {f}, which is outside tests/")

        # L14 -- 契約は、渡すものの形を述べる
        #
        # BOOTSTRAP 1-5: 後のステップに渡るのは契約「だけ」だ。`-> tuple` は、
        # 契約からは果たせない契約だ。要素の数がどこにも無いので、呼び出し側は
        # 受け取ったものを分解できず、スタブは何を返せばよいか分からない。
        #
        #     def buy_max_affordable(...) -> tuple      要素の数が無い
        #     stub:  return ("__stub__",)               要素は1つ
        #     test:  result, count = buy_max_affordable(...)
        #            ValueError: not enough values to unpack
        #
        # RED_GATE はそれを正しく止める（R5: 壊れているのは値ではなく呼び出し
        # そのもの）が、その先の何もそれを直せない。ソルバーは STUB のあいだ署名
        # しか見ない（テストを見せると答えを決め打ちできてしまう）。プランナーは
        # エスカレーションを2回使っても直せなかった。欠陥はすでに書いた契約に
        # あり、P5 は緑のステップの編集を許さないからだ。
        #
        # だから規則を、そのどれかが起きる前のここに置く。chmod 777 を見張るの
        # ではなく sudo を取り上げるのと同じ手で、述べようのない契約の後始末を
        # するのではなく、それを書けなくする。
        #
        # これは、言語に依存しない規則「契約が形を決める」を Python の形で
        # 表したものだ。2つ目の言語は自分の型の構文で言い直す。規則そのものは
        # 変わらない。
        for provided in s["contracts"]["provides"]:
            shapeless = LANGUAGE["shapeless"]
            opener = LANGUAGE["shape_bracket"]
            bare = sorted({name for name, bracket in ANNOTATION.findall(provided)
                           if name in shapeless and bracket != opener})
            if bare:
                signature = provided.split(chr(8212))[0].strip()
                problems.append(
                    f"L14: step {sid} provides `{signature}` with "
                    f"{', '.join(bare)} left unparameterised; a later step is "
                    f"handed this line and nothing else, so the contents have to "
                    f"be in it ({LANGUAGE['shape_example']}). "
                    f"A named type is better still where the shape has meaning")

        # L15 -- 契約は、宣言するものの置き場を言う
        #
        # テストを書くためのブリーフには、あえて goal を入れない。テストは実装の
        # 説明からではなく、受け入れ条件から作るものだからだ。入れるのは
        # `provides` だ。だから `provides` が関数の置き場のモジュールを言わないと、
        # モジュール名はブリーフのどこにも無く、ソルバーは推測する:
        #
        #     def new_game(now: float) -> GameState
        #     -> from incgame.game import new_game   （game.py は S10 のもの）
        #     -> collected 1 test, expected 4        （R1。エスカレーションを1回失う）
        #
        # ランナーはモジュールを知っている（files_write に並んでいる）ので、
        # 安上がりに見える直し方はそれをブリーフに足すことだ。それは採らなかった。
        # 4つのファイルを書くステップでは、どの名前がどこにあるかがやはり推測に
        # なり、しかも「後の」ステップにはどれも見えない。dep_contracts は凍結した
        # `provides` の文字列しか描かないので、モジュールを運ばない契約は、後から
        # 一切直せない。その情報は契約の中にあるべきだ。これも 1-5 で、ステップ間の
        # 唯一の経路は、それだけで足りなければならない。
        for provided in s["contracts"]["provides"]:
            boundary = LANGUAGE["name_boundary"]
            if not any(re.search(rf"(?<!{boundary}){re.escape(m)}(?!{boundary})",
                                 provided)
                       for m in modules_of(s["files_write"])):
                signature = provided.split(chr(8212))[0].strip()
                problems.append(
                    f"L15: step {sid} provides `{signature}` without saying which "
                    f"module it lives in; name one of "
                    f"{', '.join(modules_of(s['files_write'])) or '(no modules)'} "
                    f"in the line, e.g. `... -- defined in "
                    f"{(modules_of(s['files_write']) or ['pkg.mod'])[0]}`")

        # L6 -- normal、boundary、error がすべてそろっている
        missing = CASES - {a["case"] for a in s["acceptance"]}
        if missing:
            problems.append(f"L6: step {sid} has no acceptance case of type {sorted(missing)}")

        # L7
        if s["expected_tests"] < len(s["acceptance"]):
            problems.append(
                f"L7: step {sid} expects {s['expected_tests']} tests for "
                f"{len(s['acceptance'])} acceptance criteria")

        # L8 -- 人間のレビューを省くには、機械が確かめられる条件が要る
        #
        # `then` は「単独で」確かめる。`given` とつなげて確かめると、期待する
        # 結果を別の呼び出しとの比較で述べた条件が、前提の中の数のおかげで通る:
        #
        #     given  state = new_game(0.0); p = buy(state, ...)
        #     then   advance(p.state, ..., 0.0) == p.state exactly
        #
        # 両辺ともスタブから出てくるので同じ番兵の値になり、テストは RED_GATE で
        # 「通る」。落ちうることを一度も示さなかったテストのために、R4 がステップを
        # 止める。期待する結果は値でなければならず、テスト対象のコードが作った
        # 2つのものの関係であってはならない。
        if not s["review_gate"]:
            for a in s["acceptance"]:
                if not CONCRETE.search(a["then"]):
                    problems.append(
                        f"L8: step {sid} has review_gate false but the [{a['case']}] criterion "
                        f"states no concrete value")

    # L4 -- 計画全体を通して、ファイルの持ち主は1つ
    owners: dict[str, str] = {}
    for s in steps:
        for f in s["files_write"]:
            if f in owners:
                problems.append(f"L4: {f} is written by both {owners[f]} and {s['id']}")
            owners[f] = s["id"]

    # L9 / L10 -- 早いうちにつながったものがあり、最後につなぐ
    # L9 は、最初の3つのうちに integration か skeleton のステップを求めていた。
    # L13 は「最初の」ステップを skeleton にすることを求め、それは作りの上で L9 を
    # 満たすので、L9 はもう発火しえない。誰も落とせない規則として残さず、
    # 2026-08-21 に廃止した。番号は使い回さない。L の番号は識別子で、過去の
    # 走行の台帳がそれを参照している。
    kinds = [s["kind"] for s in steps]
    if kinds[-1] != "integration":
        problems.append("L10: the final step is not an integration step")

    # L13 -- 計画は walking skeleton から始まる
    #
    # L9 は早いうちに integration のステップを求め、そのコメントはそれが
    # walking skeleton だと言っていた。そうではなかった。ここで言う
    # "integration" はモジュールをつなぐステップというだけで、計画はそれを
    # 満たしつつ（上の規則をすべて満たし、10ステップすべてで緑になり、スイート
    # 全体を通しつつ）、作ったものに初期状態から届かないことがありうる。最初の
    # 実際の計画がまさにそうだった。資源の唯一の出どころが生産で、生産には
    # 発電機が要り、発電機は資源で買うしかないインクリメンタルゲーム。どの
    # ステップにも誤りは無かった。始まりについてのステップが無かった。
    #
    # skeleton のステップは、端から端までの薄い切り口だ。システムの初期状態から、
    # 公開 API を通って、人が製品として動いていると認めるものまで。最初に作り、
    # 薄くする。後のステップがそれを厚くする。
    #
    # この規則が強制できるのは構造だ。計画は skeleton から始まり、ちょうど1つ
    # 持つ。中の条件が誠実かは確かめられない。この規則が買うのは、計画を作る
    # 時点で条件を「書かせる」ことだ。「新しいゲームで、……すると、プレイヤーは
    # ……を持つ」は、最初の資源をどう得るかを決めずには書けない。その抜けは、
    # 後で人間が気づくものから、プランナーが計画中に行き当たるものに変わる。
    #
    # 一度緑になった skeleton は緑のままだ。PASS_TO_PASS が後の VERIFY のたびに
    # そのテストを走らせる。二重ループの TDD の、常設の端から端までのテストに、
    # 反対側からたどり着いた形だ。最後まで赤ではなく最初のステップから緑で、
    # それがこのランナーが実際に強制できる形だ。
    skeletons = [s["id"] for s in steps if s["kind"] == "skeleton"]
    if kinds[0] != "skeleton":
        problems.append(
            f"L13: the first step is kind '{kinds[0]}'; a plan starts with a "
            f"'skeleton' step -- the thin end-to-end slice from the system's "
            f"initial state to something a person would call the product working")
    if len(skeletons) != 1:
        problems.append(
            f"L13: a plan has exactly one skeleton step, not {len(skeletons)}"
            + (f" ({', '.join(skeletons)})" if skeletons else ""))

    # L11 -- 何も使わないものは作らない。integration と skeleton のステップは
    # 除く。そこでは部品をつなぐこと自体が成果物で、後のステップへの入力ではない。
    used = set().union(*[
        {declared_name(r) for r in s["contracts"].get("requires", [])}
        for s in steps
    ]) if steps else set()
    for s in steps:
        if s["kind"] in ("integration", "skeleton"):
            continue
        for name in provides_by_step[s["id"]] - used:
            problems.append(f"L11: step {s['id']} provides `{name}`, which no step requires")

    return problems


def load_plan(step_id: str) -> tuple[dict, str]:
    tasks = json.loads((PLAN / "tasks.json").read_text(encoding="utf-8"))
    steps = {s["id"]: s for s in tasks["steps"]}
    if step_id not in steps:
        raise Halt("PLAN_LOAD", f"no step {step_id} in tasks.json")
    context = (PLAN / "CONTEXT.md").read_text(encoding="utf-8")
    return steps[step_id], context


# --------------------------------------------------------------------------
# プランナーの経路（BOOTSTRAP 1-4、RUNNER_SPEC 2 章）
# --------------------------------------------------------------------------


def canon(obj) -> str:
    """順序に依存しない比較用のキー。tasks.json の字下げを変えても、ステップの
    キーを並べ替えても、変更と読まれてはならない。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def green_steps() -> set[str]:
    """すでに緑になったステップの id。台帳から読む。"""
    done: set[str] = set()
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
            done.add(rec["step"])
    return done


def clear_proposal() -> None:
    """新しい提案を頼む前に out/ を空にする。

    そうしないと、前の呼び出しの残りのファイルが、プランナーがいま書いたものと
    して適用される。ファイルは `planner` のものだが、ランナーはこれができる。
    out/ はスティッキーで、スティッキーはファイルの所有者「または」ディレクトリの
    所有者（ランナー）に削除を許す。
    """
    PLANNER_OUT.mkdir(parents=True, exist_ok=True)
    for entry in PLANNER_OUT.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            raise Halt("PLAN_PROPOSE",
                       f"a directory is in the way in {PLANNER_OUT}: {entry.name}",
                       "Remove it by hand; the runner will not recurse into a "
                       "tree written by another uid.")
        entry.unlink()


def restore_proposal(files: dict[str, str]) -> None:
    """out/ を控えておいた提案に戻す。

    plan refine は改訂を頼む前に out/ を空にする。改訂が失敗したり、プランナーが
    エスカレーションしたりすると、リンタを通っていた改訂前の提案が失われる。
    それを防ぐために、ランナーが控えから書き戻す。書き戻したファイルは runner の
    所有になるが、次の clear_proposal で消せるので支障はない。
    """
    clear_proposal()
    for name, text in files.items():
        path = PLANNER_OUT / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o644)


def call_planner(brief: str) -> str:
    """プランナーにブリーフを1つ渡す。形も理由も call_solver と同じ。

    ブリーフが何を含むかに注意する。受け入れ条件を含む、いまの計画全体だ。それは
    漏れではない。プランナーは条件の「書き手」だ。BOOTSTRAP 1-1 は条件を書く
    アカウントとコードを書くアカウントを分けており、プランナーは `solverw` にも
    `runner` にもいない。plan/ の 0700 が買うのは、プランナーが tasks.json を
    「書けない」ことだ。望む変更はすべて、下の check_proposal を通って戻る。
    """
    PLANNER_BRIEF.mkdir(parents=True, exist_ok=True)
    brief_path = PLANNER_BRIEF / "plan.md"
    brief_path.write_text(brief, encoding="utf-8")
    shutil.chown(brief_path, group="plannerw")
    brief_path.chmod(0o640)

    limit = TIMEOUTS["planner"]
    try:
        # 上限は前提にせず渡す。planner-run の既定は 900 秒なので、ここで決めて
        # 渡さない値は、決して効かない上限になる。
        proc = run_agent("planner", "PLAN_PROPOSE", lambda: run(
            agent_command("planner", PLANNER_RUN, brief_path, limit),
            timeout=limit + BACKSTOP_MARGIN))
    except subprocess.TimeoutExpired:
        raise Halt("PLAN_PROPOSE", f"planner still running after {limit + BACKSTOP_MARGIN}s",
                   "planner-run's internal timeout did not fire; check with: "
                   "pgrep -a -u planner")
    out = proc.stdout + proc.stderr
    if proc.returncode == 124:
        raise Halt("PLAN_PROPOSE", "planner hit its own timeout in planner-run", out[-4000:])
    if proc.returncode != 0:
        raise Halt("PLAN_PROPOSE", f"planner exited {proc.returncode}", out[-4000:])
    return out


def read_proposal() -> dict[str, str]:
    """out/ にあるもの。読む前に、ファイル名の集合として確かめる。

    「変更は3つのファイルに限る」の規則で、差分の検査ではなくファイル名の
    許可リストにしてあるのは意図的だ。4つ目のファイルに触れて、その是非を議論
    しなければならない提案は存在しない。4つ目のファイルの行き先が無いからだ。
    """
    if not PLANNER_OUT.is_dir():
        raise Halt("PLAN_APPLY", f"no proposal directory at {PLANNER_OUT}")
    entries = sorted(PLANNER_OUT.iterdir(), key=lambda p: p.name)
    if not entries:
        raise Halt("PLAN_APPLY", "the planner wrote nothing")

    allowed = set(PROPOSAL_FILES) | {ESCALATE_NAME}
    stray = sorted(e.name for e in entries if e.name not in allowed)
    if stray:
        raise Halt("PLAN_APPLY", "the proposal contains files the planner may not write",
                   "wrote: " + ", ".join(stray) + "\nallowed: " + ", ".join(sorted(allowed)))

    proposal = {}
    for entry in entries:
        if entry.is_symlink() or not entry.is_file():
            raise Halt("PLAN_APPLY", f"{entry.name} is not a regular file",
                       "A symlink here would apply content from outside the "
                       "proposal directory.")
        proposal[entry.name] = entry.read_text(encoding="utf-8")

    if ESCALATE_NAME in proposal and len(proposal) > 1:
        raise Halt("PLAN_APPLY",
                   f"{ESCALATE_NAME} was written alongside a proposal",
                   "Escalating and proposing at the same time is ambiguous: "
                   "either the planner can fix this within its remit or it cannot.")
    return proposal


def check_proposal(old: dict, new: dict) -> list[str]:
    """すでにあるステップについて、プランナーが変えてはならないものすべて。

    BOOTSTRAP 1-4 は、プランナーがエスカレーションに (a)（goal を締め、進め方を
    計画し直す）で答えることを許し、受け入れ条件を書き直すか捨てる (b) と (c) を
    人間に残している。その区別を、ここで機械的に強制する。「テストを通すために
    条件を緩めるな」は、詰まったエージェントが読み替える理由を最も多く持つ指示
    だからだ。

    ステップを足すのは許す。計画はそうやって育つ。
    """
    problems: list[str] = []
    done = green_steps()
    old_steps = {s["id"]: s for s in old.get("steps", [])}
    new_steps = {s["id"]: s for s in new.get("steps", [])}

    for sid, o in old_steps.items():
        n = new_steps.get(sid)
        if n is None:
            problems.append(
                f"P1: step {sid} was removed. Deleting a step deletes its acceptance "
                f"criteria, which is case (c) and belongs to the human")
            continue
        if canon(o.get("acceptance")) != canon(n.get("acceptance")):
            problems.append(
                f"P2: step {sid} has different acceptance criteria. That is case (b) "
                f"and belongs to the human")
        # expected_tests は RED_GATE の R1 が照らす数なので、下げることは同じ条件に
        # 対してテストを減らせと頼むことになる。リンタの L7 は条件の数を下回ることを
        # すでに禁じている。これは、少しでも下がることを禁じる。
        if int(n.get("expected_tests", 0)) < int(o.get("expected_tests", 0)):
            problems.append(
                f"P3: step {sid} lowers expected_tests from {o.get('expected_tests')} "
                f"to {n.get('expected_tests')}")
        if o.get("review_gate") and not n.get("review_gate"):
            problems.append(f"P4: step {sid} turns review_gate off")
        # すでに緑のステップは、いまでは過去となった定義に対して測られた。書き
        # 直してもコードは変わらない（ランナーは走らせ直さない）。台帳が、起きても
        # いないことを記述するようになるだけだ。
        if sid in done and canon(o) != canon(n):
            problems.append(
                f"P5: step {sid} is already green; its definition is a record of what "
                f"was checked and cannot be edited")

    return problems


def brief_plan_revise(step: dict | None, escalation: str, feedback: str = "") -> str:
    spec = SYSTEM_SPEC.read_text(encoding="utf-8") if SYSTEM_SPEC.exists() \
        else "(not written yet)"
    context = (PLAN / "CONTEXT.md").read_text(encoding="utf-8")
    tasks = (PLAN / "tasks.json").read_text(encoding="utf-8")
    done = sorted(green_steps())
    focus = f"step {step['id']}" if step else "the plan"
    return f"""You are the planner. Revise the plan so that {focus} can succeed.

You do not write code and you cannot reach the repository. You write files into
the current directory and the runner decides whether to apply them.

# SYSTEM_SPEC.md
{spec}

# plan/CONTEXT.md
{context}

# plan/tasks.json
{tasks}

# Why the runner stopped
{escalation}

# Steps that are already green
{", ".join(done) if done else "(none)"}

# What you may write, into the current directory
- `tasks.json`      the full revised plan, not a diff
- `CONTEXT.md`      optional, only if the background the solver receives is what
                    was wrong
- `SYSTEM_SPEC.md`  optional, only if an agreed decision has to change
- `{ESCALATE_NAME}`   instead of all of the above; see below

Write no other file. A fourth filename is rejected without being read.

# What you may change about a step that already exists
The goal, the contracts, files_write, files_test, depends_on, max_attempts.
That is: how the step is approached, and how it is described to the solver.

# What is rejected mechanically
- changing any `acceptance` entry of a step that already exists
- removing a step
- lowering `expected_tests`, or turning `review_gate` off
- any change at all to a step that is already green
- a plan that fails the linter (RUNNER_SPEC section 8)

This is BOOTSTRAP 1-4. Case (a) -- the implementation is what is wrong, so
tighten the goal -- is yours. Case (b) -- the acceptance criteria themselves are
wrong or unreachable -- and case (c) -- the upstream design is wrong -- are
decisions for the human, not for you.

So if you conclude that this step cannot be made to pass without changing what
it is required to do, do not try. Write a single file named `{ESCALATE_NAME}`
saying which criterion is unreachable and why, and write nothing else. That is a
correct outcome, and it is the only route to the human.

{feedback_section(feedback)}
Output nothing but the files. Do not restate the plan in your final message.
"""

# --------------------------------------------------------------------------
# 人間の要件から計画を起こす（BOOTSTRAP Phase 0〜3）
# --------------------------------------------------------------------------


# このランナーが受け入れる計画を作るのに、プランナーが要るものすべて。JSON の
# 波括弧を含むので、f-string ではなく普通の文字列にしてある。中の規則はどれも
# ランナーがどのみち強制するもので、ここに書くのは往復を1回省くためだけだ。
# 書いたからといって、それが真になるわけではない。
BOOTSTRAP_RULES = """
# What to write, into the current directory

- `SYSTEM_SPEC.md`  the design, as agreed with the human. Short. The data model,
                    the module boundaries, ONE OR TWO decisions that would be
                    expensive to reverse, and a list of what v1 will not do.
- `CONTEXT.md`      the only background the solver ever receives. It gets the
                    same text on every step and has no memory between steps, so
                    it must be self-contained: language and version, the exact
                    test command, project conventions, domain vocabulary. Under
                    100 lines.
- `tasks.json`      the plan. Format below.
- `ESCALATE.md`     INSTEAD of all of the above, if you cannot plan yet. See the
                    last section.

Write no other file. A fifth filename is rejected without being read.

# tasks.json

Top level:

    "version": 1
    "timeouts": {"test": 120, "solver": 960, "planner": 1800}  (optional)
    "limits":   {"escalations": 1}                             (optional)
    "steps":    [ ... ]

Two further top-level keys exist, "policy" and "solver_tiers", and you must not
write them. They select how retries and solver backends behave, which is a
property of the machine the plan runs on and not of the plan.

Each step:

    "id"              short and stable, e.g. "S1"
    "kind"            "skeleton", "unit" or "integration". Exactly one skeleton,
                      and it is the first step -- see "Start with a skeleton".
    "goal"            what to build, addressed to the solver. It sees this only
                      while implementing, never while writing the tests.
    "depends_on"      ids of earlier steps; [] for the first
    "contracts"       {"requires": [...], "provides": [...], "invariants": [...]}
    "acceptance"      [{"case": "normal|boundary|error",
                        "given": "...", "then": "..."}, ...]
    "files_write"     exact paths the solver may create or modify
    "files_test"      exact paths for the tests; never overlaps files_write
    "expected_tests"  how many tests this step must produce
    "max_attempts"    3 is normal
    "review_gate"     false unless a human must read the tests before freezing

Aim for 10 to 15 steps. One step is ONE behaviour that can fail on its own. If
an implementation would run past roughly 150 lines, split it.

# Start with a skeleton

The first step is `"kind": "skeleton"`, and it is the only one. It is the thin
end-to-end slice: starting from the state the system is in when it is brand new,
going through the public API, arriving somewhere a person would recognise as the
product doing its job.

Thin is the point. Hardcode. Support one case, not the general one. Later steps
deepen it, and its tests stay in the suite -- every later step is checked against
them, so once this slice works it keeps working.

Why this rule exists, plainly. A plan can decompose a system into ten clean
layers, satisfy every rule here, go green on all ten, pass its whole suite, and
still have built something that cannot be reached from where it starts. That is
not hypothetical: it is what the previous plan for this project did. Every step
was correct. There was no step about starting, so nothing ever asked whether
starting was possible, and it was not.

You cannot write this step's acceptance criteria without settling how the thing
begins -- what a new instance holds, what the user's first action is, what it
produces. If settling that turns out to need a decision the requirements did not
make, that is worth knowing now, while it is still a sentence, rather than after
ten steps of work. Decide it yourself if the requirements leave room; escalate
only if the requirements rule out every answer.

# Where the code goes -- not negotiable

Every path in `files_write` starts with `src/`, and every path in `files_test`
starts with `tests/`. Those two directories are the only ones the runner can
open and close for writing, so a plan that puts code anywhere else cannot be
enforced and is rejected.

{LAYOUT_NOTE}

# Rules the runner checks before it will run the plan

A plan that breaks any of these is rejected without being run, so check them
yourself first.

    L2   a step may depend only on steps listed BEFORE it
    L3   everything in contracts.requires is provided by something it depends on
    L4   no file appears in files_write of two different steps
    L5   files_write and files_test never overlap
    L6   every step has at least one "normal", one "boundary" and one "error"
         acceptance case
    L7   expected_tests is at least the number of acceptance criteria
    L8   with review_gate false, the `then` of every criterion states a concrete
         value ON ITS OWN: a number, a quoted literal, an exception type, an
         empty collection ([] {} ()), or True / False / None
    L10  the LAST step is kind "integration"
    L11  a "unit" step may not provide something that no later step requires
    L12  files_write is under src/, files_test is under tests/
    L13  the FIRST step is kind "skeleton", and it is the only one
    L14  every type in contracts.provides states its contents: dict[str, Item],
         tuple[State, int], list[str]. A bare dict, list, tuple or set is
         rejected
    L15  every line of contracts.provides names the module it lives in, and that
         module is one of this step's own files_write

# Four things that are not obvious

These were each learned by watching a run fail on them.

1. **contracts.provides holds signature strings and nothing else.**
   The solver is asked for a stub before it is asked for an implementation, and
   `provides` is what it is given. Anything in there that carries MEANING rather
   than SHAPE gets faithfully implemented in the stub -- and a criterion that
   the stub already satisfies has never been observed to fail, which stops the
   step. Write `def parse(s: str) -> Config`. Do not write "raises ValueError on
   bad input" here; that belongs in acceptance.

2. **A contract states the shape of what it hands over (L14).**
   A later step is handed this one line and nothing else -- not the source, not
   the tests. `-> tuple` cannot be discharged from that line: the arity is not
   in it, so the caller cannot take the result apart and the stub cannot know
   what to build. Write `-> tuple[GameState, int]`, `-> dict[str, Generator]`,
   `list[str]`. Where the shape carries meaning, a named type is better than a
   parameterised container -- `-> Purchase` beats `-> tuple[GameState, int]`,
   and it is a class you declare in `provides` like any other.

3. **contracts.invariants must be observable through the public contract.**
   Invariants go into the brief for writing the TESTS. An invariant about
   internal structure ("must call normalize() rather than reimplement it")
   makes the solver write a test that reaches inside the module, which then
   errors instead of failing, and the runner rejects the step. State invariants
   as facts about inputs and outputs. Put implementation requirements in `goal`,
   which only the implementation phase sees.

4. **The acceptance criteria are the specification.**
   Write each as an exact input and an exact expected output, concrete enough
   that two engineers who never spoke would write the same test. Prefer values
   you can compute by hand. "the result is correct" is not a criterion;
   "the result is exactly 2011.357" is.

Also, and this one has stopped a step twice: the tests are first run against a
stub, and RED_GATE rejects the step if ANY of them passes there (R4) -- a test
that has never been seen to fail proves nothing later. Other criteria do not
make up for it; one passing test stops the step. Two ways a criterion walks into
this:

- Its expected answer is a value the stub can produce -- an empty string, zero,
  an empty list, or an argument returned unchanged.
- It states the expected result as a comparison between two things the code
  under test produced. `advance(p.state, catalog, 0.0) == p.state` is one:
  both sides come from the stub, so both are the same sentinel and the test
  passes. Write the expected value out instead -- `resource == 0.0,
  generators == {'cursor': 1}, last_update == 0.0`. That is a criterion the
  stub fails and a correct implementation passes, which is what a criterion is
  for.
"""

BOOTSTRAP_ESCALATE = """
# If you cannot plan yet

BOOTSTRAP Phase 0: do not start designing while an important question is still
open. If the requirements leave something undecided that would change the SHAPE
of the plan -- a representation that is expensive to reverse, a scope boundary,
a format, a rule with no stated behaviour at its edges -- do not guess.

Write a single file named ESCALATE.md containing your questions, and write
nothing else.

Ask as few questions as will do; the human wants to answer once. For each
question, state the assumption you would proceed on if they said nothing, so
that "your assumptions are fine" is a complete answer.
"""


def brief_plan_bootstrap(requirements: str, feedback: str = "") -> str:
    return f"""You are the planner. Turn the requirements below into a plan.

You do not write code and you never will. Implementation is done by a separate
agent that cannot see this conversation, has no memory between steps, and
receives exactly one step's worth of information at a time. Your output has to
work as a standalone instruction for someone in that position.

You write files into the current directory. The runner checks them before
anything is run, and if they do not pass it will come back to you with the
reasons -- so the requirements below are about the thing being built, and
making a plan this machine accepts is your problem, not the author's.

# The requirements, written by the human
{requirements}
{environment_facts()}
{BOOTSTRAP_RULES.replace('{LAYOUT_NOTE}', LANGUAGE['layout_note'])}
{BOOTSTRAP_ESCALATE}
{feedback_section(feedback)}
Output nothing but the files. Do not restate the plan in your final message.
"""


def environment_facts() -> str:
    """プロジェクトが実際にどうなっているかを、機械から読み取る。

    BOOTSTRAP_RULES の規則は、ランナーが強制するものの書き写しだ。つまり、書いた
    者が覚えていた分しか揃わない。最初の版には L12 が無く、その隙間に向けて
    計画が作られた。木、インタプリタ、テストのコマンドについての事実には、その
    失敗の仕方が無いので、書き写さずに集める。

    その問題への本当の答えは、下のやり直しのループだ。ブリーフが言い忘れたことも、
    リンタが捕まえ、プランナーに伝わる。
    """
    def version(binary: Path) -> str:
        try:
            proc = run([str(binary), "--version"])
            return (proc.stdout + proc.stderr).strip().splitlines()[0]
        except (OSError, IndexError):
            return "(not installed)"

    typescript = LANGUAGE["source_suffix"] == ".ts"

    skip = {".git", ".venv", ".runner", "plan", "__pycache__", ".pytest_cache",
            "node_modules"}
    # index.html と vitest.config.mjs は、35-node.sh が言語に関係なく置く。
    # 箱は計画の言語を知らないからだ。Python の計画に見せると、クリティックは
    # 「人が開く index.html からこの計画のコードに届かない」と指摘し、
    # プランナーはそれに答えられない。TypeScript のときだけ見せる。
    if not typescript:
        skip |= {"index.html", "vitest.config.mjs"}
    listing = []
    for child in sorted(PROJECT.rglob("*")):
        if any(part in skip for part in child.relative_to(PROJECT).parts):
            continue
        rel = child.relative_to(PROJECT)
        listing.append(f"  {rel}/" if child.is_dir() else f"  {rel}")
    tree = "\n".join(listing) or "  (empty apart from the directories above)"

    # 根に置かれたファイルは環境の持ち物で、どのステップも書かない。最初の
    # ステップより前に完成しているので、中身だけを確かめる条件はスタブの時点で
    # 通り、RED_GATE の R4 で必ず止まる。run 8 の S10 は index.html の中身を
    # 確かめる条件で止まり、プランナーは人間に差し戻した。名前は書き写さず、
    # 実際に置かれているものから取る。
    provided = sorted(child.name for child in PROJECT.iterdir()
                      if child.is_file() and child.name not in skip) \
        if PROJECT.is_dir() else []
    provided_text = ""
    if provided:
        provided_text = f"""
These files at the root belong to the environment: {", ".join(provided)}.
No step writes them, and each is already in its final form before the first
step runs. So a criterion that checks only one of them -- what it says, what it
imports, that it exists -- is already true against the stub. RED_GATE runs every
test before anything is implemented, sees that one pass, and stops the step
(R4). Every criterion has to stay false until this step's own code under src/
is written. Where one of these files matters, test the code it calls, not the
file.
"""
        if "index.html" in provided:
            provided_text += """For the page: call `start` on an element and check what it puts there. Do not
write a criterion about the text of index.html.
"""

    # テストが何に届くかを決めるファイル。言語ごとに名前は違うが仕事は同じで、
    # どちらでもプランナーに見せる必要がある。import のパスに逆らう計画は負ける。
    wiring = PROJECT / ("vitest.config.mjs" if LANGUAGE["source_suffix"] == ".ts"
                        else "conftest.py")
    wiring_text = wiring.read_text(encoding="utf-8") if wiring.exists() else "(none)"

    # 起動のつなぎを、説明ではなく全文で見せる。文章の説明は、それに向けて書く
    # プランナーには足りたが、出来上がった計画を読むクリティックには足りなかった。
    # 最初の本番の批評は、index.html を作るものも start() を呼ぶものも無いと2度
    # 報告した。計画は index.html を作らない。作るのは環境で、誰もクリティックに
    # それを伝えていなかった。5件の指摘のうち2件が、ずっと根にあったファイルに
    # ついてのものだった。
    page = PROJECT / "index.html"
    page_text = ("""
`index.html` at the root, which the environment provides and no step writes.
It is what a person opens, and it is already wired:

"""
                 f"{page.read_text(encoding='utf-8')}"
                 ) if typescript and page.is_file() else ""

    # ここのほかのものと同じ理由で、書き写さずに集める。ただし、これは効き目が
    # 大きい。「窓が開く」のような条件は TclError で落ちるテストになり、それは
    # 本物の赤で、RED_GATE をきれいに通る。ソルバーが何を書いても緑にならない
    # ので、ステップはすべての段の試行を使い、エスカレーションまで使い、その原因は
    # どれにも見えるところに無い。
    interpreter = PROJECT / ".venv" / "bin" / "python"

    def imports(module: str) -> bool:
        try:
            return run([str(interpreter), "-c", f"import {module}"]).returncode == 0
        except OSError:
            return False

    display = os.environ.get("DISPLAY", "")
    argv, _ = test_argv(["<the step's files_test>"], STATE / "report.xml")
    command = " ".join(a.replace(str(PROJECT) + "/", "") for a in argv)

    # ソルバーが何をできるかも、書き写さずに使うバックエンドから決める。
    # テストのコマンドだけを見せると、プランナーは CONTEXT.md に「このコマンドで
    # 確かめてから終えよ」と書く。claude と local はファイルの読み書きしか
    # できないので、ソルバーは実行を試みて断られ、試行の一部をそれに使う。
    if any(tier in SOLVERS_THAT_RUN_COMMANDS for tier in SOLVER_TIERS):
        solver_tools = ""
    else:
        solver_tools = """
The solver cannot run commands -- not the test command above, and not anything
else. It reads files and writes files, and that is all it can do. The runner
runs the tests after the solver has finished and hands the failures to the next
attempt. So do not tell the solver, in CONTEXT.md or anywhere else, to run the
tests, to check its work by running something, or to install anything: it will
try, be refused, and spend part of its attempt on it.
"""

    if typescript:
        runtime = f"Runtime: {version(Path('node'))}"
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
    else:
        runtime = f"Interpreter: {version(interpreter)}"
        toolkit = ("GUI toolkit: "
                   + ("tkinter imports" if imports("tkinter")
                      else "tkinter does NOT import"))
        screen = "" if display else """There is no screen here and there will not be one. Code that opens a window
raises an error about the display, so an acceptance criterion about what appears
on screen becomes a test that fails and that no implementation can fix.

Every criterion you write has to be checkable by pytest with no display. If the
requirements ask for a user interface, put whatever constructs it in its own
step, keep that step as thin as you can, and write its criteria in terms of the
functions it calls and the state it passes on -- not in terms of what is drawn.
A human checks the screen afterwards; the runner never can.
"""

    return f"""# The environment, as it actually is right now

Language: {LANGUAGE["label"]}
{runtime}
Test runner: {version(VITEST if typescript else PYTEST)}

Graphical display: {f"DISPLAY={display}" if display else "NONE. DISPLAY is not set"}
{toolkit}

{screen}
The runner executes the tests itself, as:
    {command}
{solver_tools}
Everything under the project root, except plan/, .git/ and the frozen
toolchain -- this is the whole of what exists today:

{tree}
{provided_text}
{wiring.name} at the root, which the test runner loads automatically:

{wiring_text}
{page_text}
"""


def prune_proposal() -> list[str]:
    """out/ の中で、提案が使ってよい名前でないものをすべて消す。

    やり直しのあいだにだけ使う。プランナーは自分が書いたファイルを消す手段を
    持たない（作ることと編集することしかできない）ので、「それを消せ」と伝えても
    従えない助言になる。ディレクトリはランナーの所有なので、ランナーが消し、
    フィードバックでそう伝える。
    """
    allowed = set(PROPOSAL_FILES) | {ESCALATE_NAME}
    removed = []
    for entry in PLANNER_OUT.iterdir():
        if entry.name in allowed and entry.is_file() and not entry.is_symlink():
            continue
        removed.append(entry.name)
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
    return removed


def proposal_problems(proposal: dict[str, str]) -> list[str]:
    """この提案のおかしな点すべて。ランナーが判定するとおりに返す。

    `plan apply`、`plan show`、やり直しのループで共有する。プランナーが直せと
    言われるものが、それを拒んだはずのものとちょうど一致するようにだ。同じ規則を
    2つ目に実装すると、1つ目からずれうる。

    言語は計画そのものから取る。validate_plan が呼ぶ adopt_language を参照。
    それはこの関数の仕事でも、呼び出し側の仕事でもない。3つの呼び出し側が
    続けて忘れた。
    """
    existing = PLAN / "tasks.json"
    if existing.exists():
        old = json.loads(existing.read_text(encoding="utf-8"))
    else:
        # ディスクに計画が無ければ、これは bootstrap だ。P1〜P5 はどれも「変えては
        # ならない」ものについてなので、比べる相手が無い。代わりに、最初の計画は
        # そろっていなければならない。人間が合意した仕様の無いまま受け入れ条件
        # だけで始まるプロジェクトも、ソルバーが拠る背景の無い計画で始まる
        # プロジェクトも無い。
        incomplete = sorted(set(PROPOSAL_FILES) - set(proposal))
        if incomplete:
            return [f"B1: a first plan must include {name}" for name in incomplete]
        old = {"steps": []}

    if "tasks.json" in proposal:
        try:
            new = json.loads(proposal["tasks.json"])
        except json.JSONDecodeError as bad:
            return [f"B2: tasks.json is not valid JSON: {bad}"]
    else:
        new = old

    return check_proposal(old, new) + validate_plan(new)


def plan_with_retry(brief_for, tag: str, keep: dict[str, str] | None = None) -> int:
    """プランナーを呼び、書いたものを確かめ、違反を返す。

    これのおかげで、人間が前もって環境を知っていなくても、プランナーが自分で
    環境に合わせられる。規則を述べる唯一の正式なものは、すでにランナーが持って
    いる。計画を拒むコードそのものだ。だから、ブリーフが規則を漏れなく説明した
    ことに期待するのではなく、規則を走らせて、何が落ちたかを伝える。

    プランナーはこれで新しい権限を得ない。リンタを走らせず、走らせることも
    できない。結果を伝えられるだけだ。試行のあいだ、前のファイルは「そのまま
    残す」。プランナーは禁止事項から新しい計画を作り直すのではなく、見える
    計画を編集する。そのほうが安く、ある違反を別の違反と取り替えにくい。
    """
    limit = LIMITS["revisions"]
    clear_proposal()
    feedback = ""

    for attempt in range(1, limit + 2):
        call_planner(brief_for(feedback))
        names = sorted(p.name for p in PLANNER_OUT.iterdir())
        ledger(tag, attempt=attempt, wrote=names)
        if not names:
            print("the planner wrote no files", file=sys.stderr)
            return 2

        removed = prune_proposal()

        # keep: プランナーが書かなかったファイルを補う控え。改訂のブリーフは
        # CONTEXT.md と SYSTEM_SPEC.md を「変えるときだけ書け」と伝えている。
        # ところが out/ は呼ぶ前に空にしてあり、未適用の計画は B1 で3ファイル
        # すべてを要求される。補わないと、ブリーフに従ったプランナーが落ちる
        # （run 8 の改訂で2回、プランナーの呼び出しを1回ずつ無駄にした）。
        # エスカレーションには補わない。ESCALATE.md は他のファイルと並べられない。
        if keep and ESCALATE_NAME not in names:
            carried = sorted(n for n in keep if not (PLANNER_OUT / n).exists())
            for name in carried:
                path = PLANNER_OUT / name
                path.write_text(keep[name], encoding="utf-8")
                path.chmod(0o644)
            if carried:
                ledger("PLAN_CARRIED", attempt=attempt, files=carried)

        proposal = read_proposal()

        # エスカレーションは下書きではなく答えだ。確かめるものも直すものも無い。
        # 宛先は人間で、`plan apply` がそれを表に出す。
        if ESCALATE_NAME in proposal:
            return 0

        problems = proposal_problems(proposal)
        # 紛れ込んだファイルは、すでに片付いている。prune_proposal が消し、
        # read_proposal は許された3つの名前しか見ないので、それについての何も
        # `plan apply` に届かない。そのうえで違反にすると、もう存在しない状態の
        # ために試行を1回使う。run 7 は4回のうち1回をそれで失った。ほかは正しい
        # 計画だった。だからこれは注記で、別の理由で落ちている試行のフィード
        # バックに添えるだけで、それ自体を落ちる理由にはしない。紛れ込んだ
        # ファイルが問題になる場合（綴りを誤った名前で計画を書いた場合）はやはり
        # 落ちる。消すと必要なファイルが欠け、proposal_problems がそう言うからだ。
        notes = [f"B3: {n} is not a filename a proposal may use; the "
                 f"runner deleted it" for n in removed]
        if removed:
            ledger("PLAN_PRUNED", attempt=attempt, removed=removed)
        if not problems:
            if attempt > 1:
                print(f"the plan passed on attempt {attempt}")
            return 0

        ledger("PLAN_SELFCHECK", attempt=attempt, violations=problems)
        if attempt > limit:
            print(f"\nthe planner could not produce an acceptable plan in "
                  f"{attempt} attempts; {PLANNER_OUT} holds the last one",
                  file=sys.stderr)
            for problem in problems:
                print("  " + problem, file=sys.stderr)
            return 2

        feedback = "\n".join(notes + problems)

    return 2   # 届かない。ループは必ず戻る


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


def stamp_language(name: str) -> None:
    """受け入れた提案に、ランナー自身の行為として言語を書き込む。

    プランナーはこれを書かない。solver_tiers を書かないのと同じ理由だ。この機械が
    どの言語を持つかは箱の性質で、プロジェクトがどれを使うかは、誰かに計画を頼む
    前に人間が決めることだ。どちらもプランナーが選ぶものではなく、選べる
    プランナーは、入っていないツールチェーンを選ぶことがある。

    リンタが通った後に書き込む。安全なのは、どの規則もこのキーを読まないから
    だけだ。このキーは規則を表す語彙を選ぶもので、その選択はブリーフを組み
    立てたときにすでに済んでいる。
    """
    path = PLANNER_OUT / "tasks.json"
    try:
        tasks = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(tasks, dict):
        return
    tasks["language"] = name
    body = (json.dumps(tasks, ensure_ascii=False, indent=2) + chr(10)).encode("utf-8")
    # O_CREAT を付けずに書く。これは細かい話ではない。out/ はスティッキーで
    # グループが書け、ファイルは `planner` のものだ。カーネルの
    # fs.protected_regular（ここでは 2）は、まさにその形のディレクトリで、他人の
    # 既存のファイルを O_CREAT で開くことを拒む。/tmp で、ほかの人が書こうと
    # しているファイルを先に作らせないための保護だ。pathlib の write_text は "w"、
    # つまり O_WRONLY|O_CREAT|O_TRUNC で開くので EACCES で拒まれ、同じプロセスが
    # 同じファイルを普通の O_WRONLY|O_TRUNC で開くと通る。プランナーが書いた
    # ファイルをここで書き直すものは、すべてこの方法で行う。
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    try:
        os.write(fd, body)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# クリティック
# --------------------------------------------------------------------------
#
# 4つ目の役。あるのは理屈ではなく測った結果のためだ。run 7 は緑のステップ10と
# 通ったテスト42で終わり、成果物は入力をまったく受け付けなかった。資源は 0.0
# から始まり、生産は空の dict を足し、どちらの購入も 0 より高かった。どの関門も
# 正しかった。どの条件も本当に何かを確かめていた。機械の中に、結果が良いかを
# 問える位置にあるものが無かった。
#
# 関門が確かめるのは「作業の形」だ。テストを先に書いたか、スタブに対して正しい
# 理由で落ちるか、ほかに何かを触ったか、通っていたものが落ちたか。どれも何を
# 作るかには触れず、触れるべきでもない。だからどんな題材、どんな言語でも働く。
# クリティックはもう半分だ。何かが終わったと決めることは決してなく、何かが
# おかしいと言えるだけだ。
#
# モードを2つにするのは、1つの問いでは両方の種類の欠陥を見つけられないからだ。
# run 7 の計画で測り、指摘は重ならなかった:
#
#   coverage  見つけたもの: リセットの機能に画面から届かない。起動のコマンドを
#             確かめるものが無い。求められた発電機ごとの数値が、欠けているだけで
#             なく反対の値で確かめられていて、正しい実装が条件に「落ちる」
#   trace     見つけたもの: 初期状態が、どの操作でも動かない不動点。モジュール
#             まるごとが最初から死んでいる。記述する関数が一度も呼ばれないから
#             こそ成り立つ不変条件
#
# どちらも、もう片方の指摘を見つけなかった。だから、2つの節を持つ1つの
# ブリーフではなく、別々のブリーフで別々に呼ぶ。1つの枠組みにすると片方が
# 支配し、最初の試みはまさにそれで不動点を見逃した。

CRITIQUE_MODES = ("coverage", "trace")


def brief_critique_coverage(requirements: str, tasks: str) -> str:
    """この計画を完全に満たしたとき、人間が求めたものが手に入るか。"""
    return f"""You are the critic. Your only job is to answer one question about
a piece of work that has not been built yet.

# The question

A human wrote requirements for something they want. A planner turned those
requirements into a plan: a sequence of steps, each with acceptance criteria
that a machine will check.

**If every acceptance criterion in this plan passed, would the human's
requirements be satisfied?**

# The requirements, written by the human

{requirements}

# The plan

{tasks}

{environment_facts()}

# How to read the plan

`steps` is a sequence. Each step has:

- `goal` -- prose describing what the step builds
- `contracts.provides` / `requires` / `invariants` -- the signatures and
  properties this step hands to later steps
- `acceptance` -- the criteria a machine will check, as {{case, given, then}}.
  **These are the only things that will be verified.** `goal` and `invariants`
  are prose; nothing checks them.
- `files_write` -- the files this step creates. A file belongs to exactly one
  step and is never edited by a later step.

# What to look for

Read the requirements as a person who will use the thing, not as someone
checking boxes. Then work out what the plan actually produces, by reading the
contracts and criteria as a whole system rather than step by step.

A plan can be wrong in ways no single step is wrong in. Pay attention to what
the steps produce TOGETHER, and to anything the requirements ask for that no
criterion anywhere would detect the absence of.

Watch for the sharper case as well: a criterion that does not merely omit
something the requirements ask for, but asserts an exact result that a correct
implementation would fail. A plan can test against its own requirements.

{findings_contract()}"""


def brief_critique_trace(tasks: str) -> str:
    """初期状態から始めて、使う人は実際にどこまで届くか。

    あえて要件を渡さない。run 7 の計画で、このモードは条件だけから行き詰まりを
    導いた。つまり、欠けているものについて要件が何も言っていなくても、動かない
    製品を捕まえられる。run 7 の要件は、初期状態に一言も触れていなかった。
    """
    return f"""You are the tracer. You read a plan for something that has not
been built yet, and you work out what a user of the finished thing would
actually be able to do.

# The plan

{tasks}

{environment_facts()}

# How to read it

`steps` is a sequence. Each step has:

- `contracts.provides` -- the functions and types this step creates
- `contracts.requires` -- what it takes from earlier steps
- `contracts.invariants` -- properties claimed in prose (nothing verifies these)
- `acceptance` -- {{case, given, then}} triples. These are the only things a
  machine will check, and they carry concrete values, so they are the most
  reliable statement of what the code will actually do
- `goal` -- prose
- `files_write` -- the files this step owns

# Your task

Work out, from the contracts and the acceptance criteria:

1. **The initial state.** What state does the artifact hold the first time a
   user opens it, before anything has happened? Quote the criteria that fix it.

2. **The action set.** What can a user of the finished artifact actually do?
   Not what functions exist -- what a person operating the thing can invoke.
   Reaching a function requires that something in the plan exposes it to them;
   a function no exposed surface calls is not an action.

3. **The reachable set.** Starting from the initial state and applying any
   sequence of those actions, what states can be reached? Derive it, showing
   the arithmetic where the criteria give you concrete numbers.

Then say plainly:

- Is any state reachable from the initial state at all, or is the initial state
  fixed under every available action?
- Are there states the artifact is evidently built to handle that no sequence
  of user actions can ever reach? Name them and show why.

  This question is about the artifact's OWN logic: a goal that can never be
  met, a purchase that can never be afforded, a screen that can never be
  shown. It is not about input that arrives from outside the user's actions --
  saved data read back from storage, storage that was edited or corrupted, a
  save written by an older version, a clock that moved, a file or response
  from elsewhere. That input is real even though no click produces it, and a
  test that feeds it in is checking a defence, not exercising dead code. Do
  not report it.

  That exemption covers the DEFENCE, not the thing being read. If the
  artifact reads something back -- a save, a setting, a record -- check that
  some step in the plan also writes it. Something the artifact reads that no
  step ever writes, and that the plan does not say comes from elsewhere (a
  file the user supplies, a response from a server), is a defect in the
  artifact's own logic: the read path is reachable, and what it finds is
  always nothing. Report that.

Show your derivation with the actual numbers from the criteria, so that someone
can check each step. Do not describe what the plan intends -- describe what it
specifies. If your derivation contradicts what the prose in `goal` or
`invariants` claims, trust the acceptance criteria and say that they disagree.

{findings_contract()}"""


def findings_contract() -> str:
    """クリティックが書くものと、書いてよい唯一のファイル名。

    あえて、構造を持った枠の中に文章を入れる。ランナーは続けるかを決めるために
    指摘を数える必要があり、プランナーは手を打つために指摘を読む必要がある。
    数だけでは「計画が自分の要件に逆らって確かめている」を運べず、自由な文章は
    数えられない。
    """
    return f"""# What to write

Write exactly one file, named `{FINDINGS_NAME}`, in the current directory.
Write no other file: a name you were not given is a name the runner deletes
without reading.

    {{
      "findings": [
        {{
          "title": "one line, the claim itself",
          "evidence": "which steps, contracts or criteria show it. Be specific
                       enough that someone can check you. Quote the criteria.",
          "machine_would_notice": false
        }}
      ]
    }}

`machine_would_notice` is false when every criterion would pass while the
problem stood. Say so plainly when that is the case -- it is the difference
between a plan that is incomplete and a plan that cannot be caught.

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


def clear_critique() -> None:
    """批評を頼む前に out/ を空にする。

    clear_proposal と同じ理由だ。前の呼び出しの残りのファイルは、今回の答えと
    して読まれる。「クリティックは何も見つけなかった」と「クリティックは走ら
    なかった」は、決して同じに見えてはならない。
    """
    CRITIC_OUT.mkdir(parents=True, exist_ok=True)
    for entry in CRITIC_OUT.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            raise Halt("CRITIQUE",
                       f"a directory is in the way in {CRITIC_OUT}: {entry.name}",
                       "Remove it by hand; the runner will not recurse into a "
                       "tree written by another uid.")
        entry.unlink()


def call_critic(brief: str, mode: str) -> str:
    """クリティックにブリーフを1つ渡す。

    ブリーフが何を含み、何を含まないかに注意する。計画は運ぶ。計画の批評には
    計画が要るからだ。テスト、台帳、何が通ったかについては何も運ばない。そして
    クリティックはどのみちそれに届かない。tests/ と src/ は 2770 runner:solverw で、
    クリティックはどちらのグループにもいない。これは飾りではなく測った結果だ。
    すでに通ったものが見えるクリティックは、通ったと報告する。
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
            timeout=limit + BACKSTOP_MARGIN))
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
            if unseen:
                lines.append("   NO GATE WOULD CATCH THIS")
            if evidence:
                lines.append("   " + evidence.replace("\n", "\n   "))
        lines.append("")
    return "\n".join(lines)


def brief_plan_refine(requirements: str, tasks: str, findings: str,
                      feedback: str = "") -> str:
    """まだ適用していない自分の計画への批評を、プランナーに渡す。

    `plan propose` とは違う状況なので、あちらのブリーフを使い回さず、違いを
    ブリーフに書く。何も作られておらず、何も緑でないので、P5 は効かない。受け
    入れ条件を含めて、この計画のどこでもまだ変えられる。これが `plan apply` の
    前に行う理由のすべてだ。適用した後は、条件はループが測られる基準になり、
    それを書き直すのは (b) で、人間のものになる。
    """
    return f"""You are the planner. A critic read the plan you wrote and found
problems with it. Revise the plan so those problems are gone.

You do not write code and you cannot reach the repository. You write files into
the current directory and the runner decides whether to apply them.

# The requirements, written by the human
{requirements}

# The plan you wrote
{tasks}

# What the critic found
{findings}

# What is different about this moment

NOTHING HAS BEEN BUILT YET. This plan has not been applied, no step is green,
and no test exists. So unlike a revision after a step has failed, **everything
here is still yours to change** -- the steps, the contracts, and the acceptance
criteria. This is the last point at which the criteria can change without a
human deciding it, which is why the critique happens here.

# The critic could be wrong

It was shown the requirements and the plan, and nothing else. It has never seen
the repository. If a finding is mistaken -- most likely because it assumed
something must be built that the environment already provides -- then do not
contort the plan to satisfy it. Leave that part alone and say why in the plan
itself, where it will survive: put the reason in the `goal` of the step it
concerns, in one sentence. A finding you silently ignore will simply be found
again on the next pass.

# What you may write, into the current directory
- `tasks.json`      the full revised plan, not a diff
- `CONTEXT.md`      optional, only if the background the solver receives is what
                    was wrong
- `SYSTEM_SPEC.md`  optional, only if an agreed decision has to change
- `{ESCALATE_NAME}`   instead of all of the above; see below

Write no other file. A fourth filename is rejected without being read.

# What is rejected mechanically
- a plan that fails the linter (RUNNER_SPEC section 8)
- lowering `expected_tests` or turning `review_gate` off
- removing a step

Fixing a finding by deleting the criterion that would have caught it is the one
move that makes the plan worse while appearing to answer. If a requirement
cannot be met, that is worth saying out loud rather than arranging for nobody
to notice.

If you conclude the requirements themselves are contradictory, or that what the
critic wants cannot be done without a decision that is not yours, write a single
file named `{ESCALATE_NAME}` saying so and write nothing else. That is a correct
outcome and it is the only route to the human.

{feedback_section(feedback)}
Output nothing but the files. Do not restate the plan in your final message.
"""


def run_critique(modes: list[str], tasks: str) -> dict[str, list[dict]]:
    """1つの計画の本文に対して、各モードを1回ずつ走らせる。"""
    requirements = REQUIREMENTS.read_text(encoding="utf-8") \
        if REQUIREMENTS.is_file() else ""
    by_mode: dict[str, list[dict]] = {}
    for mode in modes:
        if mode == "coverage" and not requirements:
            # 飛ばしたことを黙らない。飛ばしたモードは by_mode に入らないので、
            # 呼び出し側から見ると「走って何も見つけなかった」と区別できない。
            ledger("CRITIQUE_SKIPPED", mode=mode, reason=f"no requirements at {REQUIREMENTS}")
            print(f"coverage skipped: no requirements at {REQUIREMENTS}",
                  file=sys.stderr)
            continue
        brief = (brief_critique_coverage(requirements, tasks) if mode == "coverage"
                 else brief_critique_trace(tasks))
        ledger("CRITIQUE", mode=mode)
        clear_critique()
        call_critic(brief, mode)
        findings = read_findings()
        ledger("FINDINGS", mode=mode, count=len(findings),
               titles=[str(f.get("title", ""))[:200] for f in findings])
        by_mode[mode] = findings
    return by_mode


def no_critique_ran(modes: list[str]) -> int:
    """頼んだモードが1つも走らなかったときの終わり方。

    指摘ゼロとは別の結果として扱う。clean と表示すると、批評を1回も受けて
    いない計画が、批評を通った計画と同じに見える。返す 1 は「ランナーが仕事を
    できなかった」で、cmd_critique の 0（指摘なし）と 4（指摘あり）のどちらとも
    重ならない。
    """
    ledger("CRITIQUE_NONE", requested=list(modes))
    print(f"no critique ran (requested: {', '.join(modes)}). This is not a clean "
          f"result: nothing looked at the plan.\nFor coverage, put the "
          f"requirements at {REQUIREMENTS}.", file=sys.stderr)
    return 1


def cmd_plan_refine(modes: list[str]) -> int:
    """保留中の計画を批評し、指摘を返し、それを繰り返す。

    これで外側の輪が閉じる。これが無いと批評は人が読んで手を打つ報告になり、
    機械が扱うために作られたちょうどその場所で、人間が輪の中に残る。しかも指摘の
    宛先は、どのみち人間ではなくプランナーだ。

    「保留中の」提案に対して走り、`plan apply` の前で止まる。だから、これが何を
    しても、何かを測った基準に触れることはない。

    limits.critiques で上限を掛ける。このループは1回の呼び出しの中にあるので、
    台帳ではなくここで数える。上限は「もう少し良くできる」が永遠に続くのを止める。
    これは小さな話ではない。改訂1回はプランナーの呼び出し1回で、TypeScript の
    ブリーフでは28分かかった。
    """
    pending = PLANNER_OUT / "tasks.json"
    if not pending.is_file():
        print(f"no pending proposal at {pending}", file=sys.stderr)
        print("run `plan bootstrap` first; refine works before `plan apply`, "
              "while the criteria are still a draft", file=sys.stderr)
        return 1

    requirements = REQUIREMENTS.read_text(encoding="utf-8") \
        if REQUIREMENTS.is_file() else ""
    cap = LIMITS["critiques"]

    for round_no in range(1, cap + 2):
        tasks = pending.read_text(encoding="utf-8")
        load_settings(json.loads(tasks))
        language = json.loads(tasks).get("language", "python")

        by_mode = run_critique(modes, tasks)
        if not by_mode:
            return no_critique_ran(modes)
        total = sum(len(f) for f in by_mode.values())
        report = render_findings(by_mode)
        print(f"\n=== critique {round_no} of at most {cap + 1} ===")
        print(report)

        if total == 0:
            ledger("CRITIQUE_CLEAN", round=round_no, modes=sorted(by_mode))
            print("the critic found nothing left. That is not approval -- the "
                  "gates and the human decide that.")
            return 0

        if round_no > cap:
            ledger("REFINE_CAP", round=round_no, findings=total)
            print(f"\n{total} finding(s) still standing after {cap} revision(s). "
                  f"The proposal is left as it is; read it and decide.",
                  file=sys.stderr)
            return 4

        ledger("PLAN_REFINE", round=round_no, findings=total)
        # 改訂前の提案を控える。plan_with_retry は最初に out/ を空にするので、
        # 控えが無いと、改訂が失敗したときやエスカレーションしたときに、
        # リンタを通っていた提案まで失われる。
        draft = {name: text for name, text in read_proposal().items()
                 if name in PROPOSAL_FILES}
        # tasks.json は補わない。プランナーが書かなければ、改訂が無いということだ。
        code = plan_with_retry(
            lambda feedback: brief_plan_refine(requirements, tasks, report, feedback),
            "PLAN_REFINE_DRAFT",
            keep={name: text for name, text in draft.items() if name != "tasks.json"})
        if code != 0:
            restore_proposal(draft)
            ledger("REFINE_RESTORED", round=round_no, reason="revision failed",
                   files=sorted(draft))
            print(f"the revision failed; the draft from before round {round_no} "
                  f"is back in {PLANNER_OUT}", file=sys.stderr)
            return code
        proposal = read_proposal()
        if ESCALATE_NAME in proposal:
            REFINE_ESCALATION.write_text(proposal[ESCALATE_NAME], encoding="utf-8")
            restore_proposal(draft)
            ledger("REFINE_RESTORED", round=round_no, reason="planner escalated",
                   files=sorted(draft))
            print("The planner escalated to you rather than revising:\n")
            print(proposal[ESCALATE_NAME])
            print(f"\nthe draft from before round {round_no} is back in "
                  f"{PLANNER_OUT}, and the escalation is kept at "
                  f"{REFINE_ESCALATION}.\nRun `plan apply` to apply the draft as "
                  f"it is, or `plan bootstrap` to start over.", file=sys.stderr)
            return 3
        # プランナーはファイル全体を書き直すので、言語の印を付け直す。これは
        # プランナーではなくランナーの印で、失った計画は黙って Python と読まれる。
        stamp_language(language)

    return 4   # 届かない。ループは必ず戻る


def cmd_critique(modes: list[str]) -> int:
    """ディスクにある計画について、各モードでクリティックに訊き、報告する。

    何も見つからなければ 0、何か見つかれば 4 を返す。1 や 2 にはしない。それらは
    すでに「ランナーが仕事をできなかった」を意味し、完璧に動いて問題を見つけた
    批評は、失敗した批評とは別の結果だ。`run --all` はそれらを見分ける必要がある。
    """
    # 保留中の提案を先に見る。その順番が要点だ。批評が最も価値を持つのは
    # `plan apply` の「前」、条件がまだ下書きのうちだ。適用した後は、条件はループが
    # 測られる基準になり、それを変えるのは (b) で、人間のものになる。適用済みの
    # 計画だけを読むと、この動詞はちょうど1歩遅れて届く。改訂を見直せるよう、
    # 保留中が無ければ適用済みの計画に戻る。
    pending = PLANNER_OUT / "tasks.json"
    applied = PLAN / "tasks.json"
    tasks_path = pending if pending.is_file() else applied
    if not tasks_path.is_file():
        print(f"no plan to critique: neither {pending} nor {applied}",
              file=sys.stderr)
        return 1
    print(f"critiquing {'the pending proposal' if tasks_path == pending else 'the applied plan'}"
          f" ({tasks_path})")
    tasks = tasks_path.read_text(encoding="utf-8")
    load_settings(json.loads(tasks))

    requirements = REQUIREMENTS.read_text(encoding="utf-8") \
        if REQUIREMENTS.is_file() else ""
    if not requirements:
        print(f"no requirements at {REQUIREMENTS}; coverage needs them",
              file=sys.stderr)

    by_mode = run_critique(modes, tasks)
    if not by_mode:
        return no_critique_ran(modes)
    total = sum(len(f) for f in by_mode.values())
    print(render_findings(by_mode))
    if total == 0:
        ledger("CRITIQUE_CLEAN", modes=sorted(by_mode))
        print("the critic found nothing. That is not approval -- the gates and "
              "the human decide that.")
        return 0
    print(f"{total} finding(s). Nothing is green or not green because of this; "
          f"decide what to do with them.")
    return 4


def cmd_plan_bootstrap(source: str | None, language: str = "python") -> int:
    """人間が書いた要件のファイルから、最初の計画をプランナーに頼む。

    計画が生まれる唯一の経路で、あえてほかのプランナー呼び出しと同じ形にして
    ある。ブリーフを入れ、提案が出て、ランナーが決める。ここでの人間の権限は
    要件のファイルで、plan/ を直接書く力でも、ランナーの仕組みを知る義務でも
    ない。要件は作るものについて述べる。この機械が受け入れる計画を作るのは
    プランナーの仕事で、やり直しのループがそれを願望ではなく事実にしている。
    """
    path = Path(source) if source else REQUIREMENTS
    if not path.is_file():
        print(f"no requirements at {path}", file=sys.stderr)
        print("write them there, or pass --from <path>", file=sys.stderr)
        return 1

    # bootstrap は計画全体を置き換える。すでに緑のステップがあると、台帳は
    # もう存在しない条件に対する作業を記述することになる。検査が緑のステップの
    # 編集を拒む（P5）のと同じ理由だ。
    done = green_steps()
    if done:
        print(f"refusing: {', '.join(sorted(done))} already green", file=sys.stderr)
        print("a bootstrap replaces the plan, which would orphan them. Start a "
              "fresh project directory instead.", file=sys.stderr)
        return 1

    # ブリーフを組み立てる後ではなく前に設定する。environment_facts はテストの
    # コマンドとツールキットを報告し、配置の段落はソースファイルがそもそもどんな
    # ものかをプランナーに伝える。違う言語向けに書いたブリーフからは、どの
    # ステップも誤った計画が出てくる。
    load_settings({"language": language})

    requirements = path.read_text(encoding="utf-8")
    ledger("PLAN_BOOTSTRAP", source=str(path), language=language)
    code = plan_with_retry(
        lambda feedback: brief_plan_bootstrap(requirements, feedback),
        "PLAN_BOOTSTRAP_DRAFT")
    if code == 0 and ESCALATE_NAME not in read_proposal():
        stamp_language(language)
    return code


def cmd_plan_propose(step_id: str | None) -> int:
    if not ESCALATION.exists():
        print(f"nothing to revise: {ESCALATION} does not exist", file=sys.stderr)
        return 1
    load_settings(json.loads((PLAN / "tasks.json").read_text(encoding="utf-8")))
    step = None
    if step_id:
        step, _ = load_plan(step_id)
    escalation = ESCALATION.read_text(encoding="utf-8")
    ledger("PLAN_PROPOSE", step=step_id or "-")
    return plan_with_retry(
        lambda feedback: brief_plan_revise(step, escalation, feedback),
        "PLAN_PROPOSE_DRAFT")


def cmd_plan_show() -> int:
    proposal = read_proposal()
    for name in sorted(proposal):
        body = proposal[name]
        print(f"--- {name} ({len(body.splitlines())} lines) ---")
        print(body)
    if ESCALATE_NAME in proposal:
        return 0
    problems = proposal_problems(proposal)
    print("--- would it be accepted? ---")
    for problem in problems:
        print("  " + problem)
    print("  yes" if not problems else f"  no: {len(problems)} violation(s)")
    return 0


def cmd_plan_apply() -> int:
    """提案を確かめてから適用する。拒んだときは out/ をそのまま残し、人間が
    拒まれたものを読めるようにする。"""
    proposal = read_proposal()

    if ESCALATE_NAME in proposal:
        # ランナーのエスカレーションは、意図して作った経路で人間に届く。
        # plan/ESCALATION.md はコミットされ、送られ、ホストに引かれ、
        # ダッシュボードに出る。プランナーのエスカレーションにはその経路が無い。
        # planner/out/ はリポジトリの外なので、このコマンドを走らせた端末に出る
        # だけで、ほかのどこにも残らない。1つの端末にしか見えない人間宛ての問いは、
        # 人間宛てになっていない。だからここで plan/ に写してコミットする。経路を
        # 走らせて見つけた。プランナーは正しく断り、その答えが見えなかった。
        PLANNER_ESCALATION.write_text(proposal[ESCALATE_NAME], encoding="utf-8")
        PLANNER_ESCALATION.chmod(0o644)
        ledger("PLAN_ESCALATE", note="the planner declined; this is case (b) or (c)")
        relative = PLANNER_ESCALATION.relative_to(PROJECT).as_posix()
        run(["git", "add", "--", relative], check=True)
        if run(["git", "diff", "--cached", "--quiet", "--", relative]).returncode:
            run(["git", "commit", "-q", "-m", "plan: the planner escalated to the human",
                 "--", relative], check=True)
            publish("the planner's escalation")
        print("The planner escalated to you rather than proposing a change:\n")
        print(proposal[ESCALATE_NAME])
        return 3

    problems = proposal_problems(proposal)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        ledger("PLAN_REJECT", files=sorted(proposal), violations=problems)
        print(f"\nproposal rejected: {len(problems)} violation(s); "
              f"{PLANNER_OUT} left as it is", file=sys.stderr)
        return 2

    applied = []
    for name, dest in PROPOSAL_FILES.items():
        if name not in proposal:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(proposal[name], encoding="utf-8")
        dest.chmod(0o644)
        applied.append(str(dest.relative_to(PROJECT)))

    # エスカレーションには答えたので、それは消す。これで状態がひと目で読め、
    # スクリプトから安全に動かせる。ESCALATION.md があるのは未解決の
    # エスカレーションがあるときだけで、`plan propose` はそれが無いと走らない。
    # 残すと、2回目の propose が答え済みの問いに答えてしまう。
    escalation_tracked = bool(
        run(["git", "ls-files", "--", str(ESCALATION.relative_to(PROJECT))]).stdout.strip())
    ESCALATION.unlink(missing_ok=True)

    # 適用される計画は、プランナーが訊いたことへの答えなので、その問いも消す。
    # 理由は同じで、ダッシュボードはこのファイルがあることを「誰かがあなたを
    # 待っている」と読む。
    planner_escalation_tracked = bool(
        run(["git", "ls-files", "--",
             str(PLANNER_ESCALATION.relative_to(PROJECT))]).stdout.strip())
    PLANNER_ESCALATION.unlink(missing_ok=True)

    ledger("PLAN_APPLY", files=applied)

    # 計画だけをコミットし、ほかは何も含めない。提案はふつう、止まったステップの
    # 変更が作業ツリーに残ったまま届く。それを同じコミットに入れると、捨てた試行を
    # 計画の変更の一部として記録してしまう。
    paths = applied + [str(LEDGER.relative_to(PROJECT))]
    if escalation_tracked:
        paths.append(str(ESCALATION.relative_to(PROJECT)))
    if planner_escalation_tracked:
        paths.append(str(PLANNER_ESCALATION.relative_to(PROJECT)))
    # `git commit -- <paths>` は追跡中のパスしか入れないので、3つのファイルが
    # すべて新しい最初の計画では何もコミットせず 1 で終わる。先に明示して add
    # する。（最初の bootstrap まで気づかなかった。見本のプロジェクトでは、これらの
    # ファイルが常に存在していたからだ。）
    run(["git", "add", "--"] + paths, check=True)
    run(["git", "commit", "-q", "-m", "plan: apply planner proposal", "--"] + paths,
        check=True)
    publish("the plan")
    for entry in PLANNER_OUT.iterdir():
        entry.unlink()
    print("applied: " + ", ".join(applied))
    return 0



def load_settings(tasks: dict) -> None:
    """計画が上限を上げ下げできるようにする。対象は存在するキーだけで、ほかは
    無視しない。知らないキーは、黙って無視される設定になるからだ。"""
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
    language = tasks.get("language")
    if language is not None:
        if language not in LANGUAGES:
            raise SystemExit(f"language must be one of {sorted(LANGUAGES)}, "
                             f"not {language!r}")
        LANGUAGE.clear()
        LANGUAGE.update(LANGUAGES[language])


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
    # タグは動く。ステップを reset して走らせ直すと、`git tag -f step-<id>` が
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


def complete_green(step_id: str, goal: str, attempts: int) -> None:
    """GREEN の証拠を、それが述べるコードと一緒にコミットする。

    台帳の事実は、その結果を記録する実装と同じコミットに入れる。GREEN を
    ステップのコミットの後に追記すると、その記録は次のステップのコミットに
    入る。最後のステップには次が無いので、ホストの写しは常に1ステップ遅れて
    見える。
    """
    ledger("GREEN", step=step_id, attempts=attempts)
    run(["git", "add", "-A"], check=True)
    run(["git", "commit", "-q", "-m", f"{step_id}: {goal[:60]}"], check=True)
    run(["git", "tag", "-f", f"step-{step_id}"], check=True)
    publish(f"step {step_id}")


def has_all_green(lines: list[str], plan_digest: str) -> bool:
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") == "ALL_GREEN" and record.get("plan_sha256") == plan_digest:
            return True
    return False


def completion_recorded(plan_digest: str) -> bool:
    """この計画の ALL_GREEN がコミットに届いているか。

    あえて「ファイルに記録があるか」を訊かない。記録を書くこととコミットする
    ことは2つの手順で、その間で失敗すると取り返せなくなる。台帳は ALL_GREEN と
    言うので、後の走行はすべて早く戻り、その事実をホストの写しに運ぶコミットは
    いつまでも作られない。complete_run が防ぐための欠陥そのものだ。だから問いは
    git が持っているかで、答えは git から読む。
    """
    shown = run(["git", "show", f"HEAD:{LEDGER.relative_to(PROJECT).as_posix()}"])
    return shown.returncode == 0 and has_all_green(shown.stdout.splitlines(), plan_digest)


def completion_written(plan_digest: str) -> bool:
    """コミットされたかに関係なく、作業中のファイルに記録があるか。"""
    try:
        return has_all_green(LEDGER.read_text(encoding="utf-8").splitlines(), plan_digest)
    except OSError:
        return False


def complete_run(done: set[str], tasks: dict) -> None:
    """完了を bare リポジトリに、したがって GUI に見えるようにする。"""
    plan_digest = hashlib.sha256(
        json.dumps(tasks, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if completion_recorded(plan_digest):
        return
    # git に記録が無い。この走行がいま終わったか、前の走行が記録を書いてから
    # コミットの前に失敗したかのどちらかだ。後者なら記録はすでにファイルにあり、
    # もう一度追記すると、走行が2回完了したことになる。
    if not completion_written(plan_digest):
        ledger("ALL_GREEN", steps=sorted(done), plan_sha256=plan_digest)
    ledger_path = LEDGER.relative_to(PROJECT).as_posix()
    run(["git", "add", "--", ledger_path], check=True)
    run(["git", "commit", "-q", "-m", "run: all steps green", "--", ledger_path],
        check=True)
    publish("run completion")


def run_step(step_id: str, unvalidated: bool = False) -> int:
    tasks = json.loads((PLAN / "tasks.json").read_text(encoding="utf-8"))
    load_settings(tasks)
    problems = validate_plan(tasks)
    if problems:
        if not unvalidated:
            raise Halt("PLAN_LOAD", f"plan has {len(problems)} lint violation(s)",
                       "\n".join(problems))
        # リンタを飛ばすことは許すが、決して黙っては飛ばさない。リンタに落ちた
        # 計画から出た緑は、人間のレビューなしで出た緑と同じ理由で、台帳にそう
        # 書かなければならない。
        ledger("PLAN_LINT", skipped=True, violations=problems,
               note="ran with --unvalidated; these violations were not fixed")

    step, context = load_plan(step_id)
    ledger("PLAN_LOAD", step=step_id, files_write=step["files_write"], files_test=step["files_test"])

    if touched_paths():
        raise Halt("PLAN_LOAD", "the working tree is dirty; refusing to start",
                   "\n".join(sorted(touched_paths())))

    attempt = 0
    last_run: TestRun | None = None
    try:
        # --- TEST_WRITE -------------------------------------------------
        #
        # ここでやり直すのは、テストファイルがコンパイルできなかったときだけだ。
        # それはソルバーの誤りで、ほかの誰にも直せない。プランナーは構文エラーを
        # 直せず、エスカレーションすれば、それを言われるために28分の呼び出しを
        # 使う。run 8 の S1 は2回これをやった。どちらも条件から単一引用符の
        # テスト名にアポストロフィを写したもので、2回目はブリーフがまさにそれを
        # 警告していた。ソルバーが無視できる指示は、コンパイラ自身の言葉を渡す
        # やり直しより価値が低い。
        #
        # ほかの理由ではやり直さない。意図したことだ。落ちるテストや、スタブに
        # 対して通るテストは、頼んだ内容の問題で、もう一度頼んでも同じ誤解を引き
        # 直すために払うだけだ。
        broken = ""
        for write_attempt in range(1, LIMITS["test_writes"] + 1):
            set_writable(tests=True, src=False)
            call_solver("TEST_WRITE", brief_test_write(step, context, broken))
            assert_touched("TEST_WRITE", step["files_test"])
            assert_written("TEST_WRITE", step["files_test"])
            ledger("TEST_WRITE", step=step_id, ok=True, attempt=write_attempt)

            # --- STUB ---------------------------------------------------
            set_writable(tests=False, src=True)
            written = generate_stub(step, dep_contract_lines(step))
            if written is None:
                call_solver("STUB", brief_stub(step))
                ledger("STUB", step=step_id, ok=True, by="solver")
            else:
                for rel, text in written.items():
                    path = PROJECT / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
                ledger("STUB", step=step_id, ok=True, by="runner",
                       files=sorted(written))
            assert_touched("STUB", step["files_test"] + step["files_write"])
            assert_written("STUB", step["files_write"])

            red = pytest_run(f"red-{write_attempt}", step["files_test"])
            broken = chr(10).join(k for k in red.failure_kinds
                                  if k.startswith("<did not compile"))
            if not broken:
                break

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
            for path in step["files_test"] + step["files_write"]:
                (PROJECT / path).unlink(missing_ok=True)
            run(["git", "checkout", "--"] + step["files_test"] + step["files_write"],
                check=False)
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
        # ことになる。計画の誤りに見え、構文エラーを直せないプランナーに
        # エスカレーションされる。
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
        bad = sorted({k for k in red.failure_kinds if not RED_KINDS.match(k)})
        if bad:                                                            # R5
            raise Halt("RED_GATE",
                       "R5: failures are not assertions -- the calls themselves are broken",
                       "types seen: " + ", ".join(bad))
        ledger("RED_GATE", step=step_id, tests=red.tests, failures=red.failures, ok=True)

        # --- REVIEW_GATE ------------------------------------------------
        ledger("REVIEW_GATE", step=step_id, skipped=True,
               note="not implemented in v1; this green was not human-reviewed")

        # --- FREEZE -----------------------------------------------------
        manifest = freeze_tests()
        (STATE / "freeze").mkdir(parents=True, exist_ok=True)
        (STATE / "freeze" / f"{step_id}.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")
        ledger("FREEZE", step=step_id, files=len(manifest))

        # --- IMPL / VERIFY ----------------------------------------------
        tests_text = frozen_tests_text(step)
        red_baseline = "\n".join(red.failure_kinds)
        last_failure = red_baseline
        retry_mode = step.get("retry", POLICY["retry"])
        schedule = attempt_schedule(step)

        # このステップ自身のテストファイルを、両方の綴りで持つ。2つのテスト
        # ランナーはレポートでファイルを違う形で呼ぶ。pytest はドット区切りの
        # モジュールのパス（tests.test_models）、vitest は渡したままのパス
        # （tests/engine.test.ts）だ。ドットの形だけにすると、TypeScript では
        # ステップ「自身の」落ちたテストが一致せず、すべてソルバーに回帰として
        # 伝わる。「このステップの前に通っていたテストがいま落ちている」と、
        # 何も通っていなかった最初のステップで言われる。
        own_tests = {Path(p).with_suffix("").as_posix().replace("/", ".")
                     for p in step["files_test"]} | set(step["files_test"])

        timeouts = 0
        while attempt < len(schedule):
            backend = schedule[attempt]
            handover = attempt > 0 and backend != schedule[attempt - 1]
            attempt += 1
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
                # 試行を1回使っただけで、ステップは止めない。残りの試行があれば
                # 次へ進み、使い切れば下の else がいつもどおりエスカレーションする。
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
            regressions = [f for f in green.failed_files if f not in own_tests]
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
            broke = [f for f in last_run.failed_files if f not in own_tests] if last_run else []
            tried = ", ".join(dict.fromkeys(schedule))
            reason = (f"still failing after {attempt} attempts"
                      + (f" across {tried}" if len(SOLVER_TIERS) > 1 else ""))
            if timeouts:
                # プランナーに届く理由に残す。全部が時間切れなら、実装が難しいの
                # ではなく、ステップが大きすぎるか、条件が満たせないことを疑う。
                reason += f"; {timeouts} of them ran out of time"
            if broke:
                # 理由の中で別に名指しする。これは ESCALATION.md を通って
                # プランナーに届き、「このステップは前のステップを壊さずには
                # 作れない」は「このステップはまだできていない」とは別の問題だ
                # からだ。2組の受け入れ条件の矛盾かもしれず、それはソルバーが
                # 解くものではない。
                reason += "; and it now breaks " + ", ".join(broke)
            raise Halt("IMPL", reason, last_run.output[-4000:] if last_run else "")

        # --- GREEN ------------------------------------------------------
        contracts_dir = STATE / "contracts"
        contracts_dir.mkdir(parents=True, exist_ok=True)
        (contracts_dir / f"{step_id}.json").write_text(
            json.dumps(step["contracts"], ensure_ascii=False, indent=2), encoding="utf-8")

        complete_green(step_id, step["goal"], attempt)
        print(f"\nstep {step_id}: GREEN in {attempt} attempt(s)")
        return 0

    except Halt as halt:
        escalate(step, halt, attempt, last_run)
        return 2


# --------------------------------------------------------------------------
# 外側のループ（RUNNER_SPEC 9 章、`run --all`）
# --------------------------------------------------------------------------


# 外側のループを止めるもの。どれもリポジトリか台帳についての客観的な事実で、
# うまくいっているかについての判断は1つも無い。BOOTSTRAP 1-6: 止まる条件は
# 前もって固定する。「迷ったら訊く」は訊く頻度をモデルに委ね、習慣ですべてを
# 承認するところで終わるからだ。
ALL_GREEN = "every step in the plan is green"
CAP_REACHED = "the escalation cap for this step is spent (RUNNER_SPEC 6-2)"
PLAN_REFUSED = "the planner's proposal was refused"
PLANNER_ASKED = "the planner handed the decision to you"
BUDGET_SPENT = "the wall-clock budget is spent"


def cmd_run_all(unvalidated: bool = False, budget_minutes: int = 0) -> int:
    """上の一覧のどれかが止めるまで、計画の順にステップを走らせる。

    ループはこうだ。ステップを走らせる。緑なら次へ進む。エスカレーションしたら、
    プランナーに1回答えさせてやり直す。上限を超えてエスカレーションしたら止まる。

    提案が妥当かはここでは何も決めない。決めるのは `plan apply` で、機械的に
    決め、ここはその終了コードを読むだけだ。この分離があるから、外側のループを
    人の目なしで走らせてよいことになる。
    """
    deadline = time.monotonic() + budget_minutes * 60 if budget_minutes else None

    def out_of_budget() -> bool:
        return deadline is not None and time.monotonic() > deadline

    def stop(reason: str, code: int) -> int:
        ledger("RUN_ALL_STOP", reason=reason)
        print(f"\nstopped: {reason}", file=sys.stderr)
        return code

    # 設定を告げる前に、計画の設定を読み込む。ここで組み込みの既定を報告し、
    # 下のループが計画の値で走ると、台帳が実際に起きたことと食い違う。
    load_settings(json.loads((PLAN / "tasks.json").read_text(encoding="utf-8")))
    ledger("RUN_ALL_START", budget_minutes=budget_minutes or "none",
           escalation_cap=LIMITS["escalations"])

    while True:
        # 毎周、計画を読み直す。前の周で適用された提案が、これから走るステップを
        # 書き換え、その後にステップを足しているかもしれない。
        tasks = json.loads((PLAN / "tasks.json").read_text(encoding="utf-8"))
        load_settings(tasks)
        problems = validate_plan(tasks)
        if problems and not unvalidated:
            raise Halt("PLAN_LOAD", f"plan has {len(problems)} lint violation(s)",
                       "\n".join(problems))

        done = green_steps()
        remaining = [s for s in tasks["steps"] if s["id"] not in done]
        if not remaining:
            complete_run(done, tasks)
            print(f"\nall {len(done)} step(s) green")
            return 0

        step_id = remaining[0]["id"]
        if out_of_budget():
            return stop(f"{BUDGET_SPENT}; {len(remaining)} step(s) left, next is {step_id}", 2)

        print(f"\n=== {step_id} ({len(done)} done, {len(remaining)} to go) ===")
        if run_step(step_id, unvalidated=unvalidated) == 0:
            continue

        # ステップがエスカレーションした。run_step はすでに ESCALATION.md を書いた。
        spent = escalation_count(step_id)
        if spent > LIMITS["escalations"]:
            return stop(f"{CAP_REACHED}: {step_id} escalated {spent} time(s); "
                        f"read {ESCALATION}", 2)
        if out_of_budget():
            return stop(f"{BUDGET_SPENT}; {step_id} is escalated and unanswered", 2)

        if cmd_plan_propose(step_id) != 0:
            return stop(f"the planner produced nothing for {step_id}", 2)

        applied = cmd_plan_apply()
        if applied == 3:
            return stop(f"{PLANNER_ASKED} on {step_id}", 3)
        if applied != 0:
            return stop(f"{PLAN_REFUSED} for {step_id}; {PLANNER_OUT} has it", 2)

        # やり直す前に最後の緑へ戻す。次の試行が、落ちたファイルを継がずに
        # きれいな木から始まるようにだ。改訂した計画はこれで消えない。
        # `plan apply` がコミットしたので、HEAD の一部になっている。
        cmd_reset(step_id)


def cmd_validate() -> int:
    tasks = json.loads((PLAN / "tasks.json").read_text(encoding="utf-8"))
    problems = validate_plan(tasks)
    for problem in problems:
        print(problem)
    print(f"\n{len(problems)} violation(s)" if problems else "plan is valid")
    return 1 if problems else 0


def cmd_reset(step_id: str) -> int:
    """止まったステップを走らせ直せるよう、木を最後の緑に戻す。

    RED_GATE で止まったステップは、tests/ を凍結したまま、作業ツリーに変更を
    残し、次の試行は始まるのを拒む。手で戻すには chmod、git reset、git clean を
    正しい順に行う必要がある。間違えやすく、しかも前の試行のファイルを黙って次に
    持ち込む形で間違える。

    順番に意味がある。chmod の前に引き取る（ランナーは所有しないものを chmod
    できない）。git の前に chmod する（FREEZE は tests/ を読み取り専用にし、git は
    書けないディレクトリの中のファイルを消せない）。
    """
    adopt(TESTS, SRC)
    set_writable(tests=True, src=True)

    # 台帳は git の管理下にある（GREEN がコミットする）ので、`reset --hard` は
    # 台帳を最後の緑まで戻し、捨てようとしている試行の記録を、理由を書いた
    # ESCALATED も含めて一緒に消す。失敗の記録だけを失う追記専用の台帳は、
    # 無いより悪い。
    kept = LEDGER.read_bytes() if LEDGER.exists() else b""

    run(["git", "reset", "--hard", "HEAD"], check=True)
    run(["git", "clean", "-fdq"], check=True)   # -x は付けない。.venv と .runner は残す

    if kept:
        LEDGER.write_bytes(kept)

    manifest = STATE / "freeze" / f"{step_id}.json"
    manifest.unlink(missing_ok=True)
    ESCALATION.unlink(missing_ok=True)

    ledger("RESET", step=step_id, note="tree restored to HEAD; freeze manifest and "
                                       "ESCALATION.md discarded")
    print(f"step {step_id}: reset to {run(['git', 'log', '--oneline', '-1']).stdout.strip()}")
    return 0


# solver が読めてはならないディレクトリ。計画（全ステップの受け入れ条件、仕様、
# 台帳）、凍結のマニフェストと契約、git の履歴。BOOTSTRAP 1-5（solver は
# 1ステップ分の情報を受け取り、それ以上は受け取らない）は、この3つのモードに
# ついての主張だ。
PRIVATE_DIRS = (PLAN, STATE, PROJECT / ".git")


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


def main() -> int:
    parser = argparse.ArgumentParser(description="loop runner v1")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate", help="lint plan/tasks.json (RUNNER_SPEC section 8)")
    run_cmd = sub.add_parser("run", help="run one step, or the whole plan with --all")
    run_cmd.add_argument("step_id", nargs="?",
                         help="the step to run; omit it and pass --all instead")
    run_cmd.add_argument("--all", action="store_true",
                         help="run every step that is not green yet, answering "
                              "escalations through the planner until one of the "
                              "stopping conditions is met")
    run_cmd.add_argument("--budget", type=int, default=0, metavar="MINUTES",
                         help="with --all: stop between steps once this much wall "
                              "clock has passed (default: no limit)")
    run_cmd.add_argument("--unvalidated", action="store_true",
                         help="run despite lint violations; they are written to the ledger")
    reset_cmd = sub.add_parser("reset", help="discard a halted step and return to the last green")
    reset_cmd.add_argument("step_id")

    # プランナーの経路。1つではなく、あえて3つの動詞に分ける。`propose` は
    # お金を使い、`apply` はループが測られる基準を変える。提案が入る前に読みたい
    # 人間は、そのどちらも起こさずに読めなければならない。
    plan_cmd = sub.add_parser("plan", help="the planner channel")
    plan_sub = plan_cmd.add_subparsers(dest="plan_cmd", required=True)
    boot_cmd = plan_sub.add_parser(
        "bootstrap", help="ask the planner for a first plan, from the requirements")
    boot_cmd.add_argument("--from", dest="source", default=None, metavar="PATH",
                          help=f"the requirements file (default: {REQUIREMENTS})")
    boot_cmd.add_argument("--language", default="python", choices=sorted(LANGUAGES),
                          help="which toolchain the plan is written for; the "
                               "runner stamps it into the plan (default: python)")
    propose_cmd = plan_sub.add_parser(
        "propose", help="ask the planner to revise the plan in answer to ESCALATION.md")
    propose_cmd.add_argument("--step", default=None,
                             help="the step that halted; named in the brief")
    plan_sub.add_parser("show", help="print the pending proposal without applying it")
    plan_sub.add_parser("apply", help="check the pending proposal and apply it if it passes")

    # クリティック。`plan apply` の一手順ではなく別の動詞にする。プランナーの
    # 経路を3つの動詞にしたのと同じ理由で、お金を使い、意見に払う前に計画を
    # 読みたい人間は、読めなければならない。
    critique_cmd = sub.add_parser(
        "critique", help="ask the critic what is wrong with the plan on disk")
    critique_cmd.add_argument("--mode", action="append", choices=list(CRITIQUE_MODES),
                              help="repeatable; default is every mode, because "
                                   "the two find disjoint kinds of defect")

    refine_cmd = plan_sub.add_parser(
        "refine", help="critique the pending proposal, hand the findings back to "
                       "the planner, and repeat until it is clean or capped")
    refine_cmd.add_argument("--mode", action="append", choices=list(CRITIQUE_MODES),
                            help="repeatable; default is every mode")

    args = parser.parse_args()

    if os.geteuid() == 0:
        print("refusing to run as root: this must run as `runner`", file=sys.stderr)
        return 1

    if fence_is_open():
        return 1

    try:
        if args.cmd == "validate":
            return cmd_validate()
        if args.cmd == "reset":
            return cmd_reset(args.step_id)
        if args.cmd == "plan":
            if args.plan_cmd == "bootstrap":
                return cmd_plan_bootstrap(args.source, args.language)
            if args.plan_cmd == "propose":
                return cmd_plan_propose(args.step)
            if args.plan_cmd == "refine":
                return cmd_plan_refine(args.mode or list(CRITIQUE_MODES))
            if args.plan_cmd == "show":
                return cmd_plan_show()
            return cmd_plan_apply()
        if args.cmd == "critique":
            return cmd_critique(args.mode or list(CRITIQUE_MODES))
        if args.all:
            if args.step_id:
                print("run takes a step id or --all, not both", file=sys.stderr)
                return 1
            return cmd_run_all(unvalidated=args.unvalidated,
                               budget_minutes=args.budget)
        if not args.step_id:
            print("run needs a step id, or --all", file=sys.stderr)
            return 1
        return run_step(args.step_id, unvalidated=args.unvalidated)
    except Halt as halt:
        print(f"HALT [{halt.phase}] {halt.reason}\n{halt.detail}", file=sys.stderr)
        return 2
    except QuotaExhausted as quota:
        # エスカレーションはしていない。計画にもソルバーにも落ち度は無く、
        # 枠が戻れば同じところからやり直せる。
        waited = QUOTA["waits"] * QUOTA["wait_seconds"] // 60
        print(f"QUOTA [{quota.phase}] {quota.who} is still out of quota ({quota.kind}) "
              f"after waiting {waited} minutes. Nothing was escalated.\n"
              f"When the limit has reset: if a step was in progress, run "
              f"`reset <step-id>` first, then run the same command again.",
              file=sys.stderr)
        return 5
    except FileNotFoundError as missing:
        # ほぼ必ず、まだ計画の無い新しいプロジェクトだ。tasks.json についての
        # トレースバックを出さず、そう言う。
        print(f"missing: {missing.filename}", file=sys.stderr)
        if str(missing.filename or "").endswith("tasks.json"):
            print(f"there is no plan yet. Write the requirements to "
                  f"{REQUIREMENTS} and run: plan bootstrap", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
