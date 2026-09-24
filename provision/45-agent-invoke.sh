#!/usr/bin/env bash
# How the runner starts the three agents. Idempotent.
#
# The runner owns the repository and drives the loop, but has no sudo -- and
# 10-users.sh asserts that, because a runner that can become root can undo every
# gate it is supposed to enforce. Yet it still has to start processes as
# DIFFERENT uids, or the write fences (RUNNER_SPEC 1-2) have nobody to apply to.
#
# So: one narrow exception per agent. Runas is limited to (solver), (planner) or
# (critic) and the command list to a single root-owned launcher each. That is
# sideways movement, not escalation -- none of those accounts has privileges of
# its own to inherit. BOOTSTRAP 1-7 forbids handing the loop accounts power
# over the machine; it does not forbid the runner from dropping into the
# accounts it supervises.
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

# The planner's channel, deliberately separate from the solver's.
#
#   brief/  runner writes, planner reads. The planner cannot reach plan/ at all
#           (0700 runner), so everything it is allowed to know arrives here.
#   out/    planner writes a PROPOSAL, runner reads it. The planner never writes
#           tasks.json; the runner applies a proposal only after checking what
#           changed. Sticky, for the same reason the workspace root is: group
#           write would otherwise let the planner delete runner-owned files.
install -d -o root  -g root     -m 755  /srv/loop/planner
install -d -o runner -g plannerw -m 2750 /srv/loop/planner/brief
install -d -o runner -g plannerw -m 3770 /srv/loop/planner/out

# The critic's channel. Same two directories, same modes, and pointedly its own
# group: criticw is not plannerw, so the critic cannot read the proposal the
# planner is writing, and the planner cannot read the findings written about it.
install -d -o root   -g root    -m 755  /srv/loop/critic
install -d -o runner -g criticw -m 2750 /srv/loop/critic/brief
install -d -o runner -g criticw -m 3770 /srv/loop/critic/out

# The human's inbox. Mirror of the planner's out/: someone else writes, the
# runner reads and decides. Sticky for the same reason -- the runner owns the
# directory, so it can clear a consumed input without being able to be
# surprised by one it did not put there.
#
# Not readable by solver or planner. The requirements describe the whole system,
# and handing that to the solver would put a second input channel beside the
# brief (the same reason SYSTEM_SPEC.md lives under plan/).
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

# The planner's credentials are a SEPARATE file with a separate key, readable by
# a different uid. Two reasons, both mechanical rather than tidy-minded:
# a solver that burns through its quota must not be able to stop the planner from
# running, and a sandbox breach must not hand over the credential that drives the
# side which sets the acceptance criteria.
if [ ! -f /etc/loop/planner.env ]; then
  cat > /etc/loop/planner.env <<'EOF'
# Non-interactive credentials for the `planner` account. Fill in by hand.
#
# Preferred -- a subscription token, so planning costs nothing per call:
#     sudo -u planner -H claude setup-token
# It prints a token; paste it after the '=' below. Note this draws on the same
# usage window as your own interactive Claude Code work.
CLAUDE_CODE_OAUTH_TOKEN=

# Fallback -- metered Console credit. planner-run uses this only when the
# subscription token above is empty, and says so when it does.
ANTHROPIC_API_KEY_CONSOLE=
EOF
fi
chown root:planner /etc/loop/planner.env
chmod 640 /etc/loop/planner.env

if [ ! -f /etc/loop/critic.env ]; then
  cat > /etc/loop/critic.env <<'EOF'
# Non-interactive credentials for the `critic` account. Fill in by hand.
#
#     sudo -u critic -H claude setup-token
#
# Separate from the planner's on purpose: the account that judges the work and
# the account that produced it must not be able to exhaust each other's quota,
# and neither should be able to read the other's credential.
CLAUDE_CODE_OAUTH_TOKEN=

# Fallback -- metered Console credit, used only when the token above is empty.
ANTHROPIC_API_KEY_CONSOLE=
EOF
fi
chown root:critic /etc/loop/critic.env
chmod 640 /etc/loop/critic.env

# A malformed drop-in makes sudo refuse to run at all, including the sudo that
# would fix it. Validate before either goes live.
for spec in "solver:91-runner-to-solver" "planner:92-runner-to-planner" \
           "critic:93-runner-to-critic"; do
  who="${spec%%:*}"; file="${spec##*:}"
  tmp="$(mktemp)"
  printf 'runner ALL=(%s) NOPASSWD: /srv/loop/bin/%s-run\n' "$who" "$who" > "$tmp"
  visudo -c -f "$tmp" >/dev/null
  install -o root -g root -m 440 "$tmp" "/etc/sudoers.d/$file"
  rm -f "$tmp"
done

# Verify what actually took effect, not what was written.
granted="$(sudo -l -U runner 2>/dev/null || true)"
for who in solver planner critic; do
  case "$granted" in
    *"($who) NOPASSWD: /srv/loop/bin/$who-run"*)
      : ;;
    *)
      echo "FATAL: runner did not receive the ($who) Runas grant" >&2
      printf '%s\n' "$granted" >&2
      exit 1 ;;
  esac
done

# ...and that it did not receive anything else. `sudo -l` prints a "(ALL : ALL)"
# style line if a broader rule exists anywhere.
case "$granted" in
  *"(ALL"*|*"(root"*)
    echo "FATAL: runner has a Runas grant beyond the three agent accounts" >&2
    printf '%s\n' "$granted" >&2
    exit 1 ;;
esac

# ---- the critic's fence, from the critic's point of view ---------------
#
# Asserted rather than assumed, the same as 40-perms.sh does for the solver. The
# critic's entire value is what it cannot see: one that reads the plan reports
# that every criterion is met, and one that reads tests/ reports that the tests
# pass. Both are true and both are worthless. So the ignorance is checked here,
# where breaking it would be silent.
fail=0
c_can()    { if sudo -u critic "$@" >/dev/null 2>&1; then :; else echo "FAIL: critic should be able to: $*"; fail=1; fi; }
c_cannot() { if sudo -u critic "$@" >/dev/null 2>&1; then echo "FAIL: critic should NOT be able to: $*"; fail=1; fi; }

c_can    test -r /srv/loop/critic/brief
c_can    test -w /srv/loop/critic/out

# The work it is judging.
c_cannot ls /srv/loop/project/plan          # the criteria it would otherwise grade against
c_cannot ls /srv/loop/project/tests         # the tests that already passed
c_cannot ls /srv/loop/project/.git          # history, which contains both
c_cannot ls /srv/loop/planner/out           # the proposal being written
c_cannot ls /srv/loop/brief                 # what the solver was told
# The requirements reach it only through a brief the runner composed. Reading
# the inbox directly would let a critique run against an input nobody handed it.
c_cannot ls /srv/loop/human/in
# Other accounts' credentials and homes.
c_cannot test -r /etc/loop/planner.env
c_cannot ls /home/runner
c_cannot ls /home/planner
c_cannot ls /home/solver
# It reads its brief; it does not get to write one for itself.
c_cannot test -w /srv/loop/critic/brief

if [ "$fail" -ne 0 ]; then
  echo "45-agent-invoke: CRITIC FENCE BROKEN" >&2
  exit 1
fi

# Report what is still missing, per account, rather than a bare "ok".
pending=""
for who in solver planner critic; do
  if ! grep -qE '^(CLAUDE_CODE_OAUTH_TOKEN|ANTHROPIC_API_KEY_CONSOLE)=.+' \
        "/etc/loop/$who.env" 2>/dev/null; then
    pending="$pending $who(token)"
  fi
done
if [ -n "$pending" ]; then
  echo "45-agent-invoke: ok (still unauthenticated:$pending)"
else
  echo "45-agent-invoke: ok"
fi
