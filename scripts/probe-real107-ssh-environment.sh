#!/usr/bin/env bash
set -euo pipefail

# Collect a fixed environment inventory from an explicitly configured real107
# SSH target. This is an observation-only harness: it stages one self-contained
# Python file in a private /tmp directory and never reads a project directory.

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'EOF'
Usage:
  PILOT107_REAL107_SSH_TARGET=<ssh-config-alias> \
  bash scripts/probe-real107-ssh-environment.sh

Optional:
  PILOT107_REAL107_ENV_OUTPUT_DIR=<local-artifact-dir>
  PILOT107_REAL107_KEEP_REMOTE=1

The remote inventory is fixed and read-only. It captures directory metadata,
filesystem capacity, selected scheduler configuration, QoS output, runtime
availability, and process limits. It never enumerates directories or reads
project files, environment variables, or credentials.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

target="${PILOT107_REAL107_SSH_TARGET:-}"
if [[ ! "$target" =~ ^[A-Za-z0-9_.@:-]+$ ]]; then
  echo "PILOT107_REAL107_SSH_TARGET must be a configured, safe SSH alias" >&2
  exit 2
fi

probe="$root/scripts/real107_probe/probe_real107_environment.py"
if [[ ! -r "$probe" ]]; then
  echo "missing environment inventory probe: $probe" >&2
  exit 1
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
output_dir="${PILOT107_REAL107_ENV_OUTPUT_DIR:-$root/artifacts/probes/real107-environment-ssh-$stamp}"
if [[ -e "$output_dir" ]]; then
  echo "refusing to overwrite existing output directory: $output_dir" >&2
  exit 2
fi
mkdir -p "$output_dir"

ssh_options=(
  -o BatchMode=yes
  -o ConnectTimeout=15
  -o ServerAliveInterval=10
  -o ServerAliveCountMax=2
)
remote_dir="$(ssh "${ssh_options[@]}" -- "$target" 'umask 077; mktemp -d /tmp/pilot107-real107-environment.XXXXXX')"
if [[ ! "$remote_dir" =~ ^/tmp/pilot107-real107-environment\.[A-Za-z0-9]+$ ]]; then
  echo "remote staging path was not a safe mktemp result" >&2
  exit 1
fi

cleanup_remote() {
  if [[ "${PILOT107_REAL107_KEEP_REMOTE:-0}" != "1" ]]; then
    ssh "${ssh_options[@]}" -- "$target" "rm -rf -- '$remote_dir'" >/dev/null 2>&1 || true
  fi
}
trap cleanup_remote EXIT

scp "${ssh_options[@]}" -- "$probe" "$target:$remote_dir/probe_real107_environment.py"
ssh "${ssh_options[@]}" -- "$target" "
  set -eu
  cd '$remote_dir'
  python3 ./probe_real107_environment.py --out-dir inventory
"
scp -r "${ssh_options[@]}" -- "$target:$remote_dir/inventory" "$output_dir/"

for required in environment_inventory.json redaction-report.json; do
  if [[ ! -f "$output_dir/inventory/$required" ]]; then
    echo "remote inventory did not return required artifact: $required" >&2
    exit 1
  fi
done

printf 'real107 ssh environment inventory=%s\n' "$output_dir/inventory"
