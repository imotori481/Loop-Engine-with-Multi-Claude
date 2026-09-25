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
[ -f "$PUB" ] || { echo "FATAL: $PUB not found" >&2; exit 1; }

install -d -o runner -g runner -m 700 /home/runner/.ssh
install -o runner -g runner -m 600 /dev/null /home/runner/.ssh/authorized_keys
cat "$PUB" > /home/runner/.ssh/authorized_keys
chown runner:runner /home/runner/.ssh/authorized_keys

# solver には鍵も .ssh ディレクトリも与えない。
rm -rf /home/solver/.ssh

echo "15-authkeys: ok"
