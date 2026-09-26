#!/usr/bin/env bash
# 箱に複数のプロジェクトを置き、1つずつ切り替えて使う。root で流す。
# 普段は `loop project <コマンド>` で呼ぶ。`loop` がまだ無い箱では直接流す:
#
#   cd /tmp && sudo ADMIN_USER=<保守ユーザー> bash /opt/loop-engine/provision/loop-project.sh <コマンド>
#
# コマンドは usage() を参照。
#
# 同時に走るのは1つだけ。3役は同じサブスクリプションの枠を分け合うので、並べても
# 枠の上限で待つ時間が増えるだけだ。
#
# 作業ツリーは、切り替えのたびに /srv/loop/project へ mv で出し入れする。ランナーも
# provision も sudoers も /srv/loop/project を名指ししており、パスを変えずに済む。
# venv の実行ファイルも /srv/loop/project/.venv を絶対パスで持っている。シンボリック
# リンクにしないのは、40-perms.sh の chown -R と find がリンクをたどらず、黙って
# 何もしないからだ。
#
# bare リポジトリは動かさない。ホストはプロジェクトごとの決まったパス
# /srv/loop/projects/<名前>/repo.git から引く。/srv/loop/repo.git は今のプロジェクトの
# bare を指すリンクで、作業ツリーの origin はこちらを指す。git はリンクをたどる。
set -euo pipefail

LOOP=/srv/loop
PROJECTS=$LOOP/projects
CURRENT=$PROJECTS/CURRENT
HERE="$(cd "$(dirname "$0")" && pwd)"

# プロジェクトごとに持つ作業場所。どれも走行の途中の状態を持つ。planner/out には
# plan apply を待つ提案が、human/in には要件が、brief/ には最後のブリーフがある。
# 退避先では / を - に替えた名前で parked/ の下に置く。
SLOTS=(project human/in planner/out planner/brief critic/out critic/brief brief)

die() { echo "loop-project: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "sudo で流す"

valid_name() {
  [[ "$1" =~ ^[a-z0-9][a-z0-9._-]*$ ]] && [ "$1" != "CURRENT" ] \
    || die "プロジェクト名に使えない: '$1'（英小文字、数字、. _ - だけ）"
}

current() { [ -f "$CURRENT" ] && cat "$CURRENT" || true; }

# 走っているループの作業場所を動かすと、ランナーは消えた木に書き続ける。
# 3役のアカウントのプロセスは、何であれ走行の一部とみなす。
busy() {
  pgrep -u runner -f 'loop\.py' >/dev/null 2>&1 && return 0
  local u
  for u in solver planner critic; do
    id -u "$u" >/dev/null 2>&1 || continue
    pgrep -u "$u" >/dev/null 2>&1 && return 0
  done
  return 1
}

# /srv/loop/repo.git を、指定のプロジェクトの bare に向け直す。一時的な名前で
# リンクを作ってから rename するので、途中でリンクが無い瞬間ができない。
link_repo() {
  ln -sfn "$PROJECTS/$1/repo.git" "$LOOP/.repo.git.new"
  mv -T "$LOOP/.repo.git.new" "$LOOP/repo.git"
}

ensure_root() {
  install -d -o root -g root -m 755 "$PROJECTS"
}

cmd_list() {
  local cur d name branch state
  cur="$(current)"
  [ -d "$PROJECTS" ] || { echo "（プロジェクトは無い。今の箱に名前を付けるなら 'loop project adopt <名前>'）"; return 0; }
  # 状態の語はどれも3文字にそろえる。printf の幅はバイトで数えるので、字数が
  # そろっていないと列がずれる。
  for d in "$PROJECTS"/*/; do
    [ -d "$d" ] || continue
    name="$(basename "$d")"
    branch="$(sudo -u runner git -C "$d/repo.git" symbolic-ref --short HEAD 2>/dev/null || echo '?')"
    if [ "$name" = "$cur" ]; then
      state="使用中"
    elif [ -d "$d/parked" ]; then
      state="退避中"
    else
      state="未構築"
    fi
    printf '%s %-24s %-10s %s\n' "$([ "$name" = "$cur" ] && echo '*' || echo ' ')" \
      "$name" "$state" "$branch"
  done
}

# 今の箱を、名前を付けたプロジェクトとして登録する。loop-project.sh ができる前に
# 作った箱は、/srv/loop/repo.git が実体のディレクトリになっている。
cmd_adopt() {
  local name="$1"
  valid_name "$name"
  [ -z "$(current)" ] || die "もう名前が付いている。今のプロジェクトは '$(current)'"
  [ -d "$LOOP/repo.git" ] && [ ! -L "$LOOP/repo.git" ] \
    || die "$LOOP/repo.git が実体のディレクトリではない。名前を付けるものが無い"
  [ ! -e "$PROJECTS/$name" ] || die "$PROJECTS/$name がもうある"
  busy && die "ループかエージェントが走っている。終わるのを待つ"

  ensure_root
  install -d -o root -g root -m 755 "$PROJECTS/$name"
  mv "$LOOP/repo.git" "$PROJECTS/$name/repo.git"
  link_repo "$name"
  echo "$name" > "$CURRENT"
  echo "今の箱に '$name' という名前を付けた"
}

# 空の bare リポジトリを用意する。ホストはここに作業用ブランチを push し、その後で
# `use` が作業ツリーを作る。bare は runner の所有にする。ホストは runner として
# SSH で push するからだ。
cmd_init() {
  local name="$1" branch="${2:-}"
  valid_name "$name"
  [ ! -e "$PROJECTS/$name" ] || die "$PROJECTS/$name がもうある"
  if [ -n "$branch" ]; then
    git check-ref-format --branch "$branch" >/dev/null 2>&1 \
      || die "ブランチ名に使えない: '$branch'"
  fi

  ensure_root
  install -d -o root -g root -m 755 "$PROJECTS/$name"
  install -d -o runner -g runner -m 755 "$PROJECTS/$name/repo.git"
  sudo -u runner git init -q --bare -b "${branch:-main}" "$PROJECTS/$name/repo.git"
  # 取り込むブランチの名前を控える。push される前に `use` が走ると、空のリポジトリ
  # から main の骨組みを作ってしまうので、`use` はこれを見て止まる。
  [ -z "$branch" ] || echo "$branch" > "$PROJECTS/$name/branch"
  echo "$PROJECTS/$name/repo.git を作った（HEAD -> ${branch:-main}）"
  if [ -n "$branch" ]; then
    echo "次は、ホストから '$branch' をここへ push し、'loop project use $name' を流す"
  fi
}

park() {
  local name="$1" slot parked="$PROJECTS/$1/parked"
  [ ! -e "$parked" ] || die "$parked がもうある。上書きしない"
  # root だけが入れる。退避中のプロジェクトの src/ や tests/ に、今のプロジェクトの
  # solver が solverw 経由で届かないようにする。
  install -d -o root -g root -m 700 "$parked"
  for slot in "${SLOTS[@]}"; do
    [ -e "$LOOP/$slot" ] || continue
    mv "$LOOP/$slot" "$parked/${slot//\//-}"
  done
}

unpark() {
  local name="$1" slot parked="$PROJECTS/$1/parked"
  for slot in "${SLOTS[@]}"; do
    [ -e "$parked/${slot//\//-}" ] || continue
    [ ! -e "$LOOP/$slot" ] || die "$LOOP/$slot が邪魔をしている。上書きしない"
    mv "$parked/${slot//\//-}" "$LOOP/$slot"
  done
  rmdir "$parked"
}

cmd_use() {
  local name="$1" cur
  valid_name "$name"
  [ -d "$PROJECTS/$name/repo.git" ] || die "プロジェクト '$name' は無い。先に 'loop project init $name'"
  if [ -e "$LOOP/repo.git" ] && [ ! -L "$LOOP/repo.git" ]; then
    die "$LOOP/repo.git が実体のディレクトリのまま。先に 'loop project adopt <名前>'"
  fi
  cur="$(current)"
  [ "$cur" != "$name" ] || { echo "'$name' はもう今のプロジェクトだ"; return 0; }
  if [ -z "$cur" ] && [ -e "$LOOP/project" ]; then
    die "$LOOP/project はあるが、名前が付いていない。先に 'loop project adopt <名前>'"
  fi
  busy && die "ループかエージェントが走っている。終わるのを待つ"

  # 作るときは、取り込むブランチが push 済みであることを先に確かめる。退避を
  # 始めてから止まると、どちらのプロジェクトも使えない状態が残る。
  if [ ! -d "$PROJECTS/$name/parked" ] && [ -f "$PROJECTS/$name/branch" ]; then
    local branch
    branch="$(cat "$PROJECTS/$name/branch")"
    sudo -u runner git -C "$PROJECTS/$name/repo.git" rev-parse -q --verify \
      "refs/heads/$branch" >/dev/null \
      || die "ブランチ '$branch' がまだ $PROJECTS/$name/repo.git に push されていない"
  fi

  [ -z "$cur" ] || park "$cur"
  link_repo "$name"
  echo "$name" > "$CURRENT"

  if [ -d "$PROJECTS/$name/parked" ]; then
    unpark "$name"
    echo "'$name' を戻した。権限を確かめる"
    ADMIN_USER="${ADMIN_USER:-maint}" bash "$HERE/40-perms.sh"
  else
    echo "'$name' を初めて作る"
    # 今のプロジェクトはもう '$name' に書き換えてある。途中で止まったとき、`use` を
    # 打ち直しても「もう今のプロジェクトだ」で終わるので、続きの流し方を示す。
    ADMIN_USER="${ADMIN_USER:-maint}" bash "$HERE/provision.sh" \
      || die "プロビジョニングが途中で止まった。今のプロジェクトはもう '$name' だ。
原因を直してから、'loop project use' ではなく次で続きを流す:
    cd /tmp && sudo ADMIN_USER=${ADMIN_USER:-maint} bash $HERE/provision.sh"
  fi
  echo "今のプロジェクト: $name"
}

usage() {
  cat >&2 <<EOF
使い方: loop project <コマンド>
  または: sudo ADMIN_USER=<保守ユーザー> bash $HERE/loop-project.sh <コマンド>

  list                            プロジェクトの一覧。* が今のもの
  current                         今のプロジェクトの名前
  init <名前> [--branch <ブランチ>]
                                  空のプロジェクトを用意する。--branch を付けると、
                                  ホストから push されるそのブランチを受け入れる
  use <名前>                      切り替える。初めてなら作る
  adopt <名前>                    loop-project.sh より前に作った箱に名前を付ける
EOF
  exit 2
}

case "${1:-}" in
  list)    cmd_list ;;
  current) current ;;
  adopt)   [ $# -eq 2 ] || usage; cmd_adopt "$2" ;;
  init)
    if [ $# -eq 2 ]; then cmd_init "$2"
    elif [ $# -eq 4 ] && [ "$3" = "--branch" ]; then cmd_init "$2" "$4"
    else usage
    fi ;;
  use)     [ $# -eq 2 ] || usage; cmd_use "$2" ;;
  *)       usage ;;
esac
