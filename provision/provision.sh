#!/usr/bin/env bash
# Provision the loop sandbox inside the WSL2 Ubuntu-24.04 distro.
# Idempotent: safe to re-run.
#
#   sudo ./provision.sh
#
# The distro's default (maintenance) user is assumed to be `maint`. If it is
# named something else:  sudo ADMIN_USER=<name> ./provision.sh
#
# Order matters. 15-authkeys must precede 50-lockdown (which is what grants
# runner a login at all), and 40-perms must follow everything that creates
# files under /srv/loop.
set -euo pipefail
cd "$(dirname "$0")"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

# 05-isolation runs first: if wsl.conf never took effect there is no sandbox to
# provision into, and every later step would succeed while meaning nothing.
for s in 05-isolation.sh 10-users.sh 15-authkeys.sh 20-layout.sh 25-runner.sh 30-python.sh 35-node.sh 40-perms.sh 45-agent-invoke.sh 50-lockdown.sh; do
  echo
  echo "=== $s ==="
  bash "$s"
done

echo
echo "=== provisioning complete ==="
echo "Next: put a key in /etc/loop/solver.env, then run the first experiment:"
echo "    sudo -u runner /srv/loop/bin/smoke-solver"
echo
echo "60-egress.sh was NOT run: the solver CLI and its API endpoint are still"
echo "undecided (RUNNER_SPEC section 11, item 1). Until it runs, the solver"
echo "account has unrestricted outbound network access."
echo
echo "Reminder: this sandbox only stays up while a wsl.exe session holds it."
echo "The keepalive scheduled task on Windows is what does that; unattended"
echo "runs across a logoff are not possible here (see provision/README 3-1)."
