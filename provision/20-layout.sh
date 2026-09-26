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
#   /srv/loop/project    ループが実際に回る作業ツリー
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
install -d -o runner -g runner -m 755 /srv/loop/logs

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
sudo -u runner tee /srv/loop/project/conftest.py >/dev/null <<'PYEOF'
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
PYEOF

# ホストがクローンできるよう、最初のコミットを置く。
cd /srv/loop/project
if ! sudo -u runner git rev-parse HEAD >/dev/null 2>&1; then
  sudo -u runner touch src/.gitkeep tests/.gitkeep plan/.gitkeep
  sudo -u runner tee .gitignore >/dev/null <<'EOF'
.venv/
.runner/
__pycache__/
*.pyc
EOF
  sudo -u runner git add -A
  sudo -u runner git commit -q -m "chore: initial skeleton"
  sudo -u runner git push -q origin main
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
