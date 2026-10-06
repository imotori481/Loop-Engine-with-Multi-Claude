"""承認した状態を、環境のファイルを除いて親ブランチへの PR にすること。

origin と箱の bare はローカルの一時リポジトリで、git は本物を使う。偽物にするのは
gh だけだ。
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from host.dashboard.pullrequest import PullRequestError, PullRequests, github_repo


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout.strip()


def commit_files(repo, files, message):
    for name, content in files.items():
        path = Path(repo) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


class Repositories(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        identity = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        patcher = mock.patch.dict(os.environ, identity)
        patcher.start()
        self.addCleanup(patcher.stop)

        # GitHub の代わり。develop が親ブランチ。
        self.origin = root / "origin.git"
        seed = root / "seed"
        git(root, "init", "-q", "-b", "develop", str(seed))
        commit_files(seed, {".gitignore": "*.tmp\n", "a.txt": "a\n"}, "base")
        git(root, "clone", "-q", "--bare", str(seed), str(self.origin))

        # loop-import が作るクローン。
        self.clones = root / "projects"
        self.clone = self.clones / "game"
        git(root, "clone", "-q", str(self.origin), str(self.clone))
        git(self.clone, "switch", "-q", "--no-track", "-c", "feat", "origin/develop")
        git(self.clone, "config", "branch.feat.loopBase", "develop")

        # 箱の bare。計画と環境のファイルと、作業の成果が載る。
        box = root / "box"
        git(root, "clone", "-q", "-b", "develop", str(self.origin), str(box))
        git(box, "switch", "-q", "-c", "feat")
        commit_files(box, {".gitignore": "*.tmp\n.venv/\n", "conftest.py": "x\n",
                           "plan/tasks.json": "{}\n", "src/game.cs": "class Game {}\n"}, "S1")
        self.bare = root / "box.git"
        git(root, "clone", "-q", "--bare", str(box), str(self.bare))
        git(self.clone, "remote", "add", "loop", str(self.bare))
        git(self.clone, "fetch", "-q", "loop", "+refs/heads/*:refs/remotes/loop/*")
        self.approved = git(box, "rev-parse", "HEAD")

        # loop-pull のライブの写し。HEAD が承認した状態。
        self.mirror = root / "project"
        git(root, "clone", "-q", "-b", "feat", str(self.bare), str(self.mirror))

        # Unity が書き換えた未コミットのアセット。
        (self.clone / "a.txt").write_text("edited in Unity\n", encoding="utf-8")

        self.gh_calls = []
        self.gh_list = ""

    def fake_run(self, argv, **kwargs):
        if argv[0] == "gh":
            self.gh_calls.append(argv)
            out = self.gh_list if argv[2] == "list" else "https://github.com/o/r/pull/1\n"
            return subprocess.CompletedProcess(argv, 0, out, "")
        if argv[:3] == ["git", "-C", str(self.clone)] and argv[3:5] == ["remote", "get-url"]:
            return subprocess.CompletedProcess(argv, 0, "https://github.com/o/r.git\n", "")
        return subprocess.run(argv, **kwargs)

    def pulls(self):
        return PullRequests(self.mirror, self.clones, run=self.fake_run)


class Opening(Repositories):
    def test_the_pr_branch_drops_the_plan_and_the_environment(self):
        result = self.pulls().open(["S1"], "played it")
        self.assertEqual((result["head"], result["base"], result["created"]),
                         ("feat-pr", "develop", True))
        files = git(self.origin, "ls-tree", "-r", "--name-only", "feat-pr").splitlines()
        self.assertEqual(sorted(files), [".gitignore", "a.txt", "src/game.cs"])
        self.assertEqual(git(self.origin, "show", "feat-pr:.gitignore"), "*.tmp")
        self.assertEqual(git(self.origin, "rev-parse", "feat-pr^"), self.approved)

    def test_the_working_tree_of_the_clone_is_not_touched(self):
        self.pulls().open(["S1"], "")
        self.assertEqual(git(self.clone, "branch", "--show-current"), "feat")
        self.assertEqual((self.clone / "a.txt").read_text(encoding="utf-8"), "edited in Unity\n")

    def test_the_pr_goes_to_the_recorded_parent(self):
        self.pulls().open(["S1", "S2"], "clear works")
        create = self.gh_calls[-1]
        self.assertEqual(create[:3], ["gh", "pr", "create"])
        self.assertEqual(create[create.index("--base") + 1], "develop")
        self.assertEqual(create[create.index("--repo") + 1], "o/r")
        self.assertIn("clear works", create[create.index("--body") + 1])

    def test_an_open_pr_is_updated_instead_of_opened_twice(self):
        self.gh_list = "https://github.com/o/r/pull/7\n"
        result = self.pulls().open(["S1"], "")
        self.assertEqual((result["url"], result["created"]),
                         ("https://github.com/o/r/pull/7", False))
        self.assertEqual([call[2] for call in self.gh_calls], ["list"])

    def test_a_branch_without_a_recorded_parent_is_refused(self):
        git(self.clone, "config", "--unset", "branch.feat.loopBase")
        with self.assertRaisesRegex(PullRequestError, "git config branch.feat.loopBase"):
            self.pulls().open(["S1"], "")
        self.assertEqual(self.gh_calls, [])

    def test_a_mirror_ahead_of_the_clone_is_refused(self):
        # 承認したものと違うものを出さない。loop-pull が済んでいない状態。
        commit_files(self.mirror, {"b.txt": "b\n"}, "later")
        with self.assertRaisesRegex(PullRequestError, "Run loop-pull"):
            self.pulls().open(["S1"], "")


class Drafting(Repositories):
    def plan(self, *green):
        plan = self.mirror / "plan"
        (plan / "tasks.json").write_text(
            '{"steps": [{"id": "S1"}, {"id": "S2"}, {"id": "S3"}]}', encoding="utf-8")
        (plan / "ledger.jsonl").write_text("".join(
            f'{{"event": "GREEN", "step": "{step}"}}\n' for step in green), encoding="utf-8")

    def test_the_draft_goes_from_its_own_branch(self):
        self.plan("S1")
        result = self.pulls().open_draft()
        self.assertEqual((result["head"], result["green"], result["remaining"]),
                         ("feat-draft", ["S1"], ["S2", "S3"]))
        files = git(self.origin, "ls-tree", "-r", "--name-only", "feat-draft").splitlines()
        self.assertEqual(sorted(files), [".gitignore", "a.txt", "src/game.cs"])
        self.assertEqual(git(self.origin, "rev-parse", "feat-draft^"), self.approved)
        create = self.gh_calls[-1]
        self.assertIn("--draft", create)
        self.assertIn("S2, S3", create[create.index("--body") + 1])

    def test_the_approval_pr_is_not_a_draft(self):
        self.pulls().open(["S1"], "")
        self.assertNotIn("--draft", self.gh_calls[-1])

    def test_an_open_draft_gets_the_new_list_of_steps(self):
        self.plan("S1", "S2")
        self.gh_list = "https://github.com/o/r/pull/7\n"
        result = self.pulls().open_draft()
        self.assertFalse(result["created"])
        edit = self.gh_calls[-1]
        self.assertEqual(edit[:4], ["gh", "pr", "edit", "https://github.com/o/r/pull/7"])
        self.assertIn("S3", edit[edit.index("--body") + 1])

    def test_nothing_is_sent_before_the_first_green(self):
        self.plan()
        with self.assertRaisesRegex(PullRequestError, "no step is green"):
            self.pulls().open_draft()
        self.assertEqual(self.gh_calls, [])

    def test_a_finished_plan_is_left_to_the_review(self):
        self.plan("S1", "S2", "S3")
        with self.assertRaisesRegex(PullRequestError, "Approve the review"):
            self.pulls().open_draft()
        self.assertEqual(self.gh_calls, [])


class Names(unittest.TestCase):
    def test_the_repository_is_read_from_either_url_form(self):
        self.assertEqual(github_repo("https://github.com/o/r.git"), "o/r")
        self.assertEqual(github_repo("git@github.com:o/r.git"), "o/r")
        self.assertEqual(github_repo("https://github.com/o/r"), "o/r")
        with self.assertRaises(PullRequestError):
            github_repo("ssh://loop-runner/srv/loop/repo.git")


if __name__ == "__main__":
    unittest.main()
