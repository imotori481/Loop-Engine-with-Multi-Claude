#!/usr/bin/env bash
# Directory layout and git repositories. Idempotent.
#
# Everything lives on the distro's own ext4 filesystem inside the VHDX. Nothing
# lives on a Windows path: /mnt/c is not mounted (automount is off), and even if
# it were, DrvFs cannot represent the ownership and mode bits that 40-perms.sh
# depends on -- the test freeze would stop being enforceable.
#
#   /srv/loop/repo.git   bare repo. The planner (on Windows) pushes here
#                        over SSH as `runner`; the runner pulls from it.
#   /srv/loop/project    working tree the loop actually runs in
#   /srv/loop/brief      runner writes the per-step brief here, solver reads it.
#                        This is the ONLY channel from runner to solver, which
#                        is what keeps RUNNER_SPEC 5 honest: solver never reads
#                        plan/ and therefore never sees tasks.json.
set -euo pipefail

# /srv/loop そのものは root 所有。runner の所有にすると、runner は直下の
# エントリを改名できる。中身が root 所有でも関係なく、bin/ を退けて自分の
# solver-run を置けば、sudoers はパスで許可しているので、それを solver として
# 実行できる。runner/ を退ければ関門のコードも差し替えられる。
# runner が書く場所は、下で1つずつ runner に渡す。
install -d -o root -g root -m 755 /srv/loop
# setgid: briefs written here by runner must land in group solverw, or the
# solver cannot read the one channel it has.
install -d -o runner -g solverw -m 2750 /srv/loop/brief
# 走行ログの置き場。runner は /srv/loop の直下に書けないので、ここに書く。
install -d -o runner -g runner -m 755 /srv/loop/logs

# repo.git と project は runner が中身を作る。直下に作る権限は runner に無いので、
# 空のディレクトリを root が runner 所有で先に用意する。git init --bare も
# git clone も、空の既存ディレクトリをそのまま使える。
# 無いときだけ作る。project は 40-perms.sh が 3770 に締めるので、ここで毎回
# 755 に戻すと、このスクリプトを単独で流したときに誰からでも読める状態になる。
[ -d /srv/loop/repo.git ] || install -d -o runner -g runner -m 755 /srv/loop/repo.git
[ -d /srv/loop/project ]  || install -d -o runner -g runner -m 755 /srv/loop/project

if [ ! -f /srv/loop/repo.git/HEAD ]; then
  sudo -u runner git init --bare -b main /srv/loop/repo.git
fi

sudo -u runner git config --global user.name  "loop runner"
sudo -u runner git config --global user.email "runner@$(hostname)"
sudo -u runner git config --global init.defaultBranch main
# The working tree is owned by runner but contains group-writable dirs; keep
# git from treating that as a dubious-ownership problem.
sudo -u runner git config --global --add safe.directory /srv/loop/project

if [ ! -d /srv/loop/project/.git ]; then
  sudo -u runner git clone /srv/loop/repo.git /srv/loop/project
fi

sudo -u runner install -d -m 755 \
  /srv/loop/project/src \
  /srv/loop/project/tests

# Private from the moment they exist, rather than created open and tightened by
# 40-perms.sh afterwards. Depending on a second script to close the fence is how
# it came to be standing open: this file was re-run on its own when the project
# was rebuilt for a new subject, 40-perms.sh was not, and ten steps then ran
# with every acceptance criterion readable by the solver. 40-perms.sh still sets
# these, and loop.py now refuses to start when they are wrong -- the runner is
# the only one of the three that runs every time.
sudo -u runner install -d -m 700 \
  /srv/loop/project/plan \
  /srv/loop/project/.runner

# conftest.py at the root does two jobs, and pytest picks it up automatically.
#
#   1. Its mere existence puts the repository root on sys.path. Without it
#      pytest inserts tests/ instead, every import of the code under test fails
#      as a collection error, and RED_GATE's R5 correctly refuses to call that
#      red -- a confusing way to discover a missing empty file.
#   2. It adds src/ as well, which is what makes the standard src-layout work:
#      code lives in src/yourpkg/ and tests import `from yourpkg...` with no
#      `src.` prefix. src/ is one of the two directories the write fence can
#      open and close (linter rule L12), so the code has to live there anyway;
#      this only keeps the imports from being ugly about it.
#
# Written unconditionally rather than only when absent: an earlier version of
# this script created the file empty, and an empty one silently skips job 2.
sudo -u runner tee /srv/loop/project/conftest.py >/dev/null <<'PYEOF'
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
PYEOF

# Seed an initial commit so the host has something to clone.
cd /srv/loop/project
if ! sudo -u runner git rev-parse HEAD >/dev/null 2>&1; then
  sudo -u runner touch src/.gitkeep tests/.gitkeep plan/.gitkeep
  sudo -u runner tee .gitignore >/dev/null <<'EOF'
.venv/
.runner/
__pycache__/
*.pyc
EOF
  sudo -u runner git add -A
  sudo -u runner git commit -q -m "chore: initial skeleton"
  sudo -u runner git push -q origin main
fi

# ---- assertions, from the runner's point of view -----------------------
# 直下に書けなければ、直下のエントリは改名も削除もできない。bin/ と runner/ を
# 差し替える経路はこれで閉じる。渡した場所には書けることも合わせて確かめる。
fail=0
if sudo -u runner test -w /srv/loop; then
  echo "FAIL: runner should NOT be able to: test -w /srv/loop"; fail=1
fi
for d in /srv/loop/brief /srv/loop/logs /srv/loop/repo.git /srv/loop/project; do
  if ! sudo -u runner test -w "$d"; then
    echo "FAIL: runner should be able to: test -w $d"; fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "20-layout: LAYOUT BROKEN" >&2
  exit 1
fi
echo "20-layout: ok"
