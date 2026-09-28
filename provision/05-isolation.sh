#!/usr/bin/env bash
# WSL 固有の隔離の検査。
#
# VirtualBox のゲストは既定でホストから隔離されている。WSL2 のディストロは
# 違う。何もしなければ Windows のドライブを全部 /mnt の下にマウントし、Windows
# の実行ファイルを起動でき、Windows 側と画面・音声・クリップボードの経路で
# つながる。隔離はすべて /etc/wsl.conf と .wslconfig から来ていて、書いたが
# 反映されていない設定は、効いている設定とまったく同じに見える。
#
# `wsl --terminate` ではこれらのファイルは読み直されない。読み直すのは
# `wsl --shutdown` だけで、それは keepalive タスクを殺す（README 3-2）。だから
# 「wsl.conf を書いた」は証拠にならない。証拠になるのは下の検査だ。
#
# 抜け道: ALLOW_WSLG=1 を付けると、WSLg の経路を分かったうえでの例外として
# 受け入れる。ほかの検査は免除できない。
set -euo pipefail

fail=0
bad()  { echo "FAIL: $*" >&2; fail=1; }
info() { echo "  note: $*"; }

# WSL の上でだけ意味がある。別のハイパーバイザではファイルの配置が違い、
# これらの検査は誤りというより誤解を招くものになる。
if ! grep -qi microsoft /proc/sys/kernel/osrelease; then
  echo "05-isolation: WSL の上ではないので飛ばす"
  exit 0
fi

# 1. Windows のファイルシステムに届かないこと。これが要の検査だ。Windows の
#    パスが1つもマウントされていなければ、solver が読める Windows のファイルも、
#    起動できる .exe も無い。wsl.conf ではなく、いまのマウント表で確かめる。
#
#    /usr/lib/wsl/* は除く。WSL はそこに GPU ドライバの置き場を必ず読み取り専用
#    （fmask/dmask 222）でマウントし、切ることはできない。
mounts="$(awk '$3=="drvfs" || ($3=="9p" && $2 !~ /^\/usr\/lib\/wsl\//) {print "  "$2" ("$3")"}' \
          /proc/self/mounts || true)"
if [ -n "$mounts" ]; then
  bad "Windows のファイルシステムがマウントされている:"
  printf '%s\n' "$mounts" >&2
fi

# automount を切っても、/mnt/c はたいてい空のディレクトリとして残る。空なら
# 問題ない。中身があれば、いま何かがそこにマウントされている。
if [ -d /mnt/c ] && [ -n "$(ls -A /mnt/c 2>/dev/null || true)" ]; then
  bad "/mnt/c に中身がある。automount が有効か、誰かが手でマウントした"
fi

case ":${PATH}:" in
  *:/mnt/c/*) bad "PATH に Windows のパスが残っている（appendWindowsPath）" ;;
esac

# 2. interop。これが確かめないものに注意する。WSLInterop の binfmt ハンドラは
#    `[interop] enabled=false` でも登録されたまま残るので、あっても何の証明にも
#    ならない。interop を切ると実際には /init が Windows 側に届かず、exec が
#        WSL ERROR: UtilAcceptVsock:273: accept4 failed 110
#    で失敗する。2026-08-17 に root で C: をマウントし、cmd.exe を実行して手で
#    確かめた。実行して確かめるには Windows のパスが要り、それは検査1がすでに
#    禁じている。だから interop は報告するだけで、合否には使わない。
if [ -e /proc/sys/fs/binfmt_misc/WSLInterop ] || [ -e /proc/sys/fs/binfmt_misc/WSLInterop-late ]; then
  info "WSLInterop の binfmt ハンドラが登録されている（interop=false でも残るので、"
  info "      どちらの証拠にもならない。.exe に届かないことは検査1が確かめる）"
fi

# 3. WSLg。見落としやすい。有効だと /mnt/wslg に、WINDOWS 側で動くコンポジタと
#    PulseAudio サーバへの、誰でも使えるソケットが置かれる。ホストへの生きた
#    経路（画面、音声、クリップボード）が、solver を含むディストロの全員に
#    開いている。wsl.conf にはこれを切る設定が無く、Windows 側の .wslconfig に
#    `guiApplications=false` を書く必要がある。
#
#    ディレクトリではなく経路そのものを探す。WSLg を切っても /mnt/wslg は空の
#    骨組み（run/user/<uid> だけ）として残るので、有無で判定すると誤検知する。
wslg_sockets="$(find /mnt/wslg /tmp/.X11-unix -type s 2>/dev/null | head -5 || true)"
wslg_mounts="$(awk '$2 ~ /^\/mnt\/wslg/ {print "  "$2" ("$3")"}' /proc/self/mounts || true)"
if [ -n "$wslg_sockets$wslg_mounts" ]; then
  if [ "${ALLOW_WSLG:-0}" = "1" ]; then
    info "WSLg が有効だが、ALLOW_WSLG=1 で例外として受け入れた"
  else
    bad "WSLg が有効。Windows への画面・音声・クリップボードの経路が、solver を含む"
    echo "      この箱の全員に開いている。見つかったもの:" >&2
    if [ -n "$wslg_sockets" ]; then printf '  socket: %s\n' $wslg_sockets >&2; fi
    if [ -n "$wslg_mounts" ]; then printf '%s\n' "$wslg_mounts" >&2; fi
    echo "      C:\\Users\\<you>\\.wslconfig で閉じる:" >&2
    echo "        [wsl2]" >&2
    echo "        guiApplications=false" >&2
    echo "      そのあと wsl --shutdown し、keepalive タスクを起こし直す（README 3-2）。" >&2
    echo "      分かったうえで受け入れるなら、ALLOW_WSLG=1 を付けて流し直す。" >&2
  fi
fi

# 4. systemd が動いていること。無いと sshd は管理されず、50-lockdown.sh が
#    `systemctl restart ssh` と `sshd -T` で行う検証が意味を失う。
state="$(systemctl is-system-running 2>/dev/null || true)"
case "$state" in
  running|degraded) ;;
  *) bad "systemd が動いていない（状態: ${state:-none}）。wsl.conf に [boot] systemd=true を書く" ;;
esac

# 5. mirrored ではなく NAT であること。mirrored だとディストロから Windows
#    ホストの localhost で listen しているサービスに届き、サンドボックスが
#    黙って広がる。loopback0 は mirrored のときにだけ現れるインターフェイスだ。
# （`grep -q` にパイプせず、変数に取ってから見る。pipefail の下では grep -q が
#  最初の一致で終わり、ip が SIGPIPE で死に、正しいのに検査が「失敗」する。
#  README 3-10）
links="$(ip -o link show 2>/dev/null || true)"
case "$links" in
  *" loopback0:"*) bad "ネットワークが mirrored になっている（loopback0 がある）。networkingMode=NAT にする" ;;
esac

if [ "$fail" -eq 0 ]; then
  echo "05-isolation: ok（すべての検査に通った）"
else
  echo "05-isolation: WSL の隔離が効いていない" >&2
fi
exit "$fail"
