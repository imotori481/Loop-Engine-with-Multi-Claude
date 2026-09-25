#!/usr/bin/env bash
# Python のツールチェーン。venv は runner が作って所有する。solver は読んで
# 実行できるが、書くことはできない。だから詰まった solver が pip install で
# 問題から逃げることはできない（RUNNER_SPEC 1-3: 環境の凍結）。
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
# ここで窓を開くものは無いが、python3-tk は省けない。Debian では tkinter が
# 別のパッケージなので、無いと `import tkinter` が ModuleNotFoundError になる。
# tkinter を import するモジュールを import したテストは、アサーションより前、
# import の時点で落ちる。ソルバーは頼まれたものと無関係な失敗を見せられ、
# 試行を全部そこで使う。venv はこれを追加の手間なく拾う。tkinter は
# site-packages ではなく標準ライブラリにあるからだ。
# それでもここに DISPLAY は無い（WSLg は sshd のセッションに届かない）。
# ランナーが確かめられるのは GUI プログラムのロジックだけで、GUI そのものは
# 確かめられない。そのためにホスト側の host/dashboard のレビューがある。
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
