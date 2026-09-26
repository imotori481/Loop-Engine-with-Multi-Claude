#!/usr/bin/env bash
# ディレクトリの配置と git リポジトリ。冪等。
#
# すべてディストロ自身の ext4（VHDX の中）に置く。Windows のパスには何も
# 置かない。/mnt/c はマウントしていない（automount は切ってある）。仮に
# マウントしても、DrvFs は 40-perms.sh が頼る所有者とモードビットを表せず、
# テストの凍結を強制できなくなる。
#
#   /srv/loop/repo.git   bare リポジトリ。ランナーが GREEN と plan apply の
#                        あとに push し、ホストが `runner` として SSH で引く。
#                        loop-project.sh で管理する箱では、今のプロジェクトの
#                        /srv/loop/projects/<名前>/repo.git を指すリンク。
#   /srv/loop/project    ループが実際に回る作業ツリー。bare の HEAD が指す
#                        ブランチをチェックアウトする
#   /srv/loop/brief      runner がステップごとのブリーフを書き、solver が読む。
#                        runner から solver への唯一の経路で、これが
#                        RUNNER_SPEC 5 を守っている。solver は plan/ を読まず、
#                        したがって tasks.json を見ることがない。
set -euo pipefail

# /srv/loop そのものは root 所有。runner の所有にすると、runner は直下の
# エントリを改名できる。中身が root 所有でも関係なく、bin/ を退けて自分の
# solver-run を置けば、sudoers はパスで許可しているので、それを solver として
# 実行できる。runner/ を退ければ関門のコードも差し替えられる。
# runner が書く場所は、下で1つずつ runner に渡す。
install -d -o root -g root -m 755 /srv/loop
# setgid: runner がここに書くブリーフはグループ solverw にならなければ
# ならない。そうでないと、solver は自分の唯一の経路を読めない。
install -d -o runner -g solverw -m 2750 /srv/loop/brief
# 走行ログの置き場。runner は /srv/loop の直下に書けないので、ここに書く。
# 走行ログには失敗したテストの中身が載り、now.json にはステップの goal が載る。
# 読めるのは runner と、humanw にいる保守ユーザーだけにする。setgid で、runner が
# 作ったファイルもグループ humanw を継ぐ。
install -d -o runner -g humanw -m 2750 /srv/loop/logs
chmod -R o-rwx /srv/loop/logs

# repo.git と project は runner が中身を作る。直下に作る権限は runner に無いので、
# 空のディレクトリを root が runner 所有で先に用意する。git init --bare も
# git clone も、空の既存ディレクトリをそのまま使える。
# 無いときだけ作る。project は 40-perms.sh が 3770 に締めるので、ここで毎回
# 755 に戻すと、このスクリプトを単独で流したときに誰からでも読める状態になる。
[ -d /srv/loop/repo.git ] || install -d -o runner -g runner -m 755 /srv/loop/repo.git
[ -d /srv/loop/project ]  || install -d -o runner -g runner -m 755 /srv/loop/project

if [ ! -f /srv/loop/repo.git/HEAD ]; then
  sudo -u runner git init --bare -b main /srv/loop/repo.git
fi

sudo -u runner git config --global user.name  "loop runner"
sudo -u runner git config --global user.email "runner@$(hostname)"
sudo -u runner git config --global init.defaultBranch main
# 作業ツリーは runner の所有だが、グループで書けるディレクトリを含む。
# git がそれを所有者の疑わしいリポジトリとして扱わないようにする。
sudo -u runner git config --global --add safe.directory /srv/loop/project

if [ ! -d /srv/loop/project/.git ]; then
  sudo -u runner git clone /srv/loop/repo.git /srv/loop/project
fi

sudo -u runner install -d -m 755 \
  /srv/loop/project/src \
  /srv/loop/project/tests

# 開いた状態で作って 40-perms.sh が後から締めるのではなく、できた瞬間から
# 非公開にする。柵を閉じるのを別のスクリプトに任せると、開いたまま残る。
# 実際、新しい題材のためにプロジェクトを作り直したとき、このファイルだけを
# 流して 40-perms.sh を流さず、10ステップのあいだ受け入れ条件が全部 solver から
# 読める状態で走った。40-perms.sh もこれらを設定し、権限が違えば loop.py は
# 起動を拒む。3つのうち毎回必ず走るのはランナーだけだからだ。
sudo -u runner install -d -m 700 \
  /srv/loop/project/plan \
  /srv/loop/project/.runner

# 根の conftest.py は2つの仕事をし、pytest が自動で読み込む。
#
#   1. 存在するだけで、リポジトリの根が sys.path に載る。無いと pytest は
#      代わりに tests/ を載せ、テスト対象のコードの import がすべて収集エラーに
#      なる。RED_GATE の R5 はそれを正しく赤と認めない。空のファイルが1つ
#      足りないことを知るには、分かりにくい経路だ。
#   2. src/ も載せる。これで標準的な src レイアウトが使える。コードは
#      src/yourpkg/ に置き、テストは `src.` を付けずに `from yourpkg...` と
#      import する。src/ は書き込みの柵が開け閉めできる2つのディレクトリの
#      1つ（リンタ規則 L12）なので、コードはどのみちそこに置く。これは
#      import の見た目を崩さないためだけのもの。
#
# 無いときだけでなく毎回書く。空のファイルだと、2つ目の仕事が黙って
# 抜け落ちるからだ。
#
# 取り込んだリポジトリが自分の conftest.py を持っていれば、上書きせずに止まる。
# 上書きすると、そのリポジトリのテストの前提を黙って壊す。runner 以外がコミット
# したことのあるファイルは、そのリポジトリの持ち物だ。中身では比べない。比べると、
# ここの雛形を直しただけで止まる。
cd /srv/loop/project
if sudo -u runner git log --format=%an -- conftest.py 2>/dev/null | grep -qvx 'loop runner'; then
  echo "20-layout: リポジトリが自分の conftest.py を持っている。上書きしない" >&2
  exit 1
fi
sudo -u runner tee conftest.py >/dev/null <<'PYEOF'
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
PYEOF

# 作業ツリーに置くが、コミットしないもの。無いと `git status` がこれらを紛れ込んだ
# ファイルとして報告し、最初の走行が dirty で止まる。
IGNORED=(.venv/ .runner/ __pycache__/ '*.pyc')

if ! sudo -u runner git rev-parse HEAD >/dev/null 2>&1; then
  # 空のリポジトリ。ホストがクローンできるよう、最初のコミットを置く。
  sudo -u runner touch src/.gitkeep tests/.gitkeep plan/.gitkeep
  printf '%s\n' "${IGNORED[@]}" | sudo -u runner tee .gitignore >/dev/null
  sudo -u runner git add -A
  sudo -u runner git commit -q -m "chore: initial skeleton"
  sudo -u runner git push -q origin HEAD
else
  # 取り込んだブランチ。既存の .gitignore には足りない行だけを足す。コミットは
  # 環境が置いた2つに絞り、ほかの変更は巻き込まない。変更が無ければ何もしない。
  # 末尾に改行の無い .gitignore では、足した行が最後の行につながってしまう。
  if [ -s .gitignore ] && [ -n "$(tail -c1 .gitignore)" ]; then
    echo | sudo -u runner tee -a .gitignore >/dev/null
  fi
  for line in "${IGNORED[@]}"; do
    grep -qxF "$line" .gitignore 2>/dev/null \
      || printf '%s\n' "$line" | sudo -u runner tee -a .gitignore >/dev/null
  done
  sudo -u runner git add -- .gitignore conftest.py
  if ! sudo -u runner git diff --cached --quiet -- .gitignore conftest.py; then
    sudo -u runner git commit -q -m "chore: environment files from 20-layout.sh" \
      -- .gitignore conftest.py
    sudo -u runner git push -q origin HEAD
  fi
fi

# ---- 検査。runner の視点で確かめる ------------------------------------
# 直下に書けなければ、直下のエントリは改名も削除もできない。bin/ と runner/ を
# 差し替える経路はこれで閉じる。渡した場所には書けることも合わせて確かめる。
fail=0
if sudo -u runner test -w /srv/loop; then
  echo "FAIL: runner should NOT be able to: test -w /srv/loop"; fail=1
fi
for d in /srv/loop/brief /srv/loop/logs /srv/loop/repo.git /srv/loop/project; do
  if ! sudo -u runner test -w "$d"; then
    echo "FAIL: runner should be able to: test -w $d"; fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "20-layout: LAYOUT BROKEN" >&2
  exit 1
fi
echo "20-layout: ok"
