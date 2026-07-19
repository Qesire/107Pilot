#!/usr/bin/env bash
#
# smoke-restart-volume-recovery.sh — thin wrapper for the restart + volume
# recovery gap smoke. Proves ``docker compose down`` mid-stack + restart
# preserves volume-persisted run state and the stack still serves new runs.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "${PILOT107_COMPETITION_BASE_URL:-}" && -n "${PILOT107_PUBLIC_URL:-}" ]]; then
  export PILOT107_COMPETITION_BASE_URL="${PILOT107_PUBLIC_URL%/}/api/v1"
fi
export PILOT107_SMOKE_PARTITION="${PILOT107_SMOKE_PARTITION:-CPU-RC}"
export PILOT107_SMOKE_QOS="${PILOT107_SMOKE_QOS:-qos_cpu_rc}"
PYTHONPATH="$root/src" python3 "$root/scripts/smoke_restart_volume_recovery.py"
