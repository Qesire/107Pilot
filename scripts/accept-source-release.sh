#!/usr/bin/env bash
#
# accept-source-release.sh — source-tree acceptance bound to a git SHA.
#
# Runs the full source-level CI gate (lint, types, unit tests, web unit tests,
# Playwright UI tests, web build, static-build drift check, compose-config
# validation) against a source checkout. It does NOT build or run the Docker
# stack — runtime closure is the job of accept-runtime-bundle.sh.
#
# This script is the offline-bundle companion to check-ci-local.sh: it adds
# the Playwright UI suite (`npm run test:ui`) that check-ci-local.sh omits, so
# the source-level gate matches what GitHub CI blocks on.
#
# Emit a JSON report with the SHA and per-step PASS/FAIL. Exit non-zero if any
# step FAILs.
#
# Env knobs:
#   PILOT107_SOURCE_ACCEPT_ARTIFACT_DIR  default artifacts/acceptance/source-<sha>-<ts>
#   PILOT107_ACCEPT_SEAL_MODE            default 0. When 1, KNOWN_SKIP is
#                                        treated as FAIL in the final
#                                        aggregation (formal seal mode
#                                        requires every step to PASS).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! git -C "$root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "accept-source-release.sh must run inside a git checkout (source tree)" >&2
  exit 1
fi

revision="$(git -C "$root" rev-parse HEAD)"
short_revision="${revision:0:12}"

# Dirty-worktree guard (matches export-cpu-rc-bundle.sh). The report records
# `git rev-parse HEAD`; if tracked files are modified, uncommitted code can
# pass while the report claims the HEAD sha. Fail closed on tracked dirty;
# untracked files are recorded for transparency but do not fail.
if ! git -C "$root" diff --quiet || ! git -C "$root" diff --cached --quiet; then
  echo "source acceptance requires a clean tracked worktree; commit or stash changes before running" >&2
  git -C "$root" --no-pager diff >&2 || true
  exit 1
fi
untracked_status="$(git -C "$root" status --porcelain --untracked-files)"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
artifact_dir="${PILOT107_SOURCE_ACCEPT_ARTIFACT_DIR:-$root/artifacts/acceptance/source-$short_revision-$timestamp}"
mkdir -p "$artifact_dir"
steps_dir="$artifact_dir/steps"
mkdir -p "$steps_dir"
started_iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# P1-3 (round 6): seal_mode is read ONCE here (before the report Python) so
# both the JSON report writer and the final Bash aggregation see the same
# value and produce a consistent exit/any_fail pair. The report Python uses
# it to compute any_fail with the seal-mode-aware formula; the Bash
# aggregation uses it to treat KNOWN_SKIP as FAIL.
seal_mode="${PILOT107_ACCEPT_SEAL_MODE:-0}"

log() { printf '%s\n' "$*"; }

# Exit-code → status mapping (matches accept-runtime-bundle.sh):
#   rc=0   → PASS
#   rc=77  → KNOWN_SKIP (reserved for explicit capability-probe architectural
#            skip; no source step emits 77 today)
#   else   → FAIL  (strict; any non-zero other than 77 is a real regression)
run_step() {
  local name="$1" fn="$2"
  local status_file="$steps_dir/$name.status"
  local log_file="$steps_dir/$name.log"
  local start_ts end_ts rc
  start_ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  log "=== STEP: $name ==="
  # CRITICAL: do NOT use `( set -e; "$fn" ) || rc=$?`. A subshell on the LHS
  # of `||` is in a conditional context, so `set -e` inside it does NOT
  # reliably propagate — an intermediate failing command can be swallowed
  # and the function may continue to a final success, yielding a false PASS.
  # Run the subshell outside any conditional, capturing rc directly via
  # PIPESTATUS[0] (the subshell's rc, NOT tee's). The `set +e` / `set -e`
  # bracket preserves the script's errexit expectations around this block.
  # `tee` streams the step's full stdout+stderr to the console AND captures
  # it to $log_file for the per-step evidence log.
  rc=0
  set +e
  ( set -e; "$fn" ) 2>&1 | tee "$log_file"
  rc=${PIPESTATUS[0]}
  set -e
  end_ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if [[ "$rc" -eq 0 ]]; then
    printf 'start=%s\nend=%s\nstatus=PASS\n' "$start_ts" "$end_ts" >"$status_file"
    log "=== STEP: $name PASS ==="
  elif [[ "$rc" -eq 77 ]]; then
    printf 'start=%s\nend=%s\nstatus=KNOWN_SKIP\nrc=%s\n' "$start_ts" "$end_ts" "$rc" >"$status_file"
    log "=== STEP: $name KNOWN_SKIP (rc=$rc) ==="
  else
    printf 'start=%s\nend=%s\nstatus=FAIL\nrc=%s\n' "$start_ts" "$end_ts" "$rc" >"$status_file"
    log "=== STEP: $name FAIL (rc=$rc) ==="
  fi
  return 0
}

step_ruff() {
  ( cd "$root" && uv run --extra dev ruff check src tests scripts )
}

# P2-1 (round 4): locked-sync steps prove reproducibility from the locked
# manifests. uv_sync runs `uv sync --locked --extra dev --extra api` — if
# uv.lock is out of sync with pyproject.toml, --locked fails the step.
# npm_ci runs `npm ci` against package-lock.json — if the lockfile is out of
# sync with package.json, npm ci fails. A final sync_drift step re-asserts
# the worktree is still clean afterwards (catches a stale lockfile that a
# non-locked sync would silently update).
step_uv_sync() {
  ( cd "$root" && uv sync --locked --extra dev --extra api )
}

step_npm_ci() {
  ( cd "$root" && npm ci )
}

step_mypy() {
  ( cd "$root" && uv run --extra dev mypy src )
}

step_pytest() {
  ( cd "$root" && uv run --extra dev pytest -q )
}

step_typecheck() {
  ( cd "$root" && npm run typecheck )
}

step_vitest() {
  ( cd "$root" && npm test -- --run )
}

step_playwright() {
  # Playwright UI suite. On a fresh checkout browsers must be installed first:
  #   npx playwright install
  # (with --with-deps on CI hosts that lack browser shared libraries).
  ( cd "$root" && npm run test:ui )
}

step_build() {
  ( cd "$root" && npm run build )
}

step_static_drift() {
  # The committed src/pilot107/web/static build must match the sources.
  if ! git -C "$root" diff --exit-code -- src/pilot107/web/static >/dev/null 2>&1; then
    git -C "$root" --no-pager diff -- src/pilot107/web/static >&2 || true
    echo "static drift detected in src/pilot107/web/static; run npm run build and commit" >&2
    return 1
  fi
}

step_compose_config() {
  ( cd "$root" && sh simulator/compose/scripts/check-compose-config.sh )
}

step_sync_drift() {
  # P2-1 (round 4): re-assert the locked sync (uv_sync + npm_ci) did not
  # modify tracked files. `--locked` would fail if the lockfile is stale, but
  # a stale lockfile that a non-locked sync silently updates would surface as
  # a tracked diff here. Fail closed.
  if ! git -C "$root" diff --quiet || ! git -C "$root" diff --cached --quiet; then
    echo "source acceptance modified tracked files during sync — lockfile may be out of date" >&2
    git -C "$root" --no-pager diff --stat >&2 || true
    return 1
  fi
}

STEPS=(
  "uv_sync|step_uv_sync"
  "npm_ci|step_npm_ci"
  "ruff|step_ruff"
  "mypy|step_mypy"
  "pytest|step_pytest"
  "typecheck|step_typecheck"
  "vitest|step_vitest"
  "playwright|step_playwright"
  "build|step_build"
  "static_drift|step_static_drift"
  "compose_config|step_compose_config"
  "sync_drift|step_sync_drift"
)

for spec in "${STEPS[@]}"; do
  name="${spec%%|*}"
  fn="${spec#*|}"
  run_step "$name" "$fn"
done

# JSON report.
# P1-3 (round 6): seal_mode is passed as argv[6] so the report Python can
# compute any_fail with the seal-mode-aware formula and emit seal_mode /
# overall_status / process_exit_code fields that agree with the Bash
# aggregation's overall_rc.
# P1-2 (round 7): the failure formula is UNIFIED with the Bash aggregation.
# Any step whose status is not in {PASS, FAIL, KNOWN_SKIP} (this includes the
# initial "MISSING" sentinel for a never-written status file, an empty value,
# or an unknown token) counts as failed — exactly mirroring the Bash guards
# that fail on missing status file / empty status / unknown status. The Bash
# aggregation now READS process_exit_code from this JSON and exits with it,
# so JSON and Bash are consistent by construction (see aggregation block
# below).
python3 - "$artifact_dir" "$steps_dir" "$revision" "$started_iso" "$untracked_status" "$seal_mode" "${STEPS[@]}" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

artifact_dir = Path(sys.argv[1])
steps_dir = Path(sys.argv[2])
revision = sys.argv[3]
started_iso = sys.argv[4]
untracked_status = sys.argv[5]
seal_mode = sys.argv[6] == "1"
step_specs = sys.argv[7:]

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

# P1-2 (round 7): the single source of truth for "is this step failed".
# Matches the Bash aggregation: missing/empty/unknown status → failed,
# explicit FAIL → failed, KNOWN_SKIP → failed only in seal mode.
VALID_STATUSES = {"PASS", "FAIL", "KNOWN_SKIP"}


def step_failed(status: str) -> bool:
    return (
        status not in VALID_STATUSES
        or status == "FAIL"
        or (seal_mode and status == "KNOWN_SKIP")
    )


steps = []
for spec in step_specs:
    name = spec.split("|", 1)[0]
    status_file = steps_dir / f"{name}.status"
    log_file = steps_dir / f"{name}.log"
    log_rel = f"steps/{name}.log"
    entry = {
        "name": name,
        "status": "MISSING",
        "start": None,
        "end": None,
        "rc": None,
        "log_path": log_rel,
        "log_sha256": hashlib.sha256(log_file.read_bytes()).hexdigest() if log_file.is_file() else EMPTY_SHA256,
    }
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
    steps.append(entry)

# P1-2 (round 7): unified failure formula. MISSING / empty / unknown status
# values count as failed, matching the Bash aggregation's missing-file and
# unknown-status guards. This closes the prior gap where a step with
# status="MISSING" left any_fail=false while Bash exited 1.
any_fail = any(step_failed(s["status"]) for s in steps)
overall_status = "PASS" if not any_fail else "FAIL"
process_exit_code = 0 if not any_fail else 1

report = {
    "profile": "source",
    "release_revision": revision,
    "started_at": started_iso,
    "ended_at": max((s["end"] or "" for s in steps), default=""),
    "seal_mode": seal_mode,
    "overall_status": overall_status,
    "process_exit_code": process_exit_code,
    "any_fail": any_fail,
    "untracked_files_status": untracked_status,
    "steps": steps,
}
(artifact_dir / "source-acceptance-report.json").write_text(
    json.dumps(report, indent=2, ensure_ascii=False) + "\n"
)
print("=== SOURCE ACCEPTANCE REPORT ===")
for s in steps:
    print(f"  {s['name']}: {s['status']}")
print(f"report: {artifact_dir / 'source-acceptance-report.json'}")
PY

# Final aggregation.
# P1-2 (round 7): the report Python is the single source of truth for the
# process exit code. The Bash aggregation no longer re-derives overall_rc
# from per-step status files; instead it READS process_exit_code from the
# written JSON and exits with it. This makes JSON and Bash consistent by
# construction — the unified failure formula (missing/empty/unknown status →
# failed, FAIL → failed, KNOWN_SKIP → failed in seal mode) lives in exactly
# one place (the report Python above).
#
# Defensive fallback: if the report JSON is missing or unparseable, the
# acceptance produced no trustworthy evidence — fail closed with exit 1 and a
# clear message.
report_json="$artifact_dir/source-acceptance-report.json"
overall_rc=1
if [[ ! -f "$report_json" ]]; then
  log "=== AGGREGATION: $report_json missing → FAIL (fail-closed) ===" >&2
elif ! python3 -c "import json, sys; json.load(open(sys.argv[1]))" "$report_json" >/dev/null 2>&1; then
  log "=== AGGREGATION: $report_json unparseable → FAIL (fail-closed) ===" >&2
else
  # Read the authoritative process_exit_code from the report JSON.
  overall_rc="$(python3 -c "import json, sys; print(int(json.load(open(sys.argv[1])).get('process_exit_code', 1)))" "$report_json" 2>/dev/null || echo 1)"
fi

if [[ "$overall_rc" -ne 0 ]]; then
  log "=== SOURCE ACCEPTANCE FAILED (see $artifact_dir/source-acceptance-report.json) ==="
else
  log "=== SOURCE ACCEPTANCE PASSED (sha=$short_revision) ==="
fi
exit "$overall_rc"
