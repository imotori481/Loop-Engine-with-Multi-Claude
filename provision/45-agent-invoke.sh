#!/usr/bin/env bash
# ランナーが3つのエージェントを起動する仕組み。冪等。
#
# ランナーはリポジトリを所有してループを回すが、sudo を持たない。10-users.sh は
# それを確かめる。root になれるランナーは、強制するはずの関門をすべて外せる
# からだ。それでも、別の uid でプロセスを起こす必要がある。そうしないと、
# 書き込みの柵（RUNNER_SPEC 1-2）を当てる相手がいない。
#
# だから、エージェントごとに狭い例外を1つずつ置く。Runas は (solver)、(planner)、
# (critic) のどれかに限り、コマンドはそれぞれ root 所有の起動スクリプト1本に
# 限る。これは横への移動で、昇格ではない。どのアカウントも、継げる権限を自分では
# 持っていない。BOOTSTRAP 1-7 はループのアカウントに機械への権力を渡すことを
# 禁じているが、ランナーが監督するアカウントに降りることは禁じていない。
set -euo pipefail
cd "$(dirname "$0")"

install -d -o root -g root -m 755 /srv/loop/bin
install -o root -g root -m 755 bin/solver-run   /srv/loop/bin/solver-run
install -o root -g root -m 755 bin/planner-run  /srv/loop/bin/planner-run
install -o root -g root -m 755 bin/critic-run   /srv/loop/bin/critic-run
# solver-run が名前 `claude` で呼ぶ実体。ランナーは名前しか渡せないので、
# バックエンドを増やせるのは root がここで置いたときだけ。
install -o root -g root -m 755 bin/solver-claude /srv/loop/bin/solver-claude
install -o root -g root -m 755 bin/smoke-solver /srv/loop/bin/smoke-solver
install -o root -g root -m 755 bin/smoke-pytest /srv/loop/bin/smoke-pytest
install -o root -g root -m 755 bin/smoke-planner /srv/loop/bin/smoke-planner
install -o root -g root -m 755 bin/smoke-plan    /srv/loop/bin/smoke-plan
install -o root -g root -m 755 bin/smoke-critic  /srv/loop/bin/smoke-critic

# planner の経路。solver の経路とはあえて分ける。
#
#   brief/  runner が書き、planner が読む。planner は plan/ にまったく届かない
#           （0700 runner）ので、知ってよいことはすべてここに届く。
#   out/    planner が「提案」を書き、runner が読む。planner は tasks.json を
#           書かない。runner は何が変わったかを確かめてから提案を適用する。
#           ワークスペースの根と同じ理由でスティッキーにする。グループの書き込み
#           だけだと、planner が runner 所有のファイルを消せてしまう。
install -d -o root  -g root     -m 755  /srv/loop/planner
install -d -o runner -g plannerw -m 2750 /srv/loop/planner/brief
install -d -o runner -g plannerw -m 3770 /srv/loop/planner/out

# critic の経路。同じ2つのディレクトリ、同じモードで、グループははっきり別に
# する。criticw は plannerw ではないので、critic は planner が書いている提案を
# 読めず、planner は自分について書かれた指摘を読めない。
install -d -o root   -g root    -m 755  /srv/loop/critic
install -d -o runner -g criticw -m 2750 /srv/loop/critic/brief
install -d -o runner -g criticw -m 3770 /srv/loop/critic/out

# 人間の受け渡し口。planner の out/ と対になる形で、誰かが書き、runner が読んで
# 決める。スティッキーにする理由も同じ。runner がディレクトリを所有するので、
# 使い終えた入力を片付けられ、自分が置いていない入力に不意を突かれることもない。
#
# solver と planner からは読めない。要件はシステム全体を述べており、それを
# solver に渡すとブリーフの横に2つ目の入力経路ができる（SYSTEM_SPEC.md が
# plan/ の下にあるのと同じ理由）。
install -d -o root   -g root   -m 755  /srv/loop/human
install -d -o runner -g humanw -m 3770 /srv/loop/human/in

install -d -o root -g root -m 755 /etc/loop

# ソルバーの資格情報。planner.env や critic.env とは別のファイルにし、
# solver だけが読めるようにする。ある役の資格情報が漏れても、他の役の資格情報は
# 渡らない。
#
# codex バックエンドはこのファイルを使わない。solver 自身の ChatGPT ログインが
# /home/solver/.codex（0700）に入る:
#
#     sudo -u solver -H codex login --device-auth
if [ ! -f /etc/loop/solver.env ]; then
  cat > /etc/loop/solver.env <<'EOF'
# `solver` アカウントの非対話用資格情報。手で埋める。
#
# 推奨: サブスクリプションのトークン。
#     sudo -u solver -H claude setup-token
# 表示されたトークンを '=' の後ろに貼る。プランナー、クリティック、
# あんた自身の対話作業と同じ利用枠を使う。
CLAUDE_CODE_OAUTH_TOKEN=

# 予備: 従量課金の Console クレジット。上のトークンが空のときだけ使う。
ANTHROPIC_API_KEY_CONSOLE=
EOF
fi
chown root:solver /etc/loop/solver.env
chmod 640 /etc/loop/solver.env

# planner の資格情報は、別のキーを持つ別のファイルで、別の uid が読む。理由は
# 2つあり、どちらも整理のためではなく仕組みのためだ。利用枠を使い切った solver
# が planner の実行を止められてはならない。サンドボックスが破られても、受け入れ
# 条件を決める側を動かす資格情報まで渡ってはならない。
if [ ! -f /etc/loop/planner.env ]; then
  cat > /etc/loop/planner.env <<'EOF'
# `planner` アカウントの非対話用資格情報。手で埋める。
#
# 推奨: サブスクリプションのトークン。計画づくりに呼び出しごとの費用がかからない。
#     sudo -u planner -H claude setup-token
# 表示されたトークンを '=' の後ろに貼る。あんた自身の対話作業と
# 同じ利用枠を使う。
CLAUDE_CODE_OAUTH_TOKEN=

# 予備: 従量課金の Console クレジット。planner-run は上のトークンが空のときだけ
# これを使い、使うときはそう表示する。
ANTHROPIC_API_KEY_CONSOLE=
EOF
fi
chown root:planner /etc/loop/planner.env
chmod 640 /etc/loop/planner.env

if [ ! -f /etc/loop/critic.env ]; then
  cat > /etc/loop/critic.env <<'EOF'
# `critic` アカウントの非対話用資格情報。手で埋める。
#
#     sudo -u critic -H claude setup-token
#
# planner のものとあえて分ける。作業を判定するアカウントと作業を作った
# アカウントが、互いの利用枠を使い切れてはならず、互いの資格情報を読めても
# ならない。
CLAUDE_CODE_OAUTH_TOKEN=

# 予備: 従量課金の Console クレジット。上のトークンが空のときだけ使う。
ANTHROPIC_API_KEY_CONSOLE=
EOF
fi
chown root:critic /etc/loop/critic.env
chmod 640 /etc/loop/critic.env

# 役ごとのモデル。起動スクリプトが LOOP_MODEL を --model で渡す。
# 新しく作ったファイルにも、前からあるファイルにも、行が無いときだけ足す。
# 値は手で埋めたものを上書きしない。
for who in solver planner critic; do
  if ! grep -q '^LOOP_MODEL=' "/etc/loop/$who.env"; then
    cat >> "/etc/loop/$who.env" <<'EOF'

# モデル。空ならアカウントの既定のモデルを使う。
# 例: LOOP_MODEL=claude-sonnet-5
LOOP_MODEL=
EOF
  fi
  if ! grep -q '^LOOP_EFFORT=' "/etc/loop/$who.env"; then
    cat >> "/etc/loop/$who.env" <<'EOF'

# effort。low / medium / high / xhigh / max のどれか。空なら既定。
LOOP_EFFORT=
EOF
  fi
done

# 形の壊れた drop-in があると、sudo は一切動かなくなる。それを直すための sudo も
# 含めてだ。どちらも有効にする前に検証する。
for spec in "solver:91-runner-to-solver" "planner:92-runner-to-planner" \
           "critic:93-runner-to-critic"; do
  who="${spec%%:*}"; file="${spec##*:}"
  tmp="$(mktemp)"
  printf 'runner ALL=(%s) NOPASSWD: /srv/loop/bin/%s-run\n' "$who" "$who" > "$tmp"
  visudo -c -f "$tmp" >/dev/null
  install -o root -g root -m 440 "$tmp" "/etc/sudoers.d/$file"
  rm -f "$tmp"
done

# 書いたものではなく、実際に効いたものを確かめる。
granted="$(sudo -l -U runner 2>/dev/null || true)"
for who in solver planner critic; do
  case "$granted" in
    *"($who) NOPASSWD: /srv/loop/bin/$who-run"*)
      : ;;
    *)
      echo "FATAL: runner に ($who) として起動する許可が付いていない" >&2
      printf '%s\n' "$granted" >&2
      exit 1 ;;
  esac
done

# ……そして、それ以外は何も受け取っていないこと。どこかにもっと広い規則が
# あれば、`sudo -l` は "(ALL : ALL)" のような行を出す。
case "$granted" in
  *"(ALL"*|*"(root"*)
    echo "FATAL: runner に、3役のアカウント以外として起動する許可が付いている" >&2
    printf '%s\n' "$granted" >&2
    exit 1 ;;
esac

# ---- critic の柵。critic の視点で確かめる ------------------------------
#
# 40-perms.sh が solver について行うのと同じく、前提にせず確かめる。critic の
# 価値はすべて、見えないものにある。計画を読む critic は全基準を満たしていると
# 報告し、tests/ を読む critic はテストが通ると報告する。どちらも真で、どちらも
# 無価値だ。だから、破れても誰も気づかないこの無知を、ここで確かめる。
fail=0
c_can()    { if sudo -u critic "$@" >/dev/null 2>&1; then :; else echo "FAIL: critic should be able to: $*"; fail=1; fi; }
c_cannot() { if sudo -u critic "$@" >/dev/null 2>&1; then echo "FAIL: critic should NOT be able to: $*"; fail=1; fi; }

c_can    test -r /srv/loop/critic/brief
c_can    test -w /srv/loop/critic/out

# 判定する対象の作業。
c_cannot ls /srv/loop/project/plan          # 見れば判定の拠り所にしてしまう基準
c_cannot ls /srv/loop/project/tests         # すでに通ったテスト
c_cannot ls /srv/loop/project/.git          # 両方を含む履歴
c_cannot ls /srv/loop/planner/out           # 書かれている最中の提案
c_cannot ls /srv/loop/brief                 # solver に伝えた内容
c_cannot ls /srv/loop/logs                  # 失敗したテストの中身を含む走行ログ
# 要件は、runner が組み立てたブリーフを通してだけ届く。受け渡し口を直接読めると、
# 誰も渡していない入力に対して批評が走りうる。
c_cannot ls /srv/loop/human/in
# ほかのアカウントの資格情報とホーム。
c_cannot test -r /etc/loop/planner.env
c_cannot ls /home/runner
c_cannot ls /home/planner
c_cannot ls /home/solver
# ブリーフは読むもので、自分で書くものではない。
c_cannot test -w /srv/loop/critic/brief

if [ "$fail" -ne 0 ]; then
  echo "45-agent-invoke: CRITIC FENCE BROKEN" >&2
  exit 1
fi

# ---- 資格情報の柵、各役の視点で ------------------------------------------
#
# 3役が同じ Claude の資格情報の形を持つので、ファイルの権限だけが役を分ける。
# 各役は自分の .env を読めて、他の役の .env を読めないことを確かめる。
#
# 40-perms.sh ではなくここで検査する。.env を作るのはこのスクリプトで、
# 40-perms.sh はその前に走る。無いファイルへの `test -r` は失敗するので、
# 「読めない」の検査が何も確かめずに通ってしまう。
fail=0
for who in solver planner critic; do
  [ -f "/etc/loop/$who.env" ] || { echo "FAIL: /etc/loop/$who.env does not exist"; fail=1; continue; }
  if ! sudo -u "$who" test -r "/etc/loop/$who.env" 2>/dev/null; then
    echo "FAIL: $who should be able to: test -r /etc/loop/$who.env"
    fail=1
  fi
  for other in solver planner critic; do
    [ "$other" = "$who" ] && continue
    if sudo -u "$other" test -r "/etc/loop/$who.env" 2>/dev/null; then
      echo "FAIL: $other should NOT be able to: test -r /etc/loop/$who.env"
      fail=1
    fi
  done
done

if [ "$fail" -ne 0 ]; then
  echo "45-agent-invoke: CREDENTIAL FENCE BROKEN" >&2
  exit 1
fi

# 単に "ok" と言わず、まだ足りないものをアカウントごとに報告する。
pending=""
for who in solver planner critic; do
  if ! grep -qE '^(CLAUDE_CODE_OAUTH_TOKEN|ANTHROPIC_API_KEY_CONSOLE)=.+' \
        "/etc/loop/$who.env" 2>/dev/null; then
    pending="$pending $who(token)"
  fi
done
if [ -n "$pending" ]; then
  echo "45-agent-invoke: ok（資格情報がまだ無い役:$pending。provision/README §2-9）"
else
  echo "45-agent-invoke: ok"
fi
