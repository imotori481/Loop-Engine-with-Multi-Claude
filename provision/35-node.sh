#!/usr/bin/env bash
# The Node toolchain, and the reason it exists: a user interface a machine can
# check.
#
# tkinter cannot be driven without a display, and there is no display here.
# Run 7 shipped because of that gap -- ten green steps, 42 passing tests, and a
# window in which every button was disabled from the first frame. No acceptance
# criterion could have caught it, because the only thing the runner could see
# was the pure functions the plan had pushed the UI out into.
#
# A DOM does not need a screen. happy-dom builds one in memory, so a test can
# click the button and assert what changed -- which turns "is there anything
# the player can press?" from a question only a human could answer into an
# ordinary red test. Measured before this script was written:
#
#     3 passed against a working UI
#     2 failed against run 7's shape, with "expected 0 to be greater than 0"
#
# The freeze is the same idea as 30-python.sh (the solver may read and execute
# the toolchain, never write to it) but it is NOT the same mechanism, because
# Node resolves modules from the filesystem rather than from a path the runner
# controls. Two consequences, both handled below.
set -euo pipefail

TOOLS=/srv/loop/node
P=/srv/loop/project

# Pinned. A range would let a `npm install` months from now change what the
# gates mean without anyone choosing that.
VITEST_VERSION="4.1.11"
HAPPY_DOM_VERSION="20.13.1"

command -v node >/dev/null || { echo "35-node: node is not installed" >&2; exit 1; }
cd "$(dirname "$0")"

install -d -o root -g root -m 755 /srv/loop/bin
install -o root -g root -m 755 bin/smoke-dom /srv/loop/bin/smoke-dom

# ---- the toolchain, outside the project --------------------------------
#
# Outside on purpose. It has to be ignored by git -- nobody commits
# node_modules, and `git status` would be unreadable if they did -- and
# `assert_touched()` finds a stray solver file by asking git what is untracked.
# Anything git ignores is invisible to that check. Keeping the real tree out of
# the project means the ignore rule can be anchored to the root alone (below),
# so a `node_modules/` the solver creates anywhere else still shows up.
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

# Writable while npm works, frozen immediately after.
#
# --legacy-peer-deps: vitest が任意で宣言するピア依存を自動で入れない。
# npm 10.9 はその解決中に @vitest/browser-playwright の最新版（vitest 5 向け）を
# 拾い、`Cannot read properties of null (reading 'edgesOut')` で落ちる。
# この箱で使うのは package.json に書いた2つだけなので、自動解決は要らない。
chmod -R u+w "$TOOLS"
sudo -u runner npm install --prefix "$TOOLS" --no-audit --no-fund --silent --legacy-peer-deps

# ---- freeze ------------------------------------------------------------
# Readable and executable by everyone, writable by runner alone. A stuck solver
# cannot npm-install its way out of a problem, for the same reason it cannot
# pip-install its way out of one.
chown -R runner:runner "$TOOLS"
chmod -R go-w "$TOOLS"

# ---- reach it from the project -----------------------------------------
# A symlink rather than a copy: one tree to freeze, and `project/node_modules`
# is where Node looks. The link is owned by runner and the project root carries
# the sticky bit, so the solver can neither replace nor remove it.
if [ ! -L "$P/node_modules" ]; then
  rm -rf "$P/node_modules"
  sudo -u runner ln -s "$TOOLS/node_modules" "$P/node_modules"
fi

# ---- the ignore rule is part of the fence ------------------------------
#
# `/node_modules` and not `node_modules`. The second form ignores the name at
# every depth, which would hide a `src/node_modules` the solver created --
# and with the network open, that is a working route to any package on the
# registry. Anchored to the root, only the frozen symlink is ignored and
# anything else by that name is untracked, which is what assert_touched()
# reports as writing outside the allowlist.
IGNORE="$P/.gitignore"
if grep -qx 'node_modules/\?' "$IGNORE" 2>/dev/null; then
  echo "35-node: refusing: $IGNORE ignores node_modules at every depth" >&2
  echo "  Change that line to /node_modules -- see the comment above." >&2
  exit 1
fi
grep -qx '/node_modules' "$IGNORE" 2>/dev/null || \
  sudo -u runner tee -a "$IGNORE" >/dev/null <<<'/node_modules'

# ---- the config, owned by runner like conftest.py ----------------------
# Environment, not plan: which DOM the tests get is not something a step may
# decide. `.mjs` because the project's package.json is the solver's to write
# and may not say "type": "module".
#
# The mode is set here rather than left to 40-perms.sh, and the assertion below
# is why: the project root is setgid solverw, so a file runner creates there
# inherits group solverw, and runner's umask of 002 makes it 664 -- writable by
# the solver. Depending on a later script to close a fence is how it came to be
# standing open once already (see 20-layout.sh).
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

# ---- the page, and why the runner owns it ------------------------------
#
# The artifact is something a person opens, and the file they open has to sit
# at the root -- which is exactly where the write fence does not reach. src/
# and tests/ are the only two directories the runner can open and close, so a
# plan that listed index.html in files_write would be rejected by L12, and a
# solver that wrote one anyway would be caught by assert_touched.
#
# So the environment provides it, like conftest.py. That is not a workaround,
# it is the fix for the thing run 7 shipped: the launch path was the one path
# no gate covered, because it was the one path the tests fabricated their way
# around. Here it is a fact of the box -- it always exists, and it always calls
# the same exported function. What remains is `src/main.ts`, which is under the
# fence, is checkable, and is the plan's to write.
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

# ---- assertions, from the solver's point of view -----------------------
# A permission model nobody tested is a permission model that does not exist.
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
# The link itself: the solver may follow it and may not swap it for its own.
chk_cannot rm -f "$P/node_modules"

"$TOOLS/node_modules/.bin/vitest" --version

if [ "$fail" -eq 0 ]; then
  echo "35-node: ok (all assertions passed)"
else
  echo "35-node: NODE FREEZE BROKEN" >&2
fi
exit "$fail"
