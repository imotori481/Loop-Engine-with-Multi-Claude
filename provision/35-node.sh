#!/usr/bin/env bash
# Node のツールチェーン。これがある理由は、機械が確かめられるユーザー
# インターフェイスにある。
#
# tkinter は画面が無いと動かせず、ここには画面が無い。run 7 はその隙間を
# 抜けて出荷された。緑のステップが10、通ったテストが42、そして最初の1フレーム
# から全部のボタンが無効な窓。どの受け入れ条件もそれを捕まえられなかった。
# ランナーに見えたのは、計画が UI を追い出した先の純粋な関数だけだったからだ。
#
# DOM に画面は要らない。happy-dom はメモリの中に DOM を作るので、テストは
# ボタンを押し、何が変わったかを確かめられる。「プレイヤーが押せるものは
# あるか」が、人間にしか答えられない問いから、普通の赤いテストに変わる。
# このスクリプトを書く前に測った結果:
#
#     動く UI に対して 3 件通る
#     run 7 と同じ形に対して 2 件落ちる（"expected 0 to be greater than 0"）
#
# 凍結の考え方は 30-python.sh と同じ（solver はツールチェーンを読んで実行
# できるが、書けない）。ただし仕組みは同じではない。Node はランナーが握る
# パスではなく、ファイルシステム上の位置からモジュールを解決するからだ。
# その帰結は2つあり、どちらも下で扱う。
set -euo pipefail

TOOLS=/srv/loop/node
P=/srv/loop/project

# 版を固定する。範囲で書くと、数か月後の `npm install` が、誰も選ばない
# うちに関門の意味を変えてしまう。
VITEST_VERSION="4.1.11"
HAPPY_DOM_VERSION="20.13.1"

command -v node >/dev/null || { echo "35-node: node is not installed" >&2; exit 1; }
cd "$(dirname "$0")"

install -d -o root -g root -m 755 /srv/loop/bin
install -o root -g root -m 755 bin/smoke-dom /srv/loop/bin/smoke-dom
install -o root -g root -m 755 bin/smoke-page /srv/loop/bin/smoke-page

# ---- ツールチェーンはプロジェクトの外に置く ----------------------------
#
# 外に置くのは意図的だ。node_modules は git に無視させる必要がある（誰も
# コミットしないし、したら `git status` が読めなくなる）。一方
# `assert_touched()` は、未追跡のファイルを git に訊いてソルバーの書き込みを
# 見つける。git が無視するものは、その検査から見えない。実体をプロジェクトの
# 外に置けば、無視の規則を根だけに固定できる（下）。ソルバーがほかの場所に
# 作った `node_modules/` は、そのまま検査に現れる。
install -d -o runner -g runner -m 755 "$TOOLS"

sudo -u runner tee "$TOOLS/package.json" >/dev/null <<EOF
{
  "name": "loop-toolchain",
  "private": true,
  "type": "module",
  "devDependencies": {
    "vitest": "$VITEST_VERSION",
    "happy-dom": "$HAPPY_DOM_VERSION"
  }
}
EOF

# npm が動いているあいだだけ書けるようにし、終わったらすぐ凍結する。
#
# --legacy-peer-deps: vitest が任意で宣言するピア依存を自動で入れない。
# npm 10.9 はその解決中に @vitest/browser-playwright の最新版（vitest 5 向け）を
# 拾い、`Cannot read properties of null (reading 'edgesOut')` で落ちる。
# この箱で使うのは package.json に書いた2つだけなので、自動解決は要らない。
chmod -R u+w "$TOOLS"
sudo -u runner npm install --prefix "$TOOLS" --no-audit --no-fund --silent --legacy-peer-deps

# ---- 凍結 ----------------------------------------------------------------
# 誰でも読めて実行できるが、書けるのは runner だけ。詰まった solver が
# pip install で逃げられないのと同じ理由で、npm install でも逃げられない。
chown -R runner:runner "$TOOLS"
chmod -R go-w "$TOOLS"

# ---- プロジェクトから届かせる ------------------------------------------
# 複製ではなくシンボリックリンクにする。凍結する木が1つで済み、Node が探すのは
# `project/node_modules` だからだ。リンクは runner の所有で、プロジェクトの根
# にはスティッキービットが付いているので、solver は差し替えも削除もできない。
if [ ! -L "$P/node_modules" ]; then
  rm -rf "$P/node_modules"
  sudo -u runner ln -s "$TOOLS/node_modules" "$P/node_modules"
fi

# ---- 無視の規則も柵の一部 ----------------------------------------------
#
# `node_modules` ではなく `/node_modules` と書く。前者はあらゆる深さでその名前を
# 無視し、ソルバーが作った `src/node_modules` を隠してしまう。ネットワークが
# 開いている以上、それはレジストリのどのパッケージにも届く経路になる。根に
# 固定すれば、無視されるのは凍結したシンボリックリンクだけで、同じ名前の
# ほかのものは未追跡として現れる。assert_touched() はそれを許可リスト外への
# 書き込みとして報告する。
IGNORE="$P/.gitignore"
if grep -qx 'node_modules/\?' "$IGNORE" 2>/dev/null; then
  echo "35-node: refusing: $IGNORE ignores node_modules at every depth" >&2
  echo "  Change that line to /node_modules -- see the comment above." >&2
  exit 1
fi
grep -qx '/node_modules' "$IGNORE" 2>/dev/null || \
  sudo -u runner tee -a "$IGNORE" >/dev/null <<<'/node_modules'

# ---- 設定ファイル。conftest.py と同じく runner が所有する --------------
# 計画ではなく環境に属する。テストがどの DOM を使うかは、ステップが決めて
# よいことではない。`.mjs` にするのは、プロジェクトの package.json はソルバーが
# 書くもので、"type": "module" と書かれているとは限らないからだ。
#
# モードは 40-perms.sh に任せず、ここで設定する。理由は下の検査にある。
# プロジェクトの根は setgid solverw なので、runner がそこに作ったファイルは
# グループ solverw を継ぎ、runner の umask 002 で 664 になる。つまり solver が
# 書ける。柵を閉じるのを後のスクリプトに任せて、開いたまま残ったことが
# 一度ある（20-layout.sh を参照）。
sudo -u runner tee "$P/vitest.config.mjs" >/dev/null <<'EOF'
// happy-dom gives every test file a document without a display. This is the
// whole reason the Node track exists: a UI that can be clicked by a machine.
export default {
  test: {
    environment: "happy-dom",
    include: ["tests/**/*.test.{js,mjs,ts}"],
    root: ".",
  },
};
EOF
chown runner:runner "$P/vitest.config.mjs"
chmod 644 "$P/vitest.config.mjs"

# ---- ページと、それを runner が所有する理由 ----------------------------
#
# 成果物は人が開くもので、開くファイルは根に置く必要がある。そこはちょうど
# 書き込みの柵が届かない場所だ。ランナーが開け閉めできるディレクトリは src/ と
# tests/ の2つだけなので、files_write に index.html を挙げた計画は L12 で弾かれ、
# それでも書いたソルバーは assert_touched に捕まる。
#
# だから conftest.py と同じく、環境が用意する。これは回避策ではなく、run 7 が
# 出荷したものへの修正だ。起動の経路は、どの関門も覆っていない唯一の経路
# だった。テストがそこを作り話で迂回した唯一の経路だったからだ。ここでは
# それが箱の事実になる。常に存在し、常に同じ export された関数を呼ぶ。
# 残るのは `src/main.ts` で、これは柵の内側にあり、確かめられ、計画が書く。
sudo -u runner tee "$P/index.html" >/dev/null <<'EOF'
<!doctype html>
<meta charset="utf-8">
<title>loop artifact</title>
<div id="app"></div>
<script type="module">
  // The whole of the shell. Everything else is under src/, where the runner
  // can fence it and the tests can reach it.
  import { start } from "/src/main.ts";
  start(document.getElementById("app"));
</script>
EOF
chown runner:runner "$P/index.html"
chmod 644 "$P/index.html"

# ---- このスクリプトがプロジェクトに書いたものをコミットする ------------
#
# 上で書いた3つは作業ツリーの中にある。コミットしないと、ランナーは最初の
# PLAN_LOAD で「作業ツリーが dirty」と言って止まる。ソルバーの書き込みを
# 見つけるための検査に、環境の側のファイルが引っかかるからだ。
# 20-layout.sh の最初のコミットと同じく runner がコミットする。対象はこの3つ
# だけにし、台帳など他の変更は巻き込まない。変更が無ければ何もしない。
ENV_FILES=(.gitignore vitest.config.mjs index.html)
sudo -u runner git -C "$P" add -- "${ENV_FILES[@]}"
if ! sudo -u runner git -C "$P" diff --cached --quiet -- "${ENV_FILES[@]}"; then
  sudo -u runner git -C "$P" commit -q -m "chore: environment files from 35-node.sh" \
    -- "${ENV_FILES[@]}"
  sudo -u runner git -C "$P" push -q origin main
fi

# ---- 検査。solver の視点で確かめる ------------------------------------
# 誰も試していない権限モデルは、存在しないのと同じだ。
fail=0
chk_can()    { if sudo -u solver "$@" >/dev/null 2>&1; then :;       else echo "FAIL: solver should be able to: $*"; fail=1; fi; }
chk_cannot() { if sudo -u solver "$@" >/dev/null 2>&1; then echo "FAIL: solver should NOT be able to: $*"; fail=1; fi; }

chk_can    test -x "$TOOLS/node_modules/.bin/vitest"
chk_can    test -r "$TOOLS/node_modules/happy-dom/package.json"
chk_cannot test -w "$TOOLS/node_modules"
chk_cannot test -w "$TOOLS/node_modules/.bin/vitest"
chk_cannot test -w "$P/vitest.config.mjs"
chk_cannot test -w "$P/index.html"
chk_can    test -r "$P/index.html"
# リンクそのもの。solver はたどれるが、自分のものに差し替えることはできない。
chk_cannot rm -f "$P/node_modules"

"$TOOLS/node_modules/.bin/vitest" --version

# 開発サーバで開いたページが start まで届くこと。計画には開発サーバの応答を
# 確かめる条件を書かせないので（loop.py の environment_facts）、要件の「開発
# サーバで開ける」はここで確かめる。
if ! sudo -u runner /srv/loop/bin/smoke-page; then
  echo "FAIL: the dev server does not reach start in src/main.ts"; fail=1
fi

if [ "$fail" -eq 0 ]; then
  echo "35-node: ok (all assertions passed)"
else
  echo "35-node: NODE FREEZE BROKEN" >&2
fi
exit "$fail"
