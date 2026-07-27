#!/usr/bin/env bash
set -euo pipefail

# Run the bundled, allowlisted CLI snapshot on an explicitly configured real
# 107 SSH target.  This is an observation-only harness: it never invokes
# sbatch/scancel and never reads a user project directory.

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'EOF'
Usage:
  PILOT107_REAL107_SSH_TARGET=<ssh-config-alias> \
  PILOT107_REAL107_PROBE_ARCHIVE=<verified-probe.tar.gz> \
  bash scripts/probe-real107-ssh-cli.sh

Optional:
  PILOT107_REAL107_CLI_OUTPUT_DIR=<local-artifact-dir>
  PILOT107_REAL107_KEEP_REMOTE=1

The target must be a preconfigured SSH alias (or a safe user@host form). The
remote action only unpacks the supplied signed probe bundle and executes its
fixed read-only CLI snapshot; it does not submit, cancel, or inspect projects.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

target="${PILOT107_REAL107_SSH_TARGET:-}"
archive="${PILOT107_REAL107_PROBE_ARCHIVE:-}"
if [[ ! "$target" =~ ^[A-Za-z0-9_.@:-]+$ ]]; then
  echo "PILOT107_REAL107_SSH_TARGET must be a configured, safe SSH alias" >&2
  exit 2
fi
if [[ -z "$archive" || ! -f "$archive" ]]; then
  echo "PILOT107_REAL107_PROBE_ARCHIVE must name an existing probe archive" >&2
  exit 2
fi

checksum="${archive}.sha256"
if [[ ! -f "$checksum" ]]; then
  echo "refusing an archive without its adjacent .sha256 file" >&2
  exit 2
fi
(
  cd "$(dirname "$archive")"
  sha256sum -c "$(basename "$checksum")"
)

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
output_dir="${PILOT107_REAL107_CLI_OUTPUT_DIR:-$root/artifacts/probes/real107-cli-ssh-$stamp}"
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
remote_dir="$(ssh "${ssh_options[@]}" -- "$target" 'umask 077; mktemp -d /tmp/pilot107-real107-probe.XXXXXX')"
if [[ ! "$remote_dir" =~ ^/tmp/pilot107-real107-probe\.[A-Za-z0-9]+$ ]]; then
  echo "remote staging path was not a safe mktemp result" >&2
  exit 1
fi

cleanup_remote() {
  if [[ "${PILOT107_REAL107_KEEP_REMOTE:-0}" != "1" ]]; then
    ssh "${ssh_options[@]}" -- "$target" "rm -rf -- '$remote_dir'" >/dev/null 2>&1 || true
  fi
}
trap cleanup_remote EXIT

scp "${ssh_options[@]}" -- "$archive" "$target:$remote_dir/probe.tar.gz"
ssh "${ssh_options[@]}" -- "$target" "
  set -eu
  cd '$remote_dir'
  tar -xzf probe.tar.gz
  bundle_dir=\$(find . -maxdepth 1 -type d -name 'pilot107-real107-probe-*' -print -quit)
  test -n \"\$bundle_dir\"
  python3 \"\$bundle_dir/probe_real107_cli_snapshot.py\" --out-dir snapshot
"
scp -r "${ssh_options[@]}" -- "$target:$remote_dir/snapshot" "$output_dir/"

for required in platform_snapshot.json manifest.json redaction-report.json; do
  if [[ ! -f "$output_dir/snapshot/$required" ]]; then
    echo "remote probe did not return required artifact: $required" >&2
    exit 1
  fi
done

printf 'real107 ssh cli snapshot=%s\n' "$output_dir/snapshot"
