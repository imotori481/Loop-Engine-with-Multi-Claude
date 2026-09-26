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

[ "$(id -u)" -eq 0 ] || die "run with sudo"

valid_name() {
  [[ "$1" =~ ^[a-z0-9][a-z0-9._-]*$ ]] && [ "$1" != "CURRENT" ] \
    || die "invalid project name: '$1' (lowercase letters, digits, . _ -)"
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
  [ -d "$PROJECTS" ] || { echo "(no projects)"; return 0; }
  for d in "$PROJECTS"/*/; do
    [ -d "$d" ] || continue
    name="$(basename "$d")"
    branch="$(sudo -u runner git -C "$d/repo.git" symbolic-ref --short HEAD 2>/dev/null || echo '?')"
    if [ "$name" = "$cur" ]; then
      state="active"
    elif [ -d "$d/parked" ]; then
      state="parked"
    else
      state="not built"
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
  [ -z "$(current)" ] || die "already managed; the current project is '$(current)'"
  [ -d "$LOOP/repo.git" ] && [ ! -L "$LOOP/repo.git" ] \
    || die "$LOOP/repo.git is not a plain directory; nothing to adopt"
  [ ! -e "$PROJECTS/$name" ] || die "$PROJECTS/$name already exists"
  busy && die "a loop or an agent is running; wait for it to finish"

  ensure_root
  install -d -o root -g root -m 755 "$PROJECTS/$name"
  mv "$LOOP/repo.git" "$PROJECTS/$name/repo.git"
  link_repo "$name"
  echo "$name" > "$CURRENT"
  echo "adopted the current sandbox as '$name'"
}

# 空の bare リポジトリを用意する。ホストはここに作業用ブランチを push し、その後で
# `use` が作業ツリーを作る。bare は runner の所有にする。ホストは runner として
# SSH で push するからだ。
cmd_init() {
  local name="$1" branch="${2:-}"
  valid_name "$name"
  [ ! -e "$PROJECTS/$name" ] || die "$PROJECTS/$name already exists"
  if [ -n "$branch" ]; then
    git check-ref-format --branch "$branch" >/dev/null 2>&1 \
      || die "invalid branch name: '$branch'"
  fi

  ensure_root
  install -d -o root -g root -m 755 "$PROJECTS/$name"
  install -d -o runner -g runner -m 755 "$PROJECTS/$name/repo.git"
  sudo -u runner git init -q --bare -b "${branch:-main}" "$PROJECTS/$name/repo.git"
  # 取り込むブランチの名前を控える。push される前に `use` が走ると、空のリポジトリ
  # から main の骨組みを作ってしまうので、`use` はこれを見て止まる。
  [ -z "$branch" ] || echo "$branch" > "$PROJECTS/$name/branch"
  echo "initialized $PROJECTS/$name/repo.git (HEAD -> ${branch:-main})"
  if [ -n "$branch" ]; then
    echo "next: push '$branch' to it from the host, then run 'use $name'"
  fi
}

park() {
  local name="$1" slot parked="$PROJECTS/$1/parked"
  [ ! -e "$parked" ] || die "$parked already exists; refusing to overwrite it"
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
    [ ! -e "$LOOP/$slot" ] || die "$LOOP/$slot is in the way; refusing to overwrite it"
    mv "$parked/${slot//\//-}" "$LOOP/$slot"
  done
  rmdir "$parked"
}

cmd_use() {
  local name="$1" cur
  valid_name "$name"
  [ -d "$PROJECTS/$name/repo.git" ] || die "no such project: '$name' (run 'init' first)"
  if [ -e "$LOOP/repo.git" ] && [ ! -L "$LOOP/repo.git" ]; then
    die "$LOOP/repo.git is a plain directory; run 'adopt <name>' first"
  fi
  cur="$(current)"
  [ "$cur" != "$name" ] || { echo "'$name' is already the current project"; return 0; }
  if [ -z "$cur" ] && [ -e "$LOOP/project" ]; then
    die "$LOOP/project exists but no project is current; run 'adopt <name>' first"
  fi
  busy && die "a loop or an agent is running; wait for it to finish"

  # 作るときは、取り込むブランチが push 済みであることを先に確かめる。退避を
  # 始めてから止まると、どちらのプロジェクトも使えない状態が残る。
  if [ ! -d "$PROJECTS/$name/parked" ] && [ -f "$PROJECTS/$name/branch" ]; then
    local branch
    branch="$(cat "$PROJECTS/$name/branch")"
    sudo -u runner git -C "$PROJECTS/$name/repo.git" rev-parse -q --verify \
      "refs/heads/$branch" >/dev/null \
      || die "branch '$branch' has not been pushed to $PROJECTS/$name/repo.git yet"
  fi

  [ -z "$cur" ] || park "$cur"
  link_repo "$name"
  echo "$name" > "$CURRENT"

  if [ -d "$PROJECTS/$name/parked" ]; then
    unpark "$name"
    echo "restored '$name'; checking the permission model"
    ADMIN_USER="${ADMIN_USER:-maint}" bash "$HERE/40-perms.sh"
  else
    echo "building '$name' for the first time"
    ADMIN_USER="${ADMIN_USER:-maint}" bash "$HERE/provision.sh"
  fi
  echo "current project: $name"
}

usage() {
  cat >&2 <<EOF
usage: loop project <command>
   or: sudo ADMIN_USER=<maintenance user> bash $HERE/loop-project.sh <command>

commands:
  list                          list the projects; * marks the current one
  current                       print the current project's name
  init <name> [--branch <b>]    prepare an empty project (or one that receives branch <b>)
  use <name>                    switch to a project, building it the first time
  adopt <name>                  name the sandbox as it was before loop-project.sh
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
