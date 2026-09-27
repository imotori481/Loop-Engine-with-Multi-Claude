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
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

# 箱が置いた環境のファイルと計画。PR には要らない。docs/COMMANDS.md の手順と同じ。
REMOVED = ("plan", "conftest.py", "index.html", "vitest.config.mjs")
# .gitignore は箱が行を足している。親ブランチの版に戻し、無ければ消す。
GITIGNORE = ".gitignore"
BASE_KEY = "branch.{}.loopBase"
PR_SUFFIX = "-pr"
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
        with self._lock:
            return self._open(steps, note)

    def _open(self, steps: list[str], note: str) -> dict[str, Any]:
        clone, branch, head = self.locate()
        branch = safe_ref(branch, "the working branch")
        base = self._git(clone, "config", "--get", BASE_KEY.format(branch), check=False)
        if not base:
            raise PullRequestError(
                f"the parent branch of {branch} is not recorded. In {clone} run: "
                f"git config {BASE_KEY.format(branch)} <base-branch>")
        base = safe_ref(base, BASE_KEY.format(branch))
        head_branch = branch + PR_SUFFIX
        repo = github_repo(self._git(clone, "remote", "get-url", "origin"))

        self._git(clone, "fetch", "--quiet", "origin", base)
        commit = self._strip(clone, head, base)
        # <branch>-pr は承認のたびにここで作り直すので、前の版の上には積まない。
        self._git(clone, "push", "--quiet", "--force", "origin",
                  f"{commit}:refs/heads/{head_branch}")

        existing = self._cmd(["gh", "pr", "list", "--repo", repo, "--head", head_branch,
                              "--base", base, "--state", "open",
                              "--json", "url", "--jq", ".[0].url"], clone)
        if existing and existing != "null":
            url, created = existing, False
        else:
            url = self._cmd(["gh", "pr", "create", "--repo", repo, "--base", base,
                             "--head", head_branch, "--title", f"{branch}: Loop Engine の成果",
                             "--body", pr_body(branch, steps, note)],
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
