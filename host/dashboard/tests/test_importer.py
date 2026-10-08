"""取り込みは、確かめた値だけを git の引数にし、loop-import.cmd と同じ順に5つの手順を流す。

git は本物を使い、箱だけを差し替える。クローン元と箱の bare は一時ディレクトリに置く。
"""

import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from host.dashboard.importer import ImportFailed, Importer, ImportJob, Request, check


def git(*args, cwd=None):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                           "-c", "init.defaultBranch=main", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FakeLive:
    """箱。init と use を記録し、切り替えはすぐ終わったことにする。"""

    def __init__(self, refuse_init=False, ends_on=None):
        self.calls = []
        self.refuse_init = refuse_init
        self.ends_on = ends_on

    def init_project(self, name, branch, src="", tests=""):
        self.calls.append(("init", name, branch, src, tests))
        if self.refuse_init:
            raise ValueError("loop-project: /srv/loop/projects/game がもうある")
        return "/srv/loop/projects/game/repo.git を作った"

    def use_project(self, name):
        self.calls.append(("use", name))
        self.used = name
        return f"'{name}' への切り替えを裏で始めた"

    def fetch(self):
        return {"project": self.ends_on or self.used,
                "switch": {"running": False, "log": ["今のプロジェクト: game"]}}


class LocalImporter(Importer):
    """push の宛先を、loop-runner の代わりに手元の bare にする。"""

    def __init__(self, live, workroot, bare):
        super().__init__(live, workroot, sleep=lambda seconds: None)
        self.bare = bare

    def push_target(self, project):
        return str(self.bare)


class Checks(unittest.TestCase):
    def ok(self, **fields):
        return check({"project": "game", "url": "https://github.com/o/r.git", "branch": "loop/x",
                      **fields})

    def test_the_usual_forms_pass(self):
        self.assertEqual(self.ok(), Request("game", "https://github.com/o/r.git", "loop/x"))
        self.assertEqual(self.ok(url="git@github.com:o/r.git").url, "git@github.com:o/r.git")
        self.assertEqual(self.ok(url="ssh://git@host:22/o/r.git", base="main",
                                 src="Assets/Source", tests="Assets/Tests").tests, "Assets/Tests")

    def test_a_url_that_reaches_the_host_or_reads_as_an_option_is_refused(self):
        for url in ("file:///C:/x", "ext::sh -c calc", "-oProxyCommand=calc@h:x", "C:\\repo",
                    "https://h/x y", "https://h/%0a", "ssh://-oProxyCommand=calc/x", ""):
            with self.assertRaises(ValueError, msg=url):
                self.ok(url=url)

    def test_names_follow_the_rules_of_the_sandbox(self):
        for project in ("Game", "CURRENT", "../x", "-x", ""):
            with self.assertRaises(ValueError, msg=project):
                self.ok(project=project)
        for branch in ("", "-b", "a..b", "a b", "a;b"):
            with self.assertRaises(ValueError, msg=branch):
                self.ok(branch=branch)
        with self.assertRaises(ValueError):
            self.ok(base="-x")
        with self.assertRaises(ValueError):
            self.ok(src="a b")
        with self.assertRaises(ValueError):
            check({"project": "game", "url": "https://h/r", "branch": "b", "src": 5})


class Run(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.origin = root / "origin"
        self.origin.mkdir()
        git("init", "-q", cwd=self.origin)
        (self.origin / "main.cpp").write_text("int WinMain() { return 0; }\n", encoding="utf-8")
        git("add", ".", cwd=self.origin)
        git("commit", "-q", "-m", "first", cwd=self.origin)
        git("branch", "develop", cwd=self.origin)
        self.bare = root / "box.git"
        git("init", "-q", "--bare", str(self.bare))
        self.workroot = root / "projects"
        self.lines = []

    def run_import(self, live, **fields):
        request = Request(**{"project": "game", "url": str(self.origin), "branch": "loop/x",
                             **fields})
        LocalImporter(live, self.workroot, self.bare).run(request, self.lines.append)

    def config(self, key):
        proc = subprocess.run(["git", "-C", str(self.workroot / "game"), "config", key],
                              capture_output=True, text=True)
        return proc.stdout.strip()

    def test_a_new_branch_is_made_from_the_default_branch_pushed_and_switched_to(self):
        live = FakeLive()
        self.run_import(live)
        self.assertEqual(live.calls, [("init", "game", "loop/x", "", ""), ("use", "game")])
        self.assertTrue(git("--git-dir", str(self.bare), "rev-parse", "refs/heads/loop/x"))
        self.assertEqual(self.config("branch.loop/x.loopBase"), "main")
        self.assertEqual(self.lines[-1].split(".")[0], "OK")

    def test_the_base_and_the_fences_are_passed_on(self):
        live = FakeLive()
        self.run_import(live, base="develop", src="code", tests="spec")
        self.assertEqual(live.calls[0], ("init", "game", "loop/x", "code", "spec"))
        self.assertEqual(self.config("branch.loop/x.loopBase"), "develop")

    def test_an_existing_branch_is_used_and_no_parent_is_guessed(self):
        live = FakeLive()
        self.run_import(live, branch="develop")
        self.assertEqual(self.config("branch.develop.loopBase"), "")
        self.assertTrue(any("loopBase <base-branch>" in line for line in self.lines))

    def test_a_clone_of_another_repository_is_not_reused(self):
        (self.workroot / "game").mkdir(parents=True)
        git("init", "-q", cwd=self.workroot / "game")
        git("remote", "add", "origin", "https://example.com/other.git", cwd=self.workroot / "game")
        live = FakeLive()
        with self.assertRaisesRegex(ImportFailed, "is a clone of"):
            self.run_import(live)
        self.assertEqual(live.calls, [])

    def test_uncommitted_changes_stop_the_import(self):
        self.run_import(FakeLive())
        (self.workroot / "game" / "main.cpp").write_text("changed\n", encoding="utf-8")
        live = FakeLive()
        with self.assertRaisesRegex(ImportFailed, "uncommitted"):
            self.run_import(live)
        self.assertEqual(live.calls, [])

    def test_nothing_is_pushed_when_the_sandbox_refuses_the_project(self):
        live = FakeLive(refuse_init=True)
        with self.assertRaisesRegex(ImportFailed, "Nothing was pushed"):
            self.run_import(live)
        self.assertEqual(git("--git-dir", str(self.bare), "for-each-ref"), "")
        self.assertEqual([call[0] for call in live.calls], ["init"])

    def test_a_switch_that_ends_elsewhere_is_a_failure(self):
        with self.assertRaisesRegex(ImportFailed, "current project is 'thm'"):
            self.run_import(FakeLive(ends_on="thm"))


class FakeImporter:
    def __init__(self, gate=None, fail=False):
        self.gate, self.fail = gate, fail

    def run(self, request, say):
        say(f"[1/5] Cloning {request.url} ...")
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail:
            raise ImportFailed("git clone failed: not found")


class Job(unittest.TestCase):
    BODY = {"project": "game", "url": "https://github.com/o/r.git", "branch": "loop/x"}

    def test_a_bad_request_is_refused_before_anything_runs(self):
        job = ImportJob(FakeImporter())
        with self.assertRaises(ValueError):
            job.start({**self.BODY, "url": "file:///x"})
        self.assertFalse(job.status()["running"])

    def test_only_one_runs_at_a_time_and_the_output_is_kept(self):
        gate = threading.Event()
        job = ImportJob(FakeImporter(gate))
        job.start(self.BODY)
        with self.assertRaisesRegex(ValueError, "already running"):
            job.start(self.BODY)
        gate.set()
        job.thread.join(5)
        status = job.status()
        self.assertEqual((status["running"], status["ok"], status["project"]), (False, True, "game"))
        self.assertEqual(status["output"], ["[1/5] Cloning https://github.com/o/r.git ..."])

    def test_a_failure_is_shown_not_raised(self):
        job = ImportJob(FakeImporter(fail=True))
        job.start(self.BODY)
        job.thread.join(5)
        status = job.status()
        self.assertFalse(status["ok"])
        self.assertEqual(status["output"][-1], "ERROR: git clone failed: not found")


if __name__ == "__main__":
    unittest.main()
