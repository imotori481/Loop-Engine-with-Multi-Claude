#!/usr/bin/env bash
# ホストの公開鍵を `runner` に入れる。50-lockdown.sh より前に流すこと。
# `runner` にログインを許すのは 50-lockdown.sh で（元の sshd の drop-in は
# 保守ユーザーしか許さない）、念のためパスワード認証も切る。
#
# この鍵は保守ユーザーの鍵とは別のもの。ホストが /srv/loop/repo.git を
# 引くためだけにある。
#
# 公開鍵は /tmp/loop-provision/loop-runner_ed25519.pub に置いておく
# （このディストロには /mnt/c が無いので、wsl の標準入力で流し込む。README 2-7）
set -euo pipefail

PUB=/tmp/loop-provision/loop-runner_ed25519.pub
KEYS=/home/runner/.ssh/authorized_keys

# /tmp は VM の再起動で消える。2回目以降のプロビジョニングで流し込み直しを
# 求めないよう、流し込んだ鍵が無くても、すでに入っている鍵があればそれを残す。
# 新しい鍵を流し込んだときだけ置き換える。
if [ -f "$PUB" ]; then
  install -d -o runner -g runner -m 700 /home/runner/.ssh
  install -o runner -g runner -m 600 /dev/null "$KEYS"
  cat "$PUB" > "$KEYS"
  chown runner:runner "$KEYS"
  note="installed from $PUB"
elif [ -s "$KEYS" ]; then
  note="no new key at $PUB; kept the one already installed"
else
  echo "FATAL: $PUB が無く、runner にもまだ鍵が無い（provision/README §2-7 で流し込む）" >&2
  exit 1
fi

# solver には鍵も .ssh ディレクトリも与えない。
rm -rf /home/solver/.ssh

echo "15-authkeys: ok ($note)"
