#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -z "${PILOT107_COMPETITION_BASE_URL:-}" ]]; then
  if [[ -n "${PILOT107_PUBLIC_URL:-}" ]]; then
    export PILOT107_COMPETITION_BASE_URL="${PILOT107_PUBLIC_URL%/}/api/v1"
  fi
fi

python3 "$root/scripts/smoke-vm-slurm-authority.py" "$@"
