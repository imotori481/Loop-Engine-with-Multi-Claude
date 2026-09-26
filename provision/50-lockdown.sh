#!/usr/bin/env bash
# SSH のアクセス制御と、更新の凍結。最後に流す。`runner` にログインを許すのは
# このスクリプトなので、15-authkeys.sh が先に成功している必要がある。また
# パスワード認証を外すので、順番を間違えると締め出される。
set -euo pipefail

ADMIN_USER="${ADMIN_USER:-maint}"

# runner の鍵が置かれていなければ、sshd に触らない。この防ぎが無いと、
# 15-authkeys.sh が失敗したとき、ログインは許されているのに認証する手段が無い
# アカウントができる。
if [ ! -s /home/runner/.ssh/authorized_keys ]; then
  echo "FATAL: /home/runner/.ssh/authorized_keys is missing or empty." >&2
  echo "       Refusing to change the SSH configuration." >&2
  exit 1
fi

# ファイル名は元の drop-in（10-loop-dev.conf）より前に並ぶようにする。sshd は
# 単一値のキーワードについて最初に見た値を採るので、99- の drop-in は後から
# 読まれ、黙って負ける。
#
# AllowUsers は例外だ。リスト値で、drop-in をまたいで累積する。10-loop-dev.conf の
# `AllowUsers maint` とここの `AllowUsers maint runner` は、順番に関係なく
# {maint, runner} に合算される。覚えておくべき帰結は逆向きのほうで、後の
# drop-in は許可を広げることしかできず、狭めることはできない。ユーザーを外すには、
# その名前を書いているファイルを直す。
rm -f /etc/ssh/sshd_config.d/99-loop.conf
cat > /etc/ssh/sshd_config.d/00-loop.conf <<EOF
# ログインできるのはこの2人だけ。エージェントのアカウントはあえて載せず、二重に
# 拒否する。エージェントはランナーが Runas の例外で起動するもので、ログインでは
# 起動しない。だから、そのどれかとして入ってくる SSH のセッションは、別の誰か
# でしかありえない。
AllowUsers $ADMIN_USER runner
DenyUsers solver planner
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF
chmod 644 /etc/ssh/sshd_config.d/00-loop.conf

sshd -t
systemctl restart ssh

# 設定を書くことと、設定が効くことは同じではない。sshd が実際に何と解釈したかを
# 訊き、どれかが効いていなければはっきり失敗する。
# 検査ごとに sshd を grep や awk にパイプせず、一度だけ変数に取る。
# `set -o pipefail` の下では、`sshd -T | grep -q ...` は一致しても失敗を返す。
# grep -q が最初の一致で終わり、sshd が SIGPIPE で死に、pipefail がそれを
# 非ゼロのパイプラインにする。設定は正しいのに、検査が「失敗」する。
sshd_out="$(sshd -T 2>/dev/null || true)"
eff() { printf '%s\n' "$sshd_out" | awk -v k="$1" '$1==k{v=$2} END{print v}'; }
# AllowUsers と DenyUsers はリスト値だ。最初のものが勝つのではなく drop-in を
# またいで累積し、`sshd -T` は名前ごとに1行を出す。最後の行（や最初の行）だけを
# 取ると誤った答えになるので、名前を全部集める。
eff_list() { printf '%s\n' "$sshd_out" | awk -v k="$1" '$1==k{for(i=2;i<=NF;i++) printf "%s ", $i}'; }
fail=0
for kv in passwordauthentication=no kbdinteractiveauthentication=no permitrootlogin=no; do
  k="${kv%%=*}"; want="${kv#*=}"; got="$(eff "$k")"
  if [ "$got" != "$want" ]; then echo "FAIL: sshd $k=$got (want $want)" >&2; fail=1; fi
done

# 許可リストの両側を確かめる。runner は入っていて、solver は入っていないこと。
allow=" $(eff_list allowusers) "
case "${allow//,/ }" in
  *" runner "*) ;;
  *) echo "FAIL: sshd allowusers=[${allow# }] does not include runner" >&2; fail=1 ;;
esac
for u in solver planner; do
  case "${allow//,/ }" in
    *" $u "*) echo "FAIL: sshd allowusers=[${allow# }] includes $u" >&2; fail=1 ;;
  esac
done
deny=" $(eff_list denyusers) "
for u in solver planner; do
  case "${deny//,/ }" in
    *" $u "*) ;;
    *) echo "FAIL: sshd does not deny user $u" >&2; fail=1 ;;
  esac
done

# sshd は 2222 のままでなければならない。Windows 側は localhostForwarding で
# 127.0.0.1:2222 として届き、~/.ssh/config の `loop-dev` はそのポートを
# 決め打ちしている。
port="$(eff port)"
if [ "$port" != "2222" ]; then
  echo "FAIL: sshd port=$port (want 2222)" >&2; fail=1
fi

[ "$fail" -eq 0 ] || { echo "50-lockdown: SSH CONFIG DID NOT TAKE EFFECT" >&2; exit 1; }

# 自動更新は、凍結した pip freeze のハッシュの下から環境を動かし、緑のステップを
# 数週間後に再現できなくする。更新は意図して行う。ずれに任せず、このディストロの
# プロビジョニングを流し直す。
systemctl disable --now unattended-upgrades.service 2>/dev/null || true
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true

echo "50-lockdown: ok"
