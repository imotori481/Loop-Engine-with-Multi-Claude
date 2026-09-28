#!/usr/bin/env bash
# solver アカウントの外向き通信の制限（RUNNER_SPEC 1-3）。
#
# provision.sh からは流さない。制限するかどうかは provision/README §4 を読んで
# 決める。既定のソルバーは Claude の API に届く必要がある。
#
#   usage: sudo ./60-egress.sh api.anthropic.com [host ...]
#
# 効果: uid `solver` は、ループバック、DNS、挙げたホストにだけ届く。それ以外の
# 外向きの接続はすべて拒否されるので、`pip install` や `apt` は環境を黙って
# 変えるのではなく、はっきり失敗する。
#
# 制約: ホストのいまの IP アドレスを固定する。CDN の後ろにある API は
# アドレスが入れ替わり、規則は予告なく古くなる。それが困るようになったら、
# `runner` で動く、許可リスト付きの転送プロキシ（tinyproxy など）に置き換え、
# ここの規則は「solver は localhost のプロキシのポートにだけ届く」に絞る。
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "sudo で流す" >&2; exit 1; }
[ "$#" -ge 1 ] || { echo "使い方: $0 <ホスト> [ホスト ...]" >&2; exit 1; }

# WSL の Ubuntu イメージには iptables も nft も無い。30-python.sh ではなくここで
# 入れるのは、この任意の手順だけが要るパッケージを、基本のプロビジョニングに
# 持ち込まないためだ。
if ! command -v iptables >/dev/null 2>&1; then
  echo "iptables が無いので入れる（iptables-nft 版）"
  DEBIAN_FRONTEND=noninteractive apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iptables
fi

UID_SOLVER="$(id -u solver)"
CHAIN=LOOP_SOLVER_OUT

iptables -N "$CHAIN" 2>/dev/null || iptables -F "$CHAIN"
# チェインはちょうど1回だけつなぐ。
iptables -C OUTPUT -m owner --uid-owner "$UID_SOLVER" -j "$CHAIN" 2>/dev/null \
  || iptables -A OUTPUT -m owner --uid-owner "$UID_SOLVER" -j "$CHAIN"

iptables -A "$CHAIN" -o lo -j ACCEPT
# DNS は宛先を問わずあえて許す。WSL2 ではリゾルバは /etc/resolv.conf に書かれた
# NAT のゲートウェイで、そのファイルは VM が起動するたびに WSL が作り直す。
# ここでアドレスを固定すると再起動で壊れる規則になり、再起動は頻繁に起きる
# （README 3-1）。このチェインが買うのは再現性で、持ち出しへの耐性ではない。
# だからポート 53 が緩くても構わない。
iptables -A "$CHAIN" -p udp --dport 53 -j ACCEPT
iptables -A "$CHAIN" -p tcp --dport 53 -j ACCEPT

for host in "$@"; do
  ips="$(getent ahostsv4 "$host" | awk '{print $1}' | sort -u)"
  [ -n "$ips" ] || { echo "FATAL: $host の名前を引けない" >&2; exit 1; }
  for ip in $ips; do
    echo "  許可: $host -> $ip"
    iptables -A "$CHAIN" -d "$ip" -p tcp --dport 443 -j ACCEPT
  done
done

# DROP ではなく REJECT にする。solver はタイムアウトまで待たされずにすぐエラーを
# 受け取り、失敗がその出力に現れる。
iptables -A "$CHAIN" -j REJECT --reject-with icmp-admin-prohibited

echo
echo "確かめる（どちらも書いたとおりに振る舞うこと）:"
sudo -u solver curl -s -m 8 -o /dev/null -w '  pypi.org  -> %{http_code}（000 か失敗が正しい）\n' https://pypi.org/simple/ || echo "  pypi.org  -> 遮断された（正しい）"
for host in "$@"; do
  sudo -u solver curl -s -m 8 -o /dev/null -w "  $host -> %{http_code}（000 以外が正しい）\n" "https://$host/" || echo "  $host -> 届かない（規則が狭すぎる）"
done

# 普通のサーバで「永続しない」は「次の再起動まで」を意味する。WSL2 では
# 「VM がアイドルで止まるまで」を意味し、それは最後の wsl.exe のセッションが
# 消えてからおよそ1分後だ。だから、ここでは永続化が実質必須になる。
echo
cat <<'EOF'
この規則は永続しない。WSL2 の VM はアイドルになるたびに止まる。
止まっても残るようにする:
  apt-get install -y iptables-persistent && netfilter-persistent save
そのあと、再起動しても残ることを確かめる。`wsl --shutdown` は使わない。
keepalive タスクまで止まる（README 3-2）:
  wsl --terminate Ubuntu-24.04      # Windows で流し、つなぎ直す
  sudo iptables -S LOOP_SOLVER_OUT  # 規則が残っていること
EOF
