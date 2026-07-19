#!/usr/bin/env bash
#
# smoke-cpu-rc-remediation.sh — thin wrapper for the rule-remediation derived-Run
# gap smoke on the cpu-rc profile. See smoke_cpu_rc_remediation.py for the flow
# and the documented gap.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "${PILOT107_COMPETITION_BASE_URL:-}" && -n "${PILOT107_PUBLIC_URL:-}" ]]; then
  export PILOT107_COMPETITION_BASE_URL="${PILOT107_PUBLIC_URL%/}/api/v1"
fi
export PILOT107_SMOKE_PARTITION="${PILOT107_SMOKE_PARTITION:-CPU-RC}"
export PILOT107_SMOKE_QOS="${PILOT107_SMOKE_QOS:-qos_cpu_rc}"
PYTHONPATH="$root/src" python3 "$root/scripts/smoke_cpu_rc_remediation.py"
