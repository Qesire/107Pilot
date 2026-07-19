#!/usr/bin/env bash
#
# accept-cpu-rc-release.sh — unified acceptance entry for the封版门 #4 matrix
# against the local Docker Slurm cpu-rc release candidate.
#
# Chains, in order:
#   1. preflight          — ruff/mypy/pytest/typecheck/vitest/build + static drift
#   2. build_images       — build :cpu-rc-<rev> images (skippable)
#   3. start_stack        — bring up the cpu-rc compose stack
#   4. compose_contract   — simulator/compose check-compose-config.sh
#   5. check_cpu_rc       — partition assertion + success/fail/cancel + Evidence + explicit Capsule
#   6. auto_capsule       — Worker auto-Capsule WITHOUT explicit POST /runs/{id}/capsule
#   7. rule_remediation   — rule-evaluated diagnosis → HTTP remediation session (gap surfacer)
#   8. restart_recovery   — docker compose down + restart preserves volume-persisted runs
#   9. report             — acceptance-report.json + summary; exit 1 if any step failed
#
# Env knobs (with defaults):
#   PILOT107_PUBLIC_URL            REQUIRED (no default). Full public origin the browser uses.
#   PILOT107_CPU_RC_REVISION       default $(git rev-parse --short=12 HEAD)
#   PILOT107_SKIP_BUILD            default 0; 1 skips build_images
#   PILOT107_SKIP_ORIGIN_VALIDATE  default 0; 1 skips start-cpu-rc.sh origin probe
#   PILOT107_ACCEPT_ARTIFACT_DIR   default artifacts/acceptance/cpu-rc-<rev>-<timestamp>
#   PILOT107_ACCEPT_LEAVE_UP       default 0; 1 leaves the stack running on exit
#
# Reuses: scripts/check-ci-local.sh, scripts/build-cpu-rc-images.sh,
# scripts/start-cpu-rc.sh, simulator/compose/scripts/check-compose-config.sh,
# scripts/check-cpu-rc.sh, scripts/smoke-auto-capsule.sh,
# scripts/smoke-cpu-rc-remediation.sh, scripts/smoke-restart-volume-recovery.sh,
# scripts/stop-cpu-rc.sh.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_dir="$root/simulator/compose"

if [[ -z "${PILOT107_PUBLIC_URL:-}" ]]; then
  echo "PILOT107_PUBLIC_URL is unset or empty. Set it to the full public origin" >&2
  echo "the browser uses to reach the deployment, e.g. https://pilot.example.edu:8443" >&2
  exit 1
fi

revision="${PILOT107_CPU_RC_REVISION:-$(git -C "$root" rev-parse --short=12 HEAD)}"
skip_build="${PILOT107_SKIP_BUILD:-0}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
artifact_dir="${PILOT107_ACCEPT_ARTIFACT_DIR:-$root/artifacts/acceptance/cpu-rc-$revision-$timestamp}"
mkdir -p "$artifact_dir"
steps_dir="$artifact_dir/steps"
mkdir -p "$steps_dir"

# Propagate knobs to child scripts (start-cpu-rc.sh, smokes).
export PILOT107_PUBLIC_URL
export PILOT107_SKIP_ORIGIN_VALIDATE="${PILOT107_SKIP_ORIGIN_VALIDATE:-0}"
export PILOT107_SMOKE_PARTITION="${PILOT107_SMOKE_PARTITION:-CPU-RC}"
export PILOT107_SMOKE_QOS="${PILOT107_SMOKE_QOS:-qos_cpu_rc}"
# Gap smokes default BASE_URL to PILOT107_PUBLIC_URL/api/v1.
export PILOT107_COMPETITION_BASE_URL="${PILOT107_COMPETITION_BASE_URL:-${PILOT107_PUBLIC_URL%/}/api/v1}"

started_iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# name|function pairs. report runs last.
STEPS=(
  "preflight|step_preflight"
  "build_images|step_build_images"
  "start_stack|step_start_stack"
  "compose_contract|step_compose_contract"
  "check_cpu_rc|step_check_cpu_rc"
  "auto_capsule|step_auto_capsule"
  "rule_remediation|step_rule_remediation"
  "restart_recovery|step_restart_recovery"
  "report|step_report"
)

log() { printf '%s\n' "$*"; }

run_step() {
  local name="$1" fn="$2"
  local status_file="$steps_dir/$name.status"
  local start_ts end_ts rc
  start_ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  log "=== STEP: $name ==="
  # Run in a subshell with errexit explicitly enabled so a failing command
  # inside the step function aborts the step (and returns its exit code)
  # instead of being masked by the `||` that captures the code. `set -e`
  # inside the subshell is authoritative even though the subshell is the
  # LHS of `||`.
  rc=0
  ( set -e; "$fn" ) || rc=$?
  end_ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if [[ "$rc" -eq 0 ]]; then
    printf 'start=%s\nend=%s\nstatus=PASS\n' "$start_ts" "$end_ts" >"$status_file"
    log "=== STEP: $name PASS ==="
  else
    printf 'start=%s\nend=%s\nstatus=FAIL\nrc=%s\n' "$start_ts" "$end_ts" "$rc" >"$status_file"
    log "=== STEP: $name FAIL (rc=$rc) ==="
  fi
  return 0
}

step_preflight() {
  bash "$root/scripts/check-ci-local.sh"
  # Static drift: the committed src/pilot107/web/static build must match sources.
  if ! git -C "$root" diff --exit-code -- src/pilot107/web/static >/dev/null 2>&1; then
    git -C "$root" --no-pager diff -- src/pilot107/web/static >&2 || true
    echo "static drift detected in src/pilot107/web/static; run npm run build and commit" >&2
    return 1
  fi
}

step_build_images() {
  if [[ "$skip_build" == "1" ]]; then
    echo "PILOT107_SKIP_BUILD=1; skipping image build"
    return 0
  fi
  PILOT107_CPU_RC_REVISION="$revision" bash "$root/scripts/build-cpu-rc-images.sh"
}

step_start_stack() {
  bash "$root/scripts/start-cpu-rc.sh"
}

step_compose_contract() {
  bash "$root/simulator/compose/scripts/check-compose-config.sh"
}

step_check_cpu_rc() {
  bash "$root/scripts/check-cpu-rc.sh"
}

step_auto_capsule() {
  bash "$root/scripts/smoke-auto-capsule.sh"
}

step_rule_remediation() {
  bash "$root/scripts/smoke-cpu-rc-remediation.sh"
}

step_restart_recovery() {
  bash "$root/scripts/smoke-restart-volume-recovery.sh"
}

step_report() {
  python3 - "$artifact_dir" "$steps_dir" "$started_iso" "${STEPS[@]}" <<'PY'
import json
import os
import sys
from pathlib import Path

artifact_dir = Path(sys.argv[1])
steps_dir = Path(sys.argv[2])
started_iso = sys.argv[3]
step_specs = sys.argv[4:]

steps = []
any_fail = False
for spec in step_specs:
    name = spec.split("|", 1)[0]
    status_file = steps_dir / f"{name}.status"
    entry = {"name": name, "status": "MISSING", "start": None, "end": None, "rc": None}
    if status_file.is_file():
        kv = {}
        for line in status_file.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k] = v
        entry["status"] = kv.get("status", "MISSING")
        entry["start"] = kv.get("start")
        entry["end"] = kv.get("end")
        if kv.get("rc") is not None:
            try:
                entry["rc"] = int(kv["rc"])
            except ValueError:
                entry["rc"] = kv["rc"]
        if entry["status"] == "FAIL":
            any_fail = True
    steps.append(entry)

report = {
    "profile": "cpu-rc",
    "revision": os.environ.get("PILOT107_CPU_RC_REVISION", ""),
    "started_at": started_iso,
    "ended_at": max((s["end"] or "" for s in steps), default=""),
    "any_fail": any_fail,
    "steps": steps,
}
(artifact_dir / "acceptance-report.json").write_text(
    json.dumps(report, indent=2, ensure_ascii=False) + "\n"
)
print("=== ACCEPTANCE REPORT ===")
for s in steps:
    print(f"  {s['name']}: {s['status']}")
print(f"report: {artifact_dir / 'acceptance-report.json'}")
PY
}

cleanup() {
  if [[ "${PILOT107_ACCEPT_LEAVE_UP:-0}" != "1" ]]; then
    log "=== CLEANUP: stopping cpu-rc stack (best-effort) ==="
    bash "$root/scripts/stop-cpu-rc.sh" || true
  else
    log "=== CLEANUP: PILOT107_ACCEPT_LEAVE_UP=1; leaving stack running ==="
  fi
}
trap cleanup EXIT

for spec in "${STEPS[@]}"; do
  name="${spec%%|*}"
  fn="${spec#*|}"
  run_step "$name" "$fn"
done

# Final exit code: 1 if any non-report step FAILED.
overall_rc=0
for spec in "${STEPS[@]}"; do
  name="${spec%%|*}"
  [[ "$name" == "report" ]] && continue
  if [[ -f "$steps_dir/$name.status" ]] && grep -q '^status=FAIL' "$steps_dir/$name.status"; then
    overall_rc=1
  fi
done

if [[ "$overall_rc" -ne 0 ]]; then
  log "=== ACCEPTANCE FAILED (see $artifact_dir/acceptance-report.json) ==="
else
  log "=== ACCEPTANCE PASSED ==="
fi
exit "$overall_rc"
