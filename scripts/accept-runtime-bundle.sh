#!/usr/bin/env bash
#
# accept-runtime-bundle.sh — validate a 107pilot CPU-RC offline runtime bundle.
#
# This script is the offline-bundle half of the封版门 #4 acceptance matrix.
# It proves the bundle carries a self-consistent, integrity-verified runtime
# closure: manifest + digests, fixed images, compose stack, and the cpu-rc
# behavioral smokes. It does NOT re-run source CI — source tests are bound to
# the git SHA by accept-source-release.sh (or GitHub CI) against the source
# tree, not the bundle.
#
# Steps (in order):
#   1. manifest_validate   — RELEASE_MANIFEST.json present + parseable;
#                            SHA256SUMS verified (sha256sum -c); archive
#                            sha256 verified when a .tar.gz is supplied.
#   2. import_images       — docker load bundled image tarball; verify each
#                            loaded image Id equals RELEASE_MANIFEST
#                            content_digest.
#   3. start_stack         — bash scripts/start-cpu-rc.sh (generates local
#                            .env.cpu-rc from the bundled .env.cpu-rc.example).
#   4. compose_readiness   — compose config validation + container health wait.
#   5. check_cpu_rc        — partition assertion + success/fail/cancel + Evidence
#                            + explicit Capsule (bash scripts/check-cpu-rc.sh).
#   6. auto_capsule        — Worker auto-Capsule WITHOUT explicit POST.
#   7. rule_remediation    — rule-evaluated diagnosis → HTTP remediation session.
#   8. restart_recovery    — docker compose down + restart preserves volume state.
#   9. report              — JSON report; exit 1 if any step FAILED.
#
# Exit-code mapping (preserved from Phase 1):
#   rc=0   → PASS
#   rc=77  → KNOWN_SKIP — exit 77 = reserved for explicit capability-probe
#            architectural skip; any other non-zero is a real regression.
#   else   → FAIL  (strict; only FAIL fails the overall release)
#
# Env knobs (with defaults):
#   PILOT107_PUBLIC_URL            REQUIRED (no default). Full public origin.
#   PILOT107_BUNDLE_DIR            If set, the extracted bundle dir to validate.
#   PILOT107_BUNDLE_ARCHIVE        If set, a .tar.gz to verify (.sha256) and
#                                  extract before validating. Used when
#                                  PILOT107_BUNDLE_DIR is unset.
#   PILOT107_SKIP_ORIGIN_VALIDATE  default 0; passed to start-cpu-rc.sh.
#   PILOT107_ACCEPT_ARTIFACT_DIR   default artifacts/acceptance/runtime-<rev>-<ts>
#   PILOT107_ACCEPT_LEAVE_UP       default 0; 1 leaves the stack running on exit.
#   PILOT107_ACCEPT_SEAL_MODE      default 0. When 1, KNOWN_SKIP is treated as
#                                  FAIL in the final aggregation (formal seal
#                                  mode requires all 10 runtime steps to PASS).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -z "${PILOT107_PUBLIC_URL:-}" ]]; then
  echo "PILOT107_PUBLIC_URL is unset or empty. Set it to the full public origin" >&2
  echo "the browser uses to reach the deployment, e.g. https://pilot.example.edu:8443" >&2
  exit 1
fi

# Resolve the bundle directory: either an explicit dir, or extract an archive.
bundle_dir=""
extracted_tmp=""
if [[ -n "${PILOT107_BUNDLE_DIR:-}" ]]; then
  bundle_dir="$PILOT107_BUNDLE_DIR"
elif [[ -n "${PILOT107_BUNDLE_ARCHIVE:-}" ]]; then
  archive="$PILOT107_BUNDLE_ARCHIVE"
  if [[ ! -f "$archive" ]]; then
    echo "PILOT107_BUNDLE_ARCHIVE=$archive not found" >&2
    exit 1
  fi
  sha_file="${archive}.sha256"
  if [[ -f "$sha_file" ]]; then
    ( cd "$(dirname "$archive")" && sha256sum -c "$(basename "$sha_file")" )
  else
    echo "WARNING: $sha_file missing; skipping archive digest check" >&2
  fi
  extracted_tmp="$(mktemp -d)"
  tar -xzf "$archive" -C "$extracted_tmp"
  # The archive contains a single top-level bundle dir.
  bundle_dir="$(find "$extracted_tmp" -mindepth 1 -maxdepth 1 -type d | head -n1)"
  if [[ -z "$bundle_dir" ]]; then
    echo "extracted archive $archive has no top-level dir" >&2
    exit 1
  fi
else
  echo "set PILOT107_BUNDLE_DIR (extracted bundle) or PILOT107_BUNDLE_ARCHIVE (.tar.gz)" >&2
  exit 1
fi

if [[ ! -d "$bundle_dir" ]]; then
  echo "bundle dir not found: $bundle_dir" >&2
  exit 1
fi

# P1-2: derive the release revision short SHA from the bundle's manifest so we
# can build an isolated compose project name. Computing it here (rather than
# only inside step_report's Python) lets every compose invocation in this
# script and in the child scripts target a project name that is unique to
# this acceptance run, so it never collides with an ambient
# `pilot107-cpu-rc` deployment and `down -v` fully removes its volumes.
release_revision_full="$(python3 - "$bundle_dir/RELEASE_MANIFEST.json" <<'PY'
import json, sys
try:
    m = json.loads(open(sys.argv[1]).read())
    print(m.get("release_revision", "") or "")
except Exception:
    pass
PY
)"
release_revision_short="${release_revision_full:0:12}"
# Unique-per-process id (epoch seconds + PID) so concurrent acceptance runs of
# the same revision don't collide on project name either.
acceptance_id="$(date +%s)-$$"
export PILOT107_CPU_RC_PROJECT_NAME="pilot107-cpu-rc-accept-${release_revision_short}-${acceptance_id}"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
artifact_dir="${PILOT107_ACCEPT_ARTIFACT_DIR:-$root/artifacts/acceptance/runtime-$timestamp}"
mkdir -p "$artifact_dir"
steps_dir="$artifact_dir/steps"
mkdir -p "$steps_dir"
started_iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Export knobs consumed by child scripts. Images are imported from the bundle
# in step_import_images, so start-cpu-rc.sh must NOT rebuild them.
# PILOT107_CPU_RC_PROJECT_NAME is already exported above (P1-2 isolated
# project). All child scripts (start-cpu-rc.sh, check-cpu-rc.sh, the smoke
# wrappers, stop-cpu-rc.sh, verify-cpu-rc-image-binding.sh, and the Python
# smokes) honor this env var with a `pilot107-cpu-rc` default.
export PILOT107_PUBLIC_URL
export PILOT107_SKIP_BUILD=1
export PILOT107_SKIP_ORIGIN_VALIDATE="${PILOT107_SKIP_ORIGIN_VALIDATE:-0}"
export PILOT107_SMOKE_PARTITION="${PILOT107_SMOKE_PARTITION:-CPU-RC}"
export PILOT107_SMOKE_QOS="${PILOT107_SMOKE_QOS:-qos_cpu_rc}"
export PILOT107_COMPETITION_BASE_URL="${PILOT107_COMPETITION_BASE_URL:-${PILOT107_PUBLIC_URL%/}/api/v1}"

log() { printf '%s\n' "$*"; }

# Exit-code → status mapping. KNOWN_SKIP_STEPS is intentionally empty: every
# step must PASS or FAIL. A step may still signal an architectural skip by
# exiting 77 (probe-gated); run_step maps rc==77 to KNOWN_SKIP below.
#
# exit 77 = reserved for explicit capability-probe architectural skip; any
# other non-zero is a real regression.
KNOWN_SKIP_STEPS=""

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
    log "=== STEP: $name KNOWN_SKIP (rc=$rc; probe-gated architectural skip) ==="
  else
    printf 'start=%s\nend=%s\nstatus=FAIL\nrc=%s\n' "$start_ts" "$end_ts" "$rc" >"$status_file"
    log "=== STEP: $name FAIL (rc=$rc) ==="
  fi
  return 0
}

# Resolve the bundle-relative root for child scripts. The bundle ships scripts/
# at $bundle_dir/scripts and the compose tree at $bundle_dir/simulator/compose.
# start-cpu-rc.sh and the smokes compute their own `root` from BASH_SOURCE, so
# we run them with BASH_SOURCE pointing inside $bundle_dir by cd-ing there.
bundle_root="$bundle_dir"

step_manifest_validate() {
  local manifest="$bundle_dir/RELEASE_MANIFEST.json"
  local sums="$bundle_dir/SHA256SUMS"
  if [[ ! -f "$manifest" ]]; then
    echo "missing RELEASE_MANIFEST.json in $bundle_dir" >&2
    return 1
  fi
  if [[ ! -f "$sums" ]]; then
    echo "missing SHA256SUMS in $bundle_dir" >&2
    return 1
  fi
  # Parse manifest (must be valid JSON with release_revision). The heredoc
  # body MUST be at column 0: `<<'PY'` (no dash) does not strip indentation,
  # so indented Python would raise IndentationError.
  if ! python3 - "$manifest" <<'PY'; then
import json, sys
m = json.loads(open(sys.argv[1]).read())
if not m.get("release_revision"):
    print("RELEASE_MANIFEST.release_revision missing", file=sys.stderr)
    sys.exit(1)
print("release_revision:", m["release_revision"])
PY
    return 1
  fi
  # Verify every file listed in SHA256SUMS is present and unmodified.
  ( cd "$bundle_dir" && sha256sum -c SHA256SUMS >/dev/null )
}

step_import_images() {
  local images_tar="$bundle_dir/images/pilot107-cpu-rc-images.tar.gz"
  local images_txt="$bundle_dir/images/images.txt"
  if [[ ! -f "$images_tar" || ! -f "$images_txt" ]]; then
    echo "bundle missing images/pilot107-cpu-rc-images.tar.gz or images/images.txt" >&2
    return 1
  fi
  docker load -i "$images_tar" >/dev/null
  # Verify each loaded image Id matches RELEASE_MANIFEST content_digest, AND
  # that images.txt and the manifest agree as sets (no extra un-manifested
  # image in images.txt, no manifest ref missing from images.txt).
  python3 - "$bundle_dir/RELEASE_MANIFEST.json" "$images_txt" <<'PY'
import json, subprocess, sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text())
release_revision = manifest.get("release_revision", "")
expected = {rec["reference"]: rec["content_digest"] for rec in manifest.get("images", [])}
loaded = [ln.strip() for ln in Path(sys.argv[2]).read_text().splitlines() if ln.strip()]
loaded_set = set(loaded)
expected_refs = set(expected)

# manifest ⊆ loaded: every manifest ref must appear in images.txt.
missing = [r for r in expected if r not in loaded_set]
if missing:
    print("images.txt missing entries for: %s" % missing, file=sys.stderr)
    sys.exit(1)
# loaded ⊆ manifest: no extra un-manifested ref in images.txt.
extra = [r for r in loaded if r not in expected_refs]
if extra:
    for r in extra:
        print("images.txt contains reference %s not in RELEASE_MANIFEST.json (release_revision=%s)" % (r, release_revision), file=sys.stderr)
    sys.exit(1)
for ref in expected:
    payload = json.loads(subprocess.check_output(["docker", "image", "inspect", ref]))[0]
    if payload["Id"] != expected[ref]:
        print("digest mismatch for %s: got %s, manifest says %s" % (ref, payload["Id"], expected[ref]), file=sys.stderr)
        sys.exit(1)
    print("ok   %s (%s)" % (ref, payload["Id"]))
PY
}

# Full set of cpu-rc compose services that must be running for the stack to be
# considered "up". Used by stack_is_healthy below.
CPU_RC_SERVICES=(
  mariadb slurmdbd slurmctld worker-1 slurmrestd
  pilot107-command-gateway pilot107-api pilot107-worker
  pilot107-web pilot107-reverse-proxy
)

# Return 0 if every service in CPU_RC_SERVICES is running AND the API readiness
# endpoint answers; return 1 otherwise. Used to make step_start_stack
# idempotent: a genuinely-healthy stack is reused instead of re-running
# start-cpu-rc.sh (which races on slurmdbd->mariadb auth when its .env.cpu-rc
# is regenerated against a pre-existing volume).
#
# P1-2: on a freshly-extracted bundle there is NO .env.cpu-rc (the export
# script ships only .env.cpu-rc.example). In that case the stack cannot
# already be up under our isolated project name, so return 1 immediately —
# step_start_stack will run start-cpu-rc.sh, which generates a fresh
# .env.cpu-rc + fresh volumes (isolated project name → fresh volumes, so the
# credential-mismatch race is impossible).
stack_is_healthy() {
  local compose_dir="$bundle_root/simulator/compose"
  local env_file="${PILOT107_CPU_RC_ENV_FILE:-$compose_dir/.env.cpu-rc}"
  local project_name="${PILOT107_CPU_RC_PROJECT_NAME:-pilot107-cpu-rc}"
  # No env file → stack was never started under this project name.
  if [[ ! -f "$env_file" ]]; then
    return 1
  fi
  local compose=(
    docker compose
    --project-name "$project_name"
    --env-file "$env_file"
    -f "$compose_dir/compose.yml"
    -f "$compose_dir/compose.competition.yml"
    -f "$compose_dir/compose.cpu-rc.yml"
    --profile competition
  )
  local svc
  for svc in "${CPU_RC_SERVICES[@]}"; do
    local id
    id="$("${compose[@]}" ps --all -q "$svc" 2>/dev/null | head -n1)"
    [[ -z "$id" ]] && return 1
    [[ "$(docker inspect --format '{{.State.Running}}' "$id" 2>/dev/null || true)" == "true" ]] || return 1
  done
  # Slurmdbd must actually accept connections (not just be Running — it can be
  # stuck retrying mariadb auth). If sacctmgr can't reach it, the stack is not
  # healthy regardless of container state.
  if ! "${compose[@]}" exec -T slurmdbd sacctmgr -n list cluster >/dev/null 2>&1; then
    return 1
  fi
  return 0
}

step_start_stack() {
  if stack_is_healthy; then
    log "  cpu-rc stack already up and healthy; skipping start-cpu-rc.sh"
    return 0
  fi
  ( cd "$bundle_root" && bash scripts/start-cpu-rc.sh )
}

step_compose_readiness() {
  # Compose config validation + health wait. start-cpu-rc.sh already waits for
  # container health before returning, so here we re-assert the compose config
  # renders and that the public health endpoint answers.
  local compose_dir="$bundle_root/simulator/compose"
  local env_file="${PILOT107_CPU_RC_ENV_FILE:-$compose_dir/.env.cpu-rc}"
  local project_name="${PILOT107_CPU_RC_PROJECT_NAME:-pilot107-cpu-rc}"
  docker compose \
    --project-name "$project_name" \
    --env-file "$env_file" \
    -f "$compose_dir/compose.yml" \
    -f "$compose_dir/compose.competition.yml" \
    -f "$compose_dir/compose.cpu-rc.yml" \
    --profile competition \
    config >/dev/null
  # Health probe: the API readiness endpoint must answer within ~60s.
  # Strict: require HTTP 200 AND a JSON body with status == "ready"
  # (matches src/pilot107/api/health.py: ready_payload is
  # {"status": "ready" | "not_ready", "checks": [...]}).
  https_port="$(awk -F= '/^PILOT107_HTTPS_PORT=/{print $2}' "$env_file" | tail -1)"
  : "${https_port:=8443}"
  python3 - "$https_port" <<'PY'
import json, ssl, sys, time, urllib.request
port = sys.argv[1]
url = "https://127.0.0.1:%s/api/v1/health/ready" % port
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
deadline = time.time() + 60
last = None
while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=5, context=ctx) as r:
            if r.status == 200:
                body = r.read().decode("utf-8", errors="replace")
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError as exc:
                    last = "non-JSON body: %s" % exc
                    time.sleep(2)
                    continue
                if isinstance(payload, dict) and payload.get("status") == "ready":
                    sys.exit(0)
                last = "status not ready: %r" % payload.get("status")
            else:
                last = "http status %s" % r.status
    except Exception as e:
        last = e
        time.sleep(2)
print("API readiness never answered ready: %s" % last, file=sys.stderr)
sys.exit(1)
PY
}

step_check_cpu_rc() {
  ( cd "$bundle_root" && bash scripts/check-cpu-rc.sh )
}

step_auto_capsule() {
  ( cd "$bundle_root" && bash scripts/smoke-auto-capsule.sh )
}

step_rule_remediation() {
  ( cd "$bundle_root" && bash scripts/smoke-cpu-rc-remediation.sh )
}

step_restart_recovery() {
  ( cd "$bundle_root" && bash scripts/smoke-restart-volume-recovery.sh )
}

# P1-4: post-smoke binding check. Even if every smoke step passed, the
# acceptance is invalid if a running container's image ID does not match the
# RELEASE_MANIFEST.json content_digest for its reference — that means the
# bundle's "report SHA" no longer describes what is actually running. This
# step writes its JSON output to $steps_dir/image_binding.json so step_report
# can embed it in the final report. The step FAILs (rc=1) on any mismatch,
# which propagates through run_step → overall_rc=1.
step_image_binding() {
  local out="$steps_dir/image_binding.json"
  local compose_dir="$bundle_root/simulator/compose"
  local env_file="${PILOT107_CPU_RC_ENV_FILE:-$compose_dir/.env.cpu-rc}"
  local project_name="${PILOT107_CPU_RC_PROJECT_NAME:-pilot107-cpu-rc}"
  # The helper resolves its own compose context from these env vars + the
  # script-relative compose_dir. Run from $bundle_root so its script_root is
  # the bundle, and point it at the bundle's RELEASE_MANIFEST.json.
  ( cd "$bundle_root" \
    && PILOT107_RELEASE_MANIFEST_PATH="$bundle_dir/RELEASE_MANIFEST.json" \
       PILOT107_CPU_RC_ENV_FILE="$env_file" \
       PILOT107_CPU_RC_PROJECT_NAME="$project_name" \
       bash scripts/verify-cpu-rc-image-binding.sh >"$out" )
}

STEPS=(
  "manifest_validate|step_manifest_validate"
  "import_images|step_import_images"
  "start_stack|step_start_stack"
  "compose_readiness|step_compose_readiness"
  "check_cpu_rc|step_check_cpu_rc"
  "auto_capsule|step_auto_capsule"
  "rule_remediation|step_rule_remediation"
  "restart_recovery|step_restart_recovery"
  "image_binding|step_image_binding"
  "report|step_report"
)

cleanup() {
  # P1-2: cleanup order MUST be stop-stack-then-remove-extracted-dir. The
  # prior code did `rm -rf "$extracted_tmp"` FIRST, then tried to `cd
  # "$bundle_root" && bash scripts/stop-cpu-rc.sh` — but in archive mode
  # $bundle_root IS $extracted_tmp, so cd failed (dir already gone) and stop
  # was swallowed by `|| true`, leaving the stack (and its named volumes)
  # running. stop-cpu-rc.sh does plain `down` (no -v), so we also run an
  # explicit `down -v` with the isolated project name to fully remove the
  # acceptance run's volumes.
  if [[ "${PILOT107_ACCEPT_LEAVE_UP:-0}" != "1" ]]; then
    log "=== CLEANUP: stopping cpu-rc stack (best-effort, with -v) ==="
    if [[ -d "$bundle_root" ]]; then
      ( cd "$bundle_root" && bash scripts/stop-cpu-rc.sh ) \
        && log "  stop-cpu-rc.sh ok" \
        || log "  stop-cpu-rc.sh failed (continuing)" >&2
      # Belt-and-suspenders: stop-cpu-rc.sh does plain `down`; explicitly
      # remove volumes for the isolated project so repeated acceptance runs
      # don't accumulate orphan volumes.
      local compose_dir="$bundle_root/simulator/compose"
      local env_file="${PILOT107_CPU_RC_ENV_FILE:-$compose_dir/.env.cpu-rc}"
      if [[ -f "$env_file" ]]; then
        docker compose \
          --project-name "$PILOT107_CPU_RC_PROJECT_NAME" \
          --env-file "$env_file" \
          -f "$compose_dir/compose.yml" \
          -f "$compose_dir/compose.competition.yml" \
          -f "$compose_dir/compose.cpu-rc.yml" \
          --profile competition \
          down -v >>"$artifact_dir/cleanup-down-v.log" 2>&1 \
          && log "  down -v ok" \
          || log "  down -v failed (continuing)" >&2
      else
        log "  no .env.cpu-rc; skipping down -v (volumes already absent or named elsewhere)" >&2
      fi
    else
      log "  bundle_root gone; cannot run stop-cpu-rc.sh" >&2
    fi
  else
    log "=== CLEANUP: PILOT107_ACCEPT_LEAVE_UP=1; leaving stack running ==="
  fi
  # Remove the extracted bundle dir LAST — stop-cpu-rc.sh and the down -v
  # above need $bundle_root (== $extracted_tmp in archive mode) to exist.
  if [[ -n "$extracted_tmp" && -d "$extracted_tmp" ]]; then
    rm -rf "$extracted_tmp" \
      && log "  removed extracted tmp $extracted_tmp" \
      || log "  failed to remove $extracted_tmp" >&2
  fi
}
trap cleanup EXIT

step_report() {
  local report_start report_end
  report_start="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  python3 - "$artifact_dir" "$steps_dir" "$bundle_dir" "$started_iso" "${STEPS[@]}" <<'PY'
import json
import hashlib
import os
import sys
from pathlib import Path

artifact_dir = Path(sys.argv[1])
steps_dir = Path(sys.argv[2])
bundle_dir = Path(sys.argv[3])
started_iso = sys.argv[4]
step_specs = sys.argv[5:]

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

manifest_path = bundle_dir / "RELEASE_MANIFEST.json"
release_revision = ""
release_revision_full = ""
release_revision_short = ""
image_digests = []
image_binding = None
if manifest_path.is_file():
    try:
        m = json.loads(manifest_path.read_text())
        release_revision = m.get("release_revision", "") or ""
        release_revision_full = release_revision
        release_revision_short = release_revision[:12] if release_revision else ""
        image_digests = [
            {"reference": r.get("reference"), "content_digest": r.get("content_digest")}
            for r in m.get("images", [])
        ]
    except Exception:
        pass

# P1-4: read the image_binding step's JSON output (if present) so the final
# report records exactly which running containers were checked, their image
# IDs, and whether each matched the manifest. If the step ran, its status is
# already reflected in $steps_dir/image_binding.status; here we embed the
# structured detail.
binding_json = steps_dir / "image_binding.json"
if binding_json.is_file():
    try:
        image_binding = json.loads(binding_json.read_text())
    except Exception:
        image_binding = None

steps = []
any_fail = False
for spec in step_specs:
    name = spec.split("|", 1)[0]
    if name == "report":
        # The report step's own log is self-referential (this Python IS the
        # report step's output), so record null log fields rather than read
        # a half-written file.
        steps.append({
            "name": name,
            "status": "PASS",
            "start": None,
            "end": None,
            "rc": None,
            "log_path": None,
            "log_sha256": None,
        })
        continue
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
        if entry["status"] == "FAIL":
            any_fail = True
    steps.append(entry)

# P1-4: if the image_binding step recorded any non-matching running image,
# force overall result FAIL even if every smoke step passed — the running
# images don't match the bundle, so the acceptance is invalid.
image_binding_all_match = True
if image_binding is not None:
    image_binding_all_match = bool(image_binding.get("all_match", True))
    if not image_binding_all_match:
        any_fail = True

report = {
    "profile": "cpu-rc-runtime-bundle",
    "release_revision": release_revision,
    "release_revision_full": release_revision_full,
    "release_revision_short": release_revision_short,
    "bundle_dir": str(bundle_dir),
    "images": image_digests,
    # P1-4: running-image ↔ manifest binding detail. `running_images` lists
    # each running container with its image_id (docker inspect .Image) and
    # whether it matches the manifest's content_digest. `image_binding_all_match`
    # is the rollup; if False, any_fail is forced True above.
    "image_binding_all_match": image_binding_all_match,
    "running_images": (image_binding or {}).get("running_images", []) if image_binding else [],
    "started_at": started_iso,
    "ended_at": max((s["end"] or "" for s in steps), default=""),
    "any_fail": any_fail,
    "steps": steps,
}
(artifact_dir / "runtime-acceptance-report.json").write_text(
    json.dumps(report, indent=2, ensure_ascii=False) + "\n"
)
print("=== RUNTIME BUNDLE ACCEPTANCE REPORT ===")
for s in steps:
    print(f"  {s['name']}: {s['status']}")
print(f"release_revision: {release_revision}")
print(f"release_revision_full: {release_revision_full}")
print(f"release_revision_short: {release_revision_short}")
print(f"image_binding_all_match: {image_binding_all_match}")
print(f"report: {artifact_dir / 'runtime-acceptance-report.json'}")
PY
  report_end="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'start=%s\nend=%s\nstatus=PASS\n' "$report_start" "$report_end" >"$steps_dir/report.status"
}

for spec in "${STEPS[@]}"; do
  name="${spec%%|*}"
  fn="${spec#*|}"
  run_step "$name" "$fn"
done

# Final aggregation. A step is treated as FAIL (overall_rc=1) if:
#   - its status file is missing (run_step never wrote it),
#   - its status value is not one of PASS/FAIL/KNOWN_SKIP (corrupt/unparseable),
#   - or it explicitly recorded status=FAIL.
# The report step is excluded (it writes its own status after the JSON is
# generated). The report JSON itself must exist and be parseable — a missing
# or unparseable report is also a FAIL. The image_binding_all_match flag is
# already forced into any_fail by step_report's Python; here we re-assert the
# step-status + report-existence invariants in bash.
# P2-4 (round 4): in seal mode (PILOT107_ACCEPT_SEAL_MODE=1), KNOWN_SKIP is
# treated as FAIL — formal seal requires all 10 runtime steps to PASS.
# Default (dev) keeps KNOWN_SKIP non-failing.
overall_rc=0
seal_mode="${PILOT107_ACCEPT_SEAL_MODE:-0}"
valid_statuses=' PASS FAIL KNOWN_SKIP '
for spec in "${STEPS[@]}"; do
  name="${spec%%|*}"
  [[ "$name" == "report" ]] && continue
  status_file="$steps_dir/$name.status"
  if [[ ! -f "$status_file" ]]; then
    log "=== AGGREGATION: step $name status file missing → FAIL ===" >&2
    overall_rc=1
    continue
  fi
  status_line="$(grep -m1 '^status=' "$status_file" || true)"
  status_val="${status_line#status=}"
  if [[ -z "$status_val" ]] || [[ " $valid_statuses " != *" $status_val "* ]]; then
    log "=== AGGREGATION: step $name has unknown status '${status_val:-<empty>}' → FAIL ===" >&2
    overall_rc=1
    continue
  fi
  if [[ "$status_val" == "FAIL" ]]; then
    overall_rc=1
  fi
  if [[ "$seal_mode" == "1" && "$status_val" == "KNOWN_SKIP" ]]; then
    log "=== AGGREGATION: step $name KNOWN_SKIP → FAIL (seal mode) ===" >&2
    overall_rc=1
  fi
done

# The report JSON must exist and be parseable. A missing/unparseable report
# means the acceptance produced no trustworthy evidence.
report_json="$artifact_dir/runtime-acceptance-report.json"
if [[ ! -f "$report_json" ]]; then
  log "=== AGGREGATION: $report_json missing → FAIL ===" >&2
  overall_rc=1
elif ! python3 -c "import json, sys; json.load(open(sys.argv[1]))" "$report_json" >/dev/null 2>&1; then
  log "=== AGGREGATION: $report_json unparseable → FAIL ===" >&2
  overall_rc=1
fi

if [[ "$overall_rc" -ne 0 ]]; then
  log "=== RUNTIME BUNDLE ACCEPTANCE FAILED (see $artifact_dir/runtime-acceptance-report.json) ==="
else
  log "=== RUNTIME BUNDLE ACCEPTANCE PASSED ==="
fi
exit "$overall_rc"
