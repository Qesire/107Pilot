#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -z "${PILOT107_COMPETITION_BASE_URL:-}" ]]; then
  if [[ -z "${PILOT107_PUBLIC_URL:-}" ]]; then
    echo "set PILOT107_PUBLIC_URL or PILOT107_COMPETITION_BASE_URL" >&2
    exit 2
  fi
  export PILOT107_COMPETITION_BASE_URL="${PILOT107_PUBLIC_URL%/}/api/v1"
fi

python3 "$root/scripts/smoke-vm-agent-task.py" "$@"
