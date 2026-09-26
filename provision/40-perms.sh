#!/usr/bin/env bash
# 権限モデル。RUNNER_SPEC 1-3（テストの凍結）と files_write の許可リストを
# 強制する主な仕組みで、ランナーのほかの検査はすべてこの上に重ねた
# トリップワイヤだ。
#
# 最後に solver の視点でモデルを確かめる。誰も試していない権限モデルは、
# 存在しないのと同じだ。
set -euo pipefail
P=/srv/loop/project
ADMIN_USER="${ADMIN_USER:-maint}"

chown -R runner:runner "$P"

# 作業ツリーの根はグループで書ける。すぐには思いつかない選択だ。Codex の
# apply_patch はワークスペースの根を経由して書くので、根が読み取り専用だと
# 編集がすべて失敗する。しかもエラーは対象のファイル名で出て、本当の原因から
# 遠い。
#
# 書けるだけでは穴になる。unlink と rename はファイルではなくディレクトリの
# 書き込みビットで決まるので、conftest.py を変更できない solver でも、それを
# 消して自分のものを置ける。テストの収集はまさに FREEZE が守るもので、それを
# 黙って無効にしてしまう。
#
# スティッキービットがそれを塞ぐ。付いていれば、ファイルを消したり改名したり
# できるのは所有者だけになる。solver は根に新しいファイルを作れる（codex に
# 必要）が、runner の持ち物には触れない。このスクリプトの末尾で確かめる。
chgrp solverw "$P"
# 3775 ではなく 3770。末尾の 5 は、箱のすべてのアカウントにワークスペースの
# 読み取りと通過を許し、そこから src/ と tests/ が誰にでも読めた。BOOTSTRAP 1-1
# が基準を満たすコードから遠ざけている planner にも、テストを読んでいないことが
# 価値のすべてである critic にもだ。ここで `other` を要するものは無い。solver は
# solverw 経由で届き、runner は所有者だ。critic 用の柵の検査が、初回で見つけた。
chmod 3770 "$P"      # setgid + sticky + rwxrwx---

# setgid ビットには、見落としやすく、放置すると高くつく2つ目の効果がある。
# RUNNER が根に作ったファイルもグループ solverw を継ぐ。runner の umask は 002
# なので 664 になり、solver が書ける。
#
# 根にあるファイルは作業物ではなく、柵そのものだ。conftest.py は pytest が何を
# import できるかを決める。vitest.config.mjs は DOM のテストが何に対して走るかを
# 決める。.gitignore は `git status` が何を紛れ込んだファイルとして報告するか、
# つまり assert_touched() の全部を決める。どれか1つでも書き換えられる solver は、
# FREEZE がハッシュを取るファイルに1つも触れずに FREEZE を無効にできる。
#
# conftest.py がいまグループ runner なのは、たまたま 20-layout.sh が根を setgid
# にする前に走るからにすぎない。後からそのスクリプトを流し直せば入れ替わる。
# 10ステップのあいだ plan/ を読める状態にしたのと同じ事故だ。だから、頼らずに
# ここで規則として書く。
find "$P" -maxdepth 1 -type f -user runner -exec chgrp runner {} + -exec chmod 644 {} +

# runner だけのもの: git の履歴、計画、凍結のマニフェスト。solver は tasks.json
# を読めてはならず（RUNNER_SPEC 5）、どのファイルのハッシュを取っているかも
# 知ってはならない。
chmod 700 "$P/.git" "$P/plan" "$P/.runner"

# solver が書ける場所。setgid にして、solver が作ったファイルがグループ solverw を
# 保ち、runner が扱えるようにする。
chown -R runner:solverw "$P/src" "$P/tests"
chmod 2770 "$P/src" "$P/tests"

# インタプリタとライブラリ。読めて実行できるが、書けない。
chmod -R go-w "$P/.venv"

chown runner:solverw /srv/loop/brief
chmod 2750 /srv/loop/brief   # setgid: ブリーフはグループ solverw にならなければならない

# ---- 検査。solver の視点で確かめる ------------------------------------
fail=0
chk_can()    { if sudo -u solver "$@" >/dev/null 2>&1; then :;       else echo "FAIL: solver should be able to: $*"; fail=1; fi; }
chk_cannot() { if sudo -u solver "$@" >/dev/null 2>&1; then echo "FAIL: solver should NOT be able to: $*"; fail=1; fi; }

# ワークスペースの根: solver は足せるが、runner の持ち物は消せない。
# conftest.py や pytest.ini の代わりに、使い捨ての runner 所有ファイルを置く。
sudo -u runner touch "$P/.perm-probe-runner"
chmod 644 "$P/.perm-probe-runner"

chk_can    touch "$P/.perm-probe-solver"                       # codex apply_patch
chk_cannot rm -f "$P/.perm-probe-runner"                       # スティッキービット
chk_cannot mv "$P/.perm-probe-runner" "$P/.perm-probe-moved"   # スティッキービット
rm -f "$P/.perm-probe-runner" "$P/.perm-probe-solver" "$P/.perm-probe-moved"

chk_can    test -w "$P/src"
chk_can    test -w "$P/tests"
chk_can    test -x "$P/.venv/bin/python"
chk_can    test -r /srv/loop/brief

chk_cannot test -w "$P/.venv/bin/python"
# 根にある柵のファイル。どれかを書き換えれば、凍結が見張るファイルの外から
# 凍結を無効にできる。
chk_cannot test -w "$P/conftest.py"
chk_cannot test -w "$P/.gitignore"
[ -e "$P/vitest.config.mjs" ] && chk_cannot test -w "$P/vitest.config.mjs"
chk_cannot ls "$P/.git"
chk_cannot ls "$P/plan"
chk_cannot ls "$P/.runner"
chk_cannot test -w /srv/loop/brief
chk_cannot ls /home/runner

# solverw にいないアカウントは、コードにまったく届いてはならない。planner は
# 基準を書くので、それを満たすコードを見てはならない（BOOTSTRAP 1-1）。critic は、
# 見れば判定の拠り所にしてしまうテストを見てはならない。
for stranger in planner critic; do
  id -u "$stranger" >/dev/null 2>&1 || continue
  for target in "$P" "$P/src" "$P/tests"; do
    if sudo -u "$stranger" ls "$target" >/dev/null 2>&1; then
      echo "FAIL: $stranger should NOT be able to: ls $target"
      fail=1
    fi
  done
done
# 保守ユーザーのホームには、人間の ssh 鍵とエージェント CLI の資格情報がある。
# solver が届けば、git の経路とログインを渡すことになる。
chk_cannot ls "/home/$ADMIN_USER"

if [ "$fail" -eq 0 ]; then
  echo "40-perms: ok (all assertions passed)"
else
  echo "40-perms: PERMISSION MODEL BROKEN" >&2
fi
exit "$fail"
