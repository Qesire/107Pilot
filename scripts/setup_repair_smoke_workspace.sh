#!/usr/bin/env bash
#
# setup_repair_smoke_workspace.sh — prepare a git workspace in the Docker
# simulator shared volume for the M2 repair-ticket end-to-end smoke.
#
# Creates /public/home/alice/repair-smoke/ with a buggy train.py that raises
# FileNotFoundError (traceback), committed to a fresh git repo so the
# CodeContextService can capture a source window.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_dir="$root/simulator/compose"
env_file="${PILOT107_SMOKE_ENV:-$compose_dir/.env.repair-smoke}"
project="${COMPOSE_PROJECT_NAME:-pilot107-sim}"

echo "==> Preparing repair-smoke workspace in Docker shared volume…"

docker compose \
  --project-name "$project" \
  --env-file "$env_file" \
  -f "$compose_dir/compose.yml" \
  exec -T login-node-sim bash -c '
set -euo pipefail
if ! command -v git >/dev/null 2>&1; then
  apt-get update -qq && apt-get install -y -qq git >/dev/null 2>&1
fi
WORKDIR=/public/home/alice/repair-smoke
rm -rf "$WORKDIR"
mkdir -p "$WORKDIR"
cd "$WORKDIR"
git init -q
git config user.email "alice@sim.local"
git config user.name "alice"
git config --global --add safe.directory "$WORKDIR"

cat > train.py << "PYEOF"
import sys


def main():
    data = load_data()
    print(f"loaded {len(data)} items")


def load_data():
    # BUG: file does not exist — will raise FileNotFoundError
    with open("missing_input.csv") as f:
        return f.readlines()


if __name__ == "__main__":
    main()
PYEOF

git add train.py
git commit -q -m "initial buggy version"
chown -R alice:alice "$WORKDIR"
# Allow the pilot107 API container (uid 10700) to traverse into the alice
# home directory and read the workspace (local code-context transport).
chmod o+x /public/home/alice
chmod -R o+rX "$WORKDIR"
echo "workspace ready: $(git rev-parse HEAD)"
'

echo "==> Repair-smoke workspace prepared."
