# 書き込みの柵の場所を読む。ほかのスクリプトが source する。
#
#   LAYOUT_SRC    コードの場所。作業ツリーの根からの相対パス（既定 src）
#   LAYOUT_TESTS  テストの場所（既定 tests）
#
# /srv/loop/layout.json は loop-project.sh が今のプロジェクトのものへ向けるリンク。
# 読むのは runner/loop.py の LAYOUT で、規則もそこにしか書かない。壊れた値なら
# import の時点で止まる。ここで別に読むと、ランナーが拒む値でディレクトリを作り、
# chown することになる。
_layout_runner="$(cd "$(dirname "${BASH_SOURCE[0]}")/../runner" && pwd)"
_layout="$(python3 -B -c "import sys; sys.path.insert(0, sys.argv[1]); import loop; print(loop.LAYOUT['src'], loop.LAYOUT['tests'])" "$_layout_runner")" \
  || { echo "layout: /srv/loop/layout.json を読めない" >&2; exit 1; }
read -r LAYOUT_SRC LAYOUT_TESTS <<<"$_layout"
unset _layout _layout_runner
