#!/usr/bin/env bash
# ランナー本体を /srv/loop/runner/loop.py に置く。何度走らせても同じ結果になる。
#
#     sudo -u runner python3 /srv/loop/runner/loop.py <verb>
#
# root 所有、runner からは読めて実行できるが書けない。ランナーは関門を強制する
# 側で、その関門はこのファイルに書いてある。runner が自分のコードを書き換え
# られたら、RED_GATE も FREEZE も VERIFY も runner の気分次第になる。
# 更新はリポジトリを pull して、このスクリプトを流し直すことで行う。
set -euo pipefail
cd "$(dirname "$0")"

SRC=../runner/loop.py
DEST=/srv/loop/runner

[ -f "$SRC" ] || { echo "25-runner: $SRC not found (run from a full clone)" >&2; exit 1; }

install -d -o root -g root -m 755 "$DEST"
install -o root -g root -m 644 "$SRC" "$DEST/loop.py"

# 保守ユーザーが打つ `loop` コマンド。PATH の通った場所に置く。
install -o root -g root -m 755 bin/loop /usr/local/bin/loop

# ---- 検査。runner の視点で確かめる ------------------------------------
fail=0
if ! sudo -u runner test -r "$DEST/loop.py"; then
  echo "FAIL: runner should be able to: test -r $DEST/loop.py"; fail=1
fi
if sudo -u runner test -w "$DEST/loop.py"; then
  echo "FAIL: runner should NOT be able to: test -w $DEST/loop.py"; fail=1
fi
if sudo -u runner test -w "$DEST"; then
  echo "FAIL: runner should NOT be able to: test -w $DEST"; fail=1
fi
# 走行は runner として /usr/local/bin/loop を実行する。runner が書き換えられれば、
# 関門の外で好きなコマンドを走らせられる。
if sudo -u runner test -w /usr/local/bin/loop; then
  echo "FAIL: runner should NOT be able to: test -w /usr/local/bin/loop"; fail=1
fi
if ! bash -n /usr/local/bin/loop; then
  echo "FAIL: /usr/local/bin/loop does not parse"; fail=1
fi
# 構文が壊れたファイルを置いたまま ok と言わない。py_compile ではなく ast で
# 確かめる。py_compile は __pycache__ を書こうとし、$DEST は runner から書けない。
if ! sudo -u runner python3 -c "import ast,sys; ast.parse(open(sys.argv[1], encoding='utf-8').read())" "$DEST/loop.py"; then
  echo "FAIL: $DEST/loop.py does not parse"; fail=1
fi

if [ "$fail" -eq 0 ]; then
  echo "25-runner: ok"
else
  echo "25-runner: RUNNER INSTALL BROKEN" >&2
fi
exit "$fail"
