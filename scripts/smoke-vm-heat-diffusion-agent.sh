#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -z "${PILOT107_COMPETITION_BASE_URL:-}" ]]; then
  if [[ -z "${PILOT107_PUBLIC_URL:-}" ]]; then
    printf '%s\n' '{"status":"FAIL","error":"set PILOT107_PUBLIC_URL or PILOT107_COMPETITION_BASE_URL"}'
    exit 2
  fi
  export PILOT107_COMPETITION_BASE_URL="${PILOT107_PUBLIC_URL%/}/api/v1"
fi

export PYTHONPATH="$root/src${PYTHONPATH:+:$PYTHONPATH}"
python3 "$root/scripts/smoke-vm-heat-diffusion-agent.py" "$@"
