"""予定レビューを承認したら、承認した状態を親ブランチへの PR として出す。

GitHub とやり取りするのはホストだけだ。箱には GitHub の資格情報を置かない。
ここで使うのは、`loop-import` が作ったクローン（`<mirrors>\\projects\\<name>`）と、
その中の `origin`、それにホストの `gh` の認証だ。

作業ツリーには一切触れない。クローンには、Unity が開いて書き換えた未コミットの
アセットが普通に残っている。ブランチを切り替えれば、それを巻き込むか、切り替えが
断られる。PR のコミットは一時的な index の上で組み立てる。

PR に出すのは、写し `project` の HEAD と同じコミットだけだ。承認はその HEAD の
ALL_GREEN に対してなされた。クローンの `loop/<branch>` がそれと違えば、承認して
いないものを出すことになるので断る。

コマンドは引数配列のまま shell=False で流す。HTTP の値はブランチ名にも引数にも
入らない。入るのは、人が書いた承認のメモを PR の本文にするところだけだ。

全ステップが緑になる前の状態は、下書きの PR として出せる（`loop-pr`）。緑の判定には
触れない。出すのは写しの HEAD、つまり最後に緑になったステップのコミットで、本文に
残りのステップを並べる。承認の PR とはブランチを分け、承認で作り直されないようにする。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

try:
    from .state import read_jsonl
except ImportError:  # 直接実行したとき: python host/dashboard/pullrequest.py
    from state import read_jsonl

# 箱が置いた環境のファイルと計画。PR には要らない。docs/COMMANDS.md の手順と同じ。
REMOVED = ("plan", "conftest.py", "index.html", "vitest.config.mjs")
# .gitignore は箱が行を足している。親ブランチの版に戻し、無ければ消す。
GITIGNORE = ".gitignore"
BASE_KEY = "branch.{}.loopBase"
PR_SUFFIX = "-pr"
DRAFT_SUFFIX = "-draft"
# loop-pull のライブの写し。loop-dashboard.cmd の PROJECT と同じ。
MIRROR = Path(r"C:\dev\roop-engin\project")
TIMEOUT_SECONDS = 120
GITHUB = re.compile(r"github\.com[:/](?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$")

Runner = Callable[..., subprocess.CompletedProcess]


class PullRequestError(ValueError):
    pass


def github_repo(url: str) -> str:
    """origin の URL から owner/name を取り出す。gh はリモートが2つあると迷うので明示する。"""
    match = GITHUB.search(url.strip())
    if match is None:
        raise PullRequestError(f"origin is not a GitHub repository: {url.strip()}")
    return match.group("repo")


def pr_body(branch: str, steps: list[str], note: str) -> str:
    lines = ["Loop Engine が全ステップを緑にし、人が成果物を動かして承認した。", "",
             f"- ステップ: {', '.join(steps) or '-'}",
             f"- 作業ブランチ: `{branch}` から、計画と箱の環境のファイルを除いた版"]
    if note.strip():
        lines += ["", "## 承認のメモ", "", note.strip()]
    return "\n".join(lines) + "\n"


def draft_body(branch: str, green: list[str], remaining: list[str]) -> str:
    return "\n".join([
        "Loop Engine が途中まで緑にした状態。残りのステップは緑になっておらず、人の承認もまだ。", "",
        f"- 緑のステップ: {', '.join(green)}",
        f"- 残りのステップ: {', '.join(remaining)}",
        f"- 作業ブランチ: `{branch}` から、計画と箱の環境のファイルを除いた版"]) + "\n"


def draft_steps(mirror: Path) -> tuple[list[str], list[str]]:
    """写しの計画の、緑のステップと残りのステップ。下書きにできなければ断る。"""
    ledger = read_jsonl(mirror / "plan" / "ledger.jsonl")
    try:
        tasks = json.loads((mirror / "plan" / "tasks.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PullRequestError(f"cannot read the plan in {mirror}: {error}") from error
    steps = tasks.get("steps", []) if isinstance(tasks, dict) else []
    ids = [step.get("id") for step in steps if isinstance(step, dict) and step.get("id")]
    done = {record.get("step") for record in ledger if record.get("event") == "GREEN"}
    green = [step for step in ids if step in done]
    remaining = [step for step in ids if step not in done]
    if not green:
        raise PullRequestError("no step is green yet. There is nothing to send")
    if not remaining:
        raise PullRequestError(
            "every step is green. Approve the review in the dashboard to send the pull request")
    return green, remaining


def safe_ref(name: str, what: str) -> str:
    # 引数になる値。オプションとして読まれうるものと、空は受け付けない。
    if not name or name.startswith("-") or any(c.isspace() for c in name):
        raise PullRequestError(f"{what} is not a usable branch name: {name!r}")
    return name


class PullRequests:
    def __init__(self, mirror: Path, clones: Path, run: Runner = subprocess.run):
        self.mirror = mirror
        self.clones = clones
        self._run = run
        # 同じ PR を2つの要求が同時に作り直さないように。
        self._lock = threading.Lock()

    def _cmd(self, argv: list[str], cwd: Path, env: dict[str, str] | None = None,
             check: bool = True) -> str:
        try:
            proc = self._run(argv, cwd=str(cwd), shell=False, capture_output=True, text=True,
                             encoding="utf-8", timeout=TIMEOUT_SECONDS,
                             env={**os.environ, **env} if env else None)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PullRequestError(f"{argv[0]} could not run: {error}") from error
        if check and proc.returncode != 0:
            reason = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise PullRequestError(f"{' '.join(argv[:3])} failed"
                                   + (f": {reason[-1]}" if reason else ""))
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def _git(self, clone: Path, *args: str, env: dict[str, str] | None = None,
             check: bool = True) -> str:
        return self._cmd(["git", "-C", str(clone), *args], clone, env, check)

    def locate(self) -> tuple[Path, str, str]:
        """写しの HEAD を loop/<branch> に持つクローンと、そのブランチと、コミット。"""
        head = self._git(self.mirror, "rev-parse", "HEAD")
        if not self.clones.is_dir():
            raise PullRequestError(f"no imported projects in {self.clones}")
        for clone in sorted(self.clones.iterdir()):
            if not (clone / ".git").exists():
                continue
            refs = self._git(clone, "for-each-ref", "refs/remotes/loop",
                             "--format=%(objectname) %(refname)", check=False)
            for line in refs.splitlines():
                sha, _, ref = line.partition(" ")
                if sha == head and ref != "refs/remotes/loop/HEAD":
                    return clone, ref.removeprefix("refs/remotes/loop/"), head
        raise PullRequestError(
            "the approved commit is in no imported project's loop/<branch>. "
            "Run loop-pull, or the project was not made with loop-import")

    def open(self, steps: list[str], note: str) -> dict[str, Any]:
        """承認した ALL_GREEN を、<branch>-pr からの PR にする。"""
        with self._lock:
            return self._open(PR_SUFFIX, lambda branch: (
                f"{branch}: Loop Engine の成果", pr_body(branch, steps, note)), draft=False)

    def open_draft(self) -> dict[str, Any]:
        """最後に緑になったステップまでを、<branch>-draft からの下書きの PR にする。"""
        green, remaining = draft_steps(self.mirror)
        with self._lock:
            result = self._open(DRAFT_SUFFIX, lambda branch: (
                f"{branch}: Loop Engine の途中の成果", draft_body(branch, green, remaining)),
                draft=True)
        return {**result, "green": green, "remaining": remaining}

    def _open(self, suffix: str, describe: Callable[[str], tuple[str, str]],
              draft: bool) -> dict[str, Any]:
        clone, branch, head = self.locate()
        branch = safe_ref(branch, "the working branch")
        base = self._git(clone, "config", "--get", BASE_KEY.format(branch), check=False)
        if not base:
            raise PullRequestError(
                f"the parent branch of {branch} is not recorded. In {clone} run: "
                f"git config {BASE_KEY.format(branch)} <base-branch>")
        base = safe_ref(base, BASE_KEY.format(branch))
        head_branch = branch + suffix
        repo = github_repo(self._git(clone, "remote", "get-url", "origin"))

        self._git(clone, "fetch", "--quiet", "origin", base)
        commit = self._strip(clone, head, base)
        # <branch>-pr と <branch>-draft は出すたびにここで作り直すので、前の版の上には積まない。
        self._git(clone, "push", "--quiet", "--force", "origin",
                  f"{commit}:refs/heads/{head_branch}")

        existing = self._cmd(["gh", "pr", "list", "--repo", repo, "--head", head_branch,
                              "--base", base, "--state", "open",
                              "--json", "url", "--jq", ".[0].url"], clone)
        title, body = describe(branch)
        if existing and existing != "null":
            url, created = existing, False
            if draft:
                # 下書きは緑が増えるたびに出し直すので、本文のステップの一覧も合わせる。
                self._cmd(["gh", "pr", "edit", url, "--repo", repo, "--body", body], clone)
        else:
            url = self._cmd(["gh", "pr", "create", "--repo", repo, "--base", base,
                             "--head", head_branch, "--title", title, "--body", body,
                             *(["--draft"] if draft else [])],
                            clone).splitlines()[-1]
            created = True
        return {"url": url, "created": created, "project": clone.name,
                "branch": branch, "head": head_branch, "base": base, "commit": commit}

    def _strip(self, clone: Path, head: str, base: str) -> str:
        """head から環境のファイルを除いたコミットを、作業ツリーに触れずに作る。"""
        with tempfile.TemporaryDirectory() as temp:
            env = {"GIT_INDEX_FILE": str(Path(temp) / "index")}
            self._git(clone, "read-tree", head, env=env)
            self._git(clone, "rm", "--cached", "-r", "-q", "--ignore-unmatch", "--",
                      *REMOVED, env=env)
            blob = self._git(clone, "rev-parse", "-q", "--verify",
                             f"origin/{base}:{GITIGNORE}", check=False)
            if blob:
                self._git(clone, "update-index", "--add", "--cacheinfo",
                          f"100644,{blob},{GITIGNORE}", env=env)
            else:
                self._git(clone, "rm", "--cached", "-q", "--ignore-unmatch", "--",
                          GITIGNORE, env=env)
            tree = self._git(clone, "write-tree", env=env)
        return self._git(clone, "commit-tree", tree, "-p", head,
                         "-m", "chore: Loop Engineの環境のファイルを除く")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="loop-pr",
        description="send the steps that are green so far as a draft pull request")
    parser.add_argument("--project", type=Path, default=MIRROR,
                        help=f"the live mirror loop-pull writes (default: {MIRROR})")
    args = parser.parse_args(argv)
    mirror = args.project.resolve()
    try:
        result = PullRequests(mirror, mirror.parent / "projects").open_draft()
    except PullRequestError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"green:     {', '.join(result['green'])}")
    print(f"remaining: {', '.join(result['remaining'])}")
    print(f"{result['head']} -> {result['base']}  "
          f"({'opened' if result['created'] else 'updated'})")
    print(result["url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
