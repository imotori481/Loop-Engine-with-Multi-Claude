#!/usr/bin/env bash
# Python toolchain. The venv is created and owned by runner; solver may read
# and execute it but never write to it, so a stuck solver cannot pip-install
# its way out of a problem (RUNNER_SPEC 1-3: environment freeze).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
# python3-tk is not optional even though nothing here opens a window. On Debian
# tkinter is a separate package, so `import tkinter` raises ModuleNotFoundError
# -- and a test that imports a module which imports tkinter fails at import
# time, before any assertion runs. The solver would see a failure with no
# relation to what it was asked to build and spend every attempt on it. The
# venv picks this up for free: tkinter lives in the stdlib, not site-packages.
# There is still no DISPLAY here (WSLg does not reach an sshd session), so the
# runner can check the logic of a GUI program but never the GUI itself. That is
# what the host-side review in host/dashboard is for.
apt-get install -y -qq python3-venv python3-pip python3-tk git

V=/srv/loop/project/.venv
# python と pip の両方があって初めて作成済みとみなす。python3-venv が無い
# 状態で venv を作ると、python だけ置いて失敗し、pip の無い venv が残る。
# python の有無だけで判定すると、次の実行はそれを作成済みとみなし、下の
# pip で止まる。片方しか無ければ、壊れているので消して作り直す。
if [ ! -x "$V/bin/python" ] || [ ! -x "$V/bin/pip" ]; then
  rm -rf "$V"
  sudo -u runner python3 -m venv "$V"
fi
sudo -u runner "$V/bin/pip" install -q --upgrade pip
sudo -u runner "$V/bin/pip" install -q pytest

"$V/bin/python" --version
"$V/bin/pytest" --version

echo "30-python: ok"
