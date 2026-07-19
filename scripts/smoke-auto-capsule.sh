#!/usr/bin/env bash
#
# smoke-auto-capsule.sh — thin wrapper for the auto-Capsule end-to-end gap smoke.
# Assumes PILOT107_COMPETITION_BASE_URL (or PILOT107_PUBLIC_URL) points at a
# running cpu-rc stack and PILOT107_AUTO_CAPSULE is default-on (the worker
# builds a Capsule after Evidence SUCCEEDED without an explicit POST).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "${PILOT107_COMPETITION_BASE_URL:-}" && -n "${PILOT107_PUBLIC_URL:-}" ]]; then
  export PILOT107_COMPETITION_BASE_URL="${PILOT107_PUBLIC_URL%/}/api/v1"
fi
export PILOT107_SMOKE_PARTITION="${PILOT107_SMOKE_PARTITION:-CPU-RC}"
export PILOT107_SMOKE_QOS="${PILOT107_SMOKE_QOS:-qos_cpu_rc}"
PYTHONPATH="$root/src" python3 "$root/scripts/smoke_auto_capsule.py"
