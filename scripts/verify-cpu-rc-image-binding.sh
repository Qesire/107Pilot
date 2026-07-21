#!/usr/bin/env bash
#
# verify-cpu-rc-image-binding.sh — verify running cpu-rc containers' image IDs
# match RELEASE_MANIFEST.json content_digests.
#
# This is the shared helper for P1-4 (Revision ↔ running image digest binding).
# scripts/check-cpu-rc.sh and scripts/accept-runtime-bundle.sh both call this
# so the binding logic lives in exactly one place.
#
# For each running container in the cpu-rc compose stack, the script reads
#   docker inspect --format '{{.Image}}'        → the image ID (sha256:...)
#   docker inspect --format '{{.Config.Image}}' → the image reference (tag)
# and cross-checks the image ID against the RELEASE_MANIFEST.json record whose
# `reference` equals the container's `{{.Config.Image}}`. The comparison
# normalizes both sides to bare lower-case hex (stripping any `sha256:` prefix)
# so a manifest content_digest `sha256:abc...` matches an inspect image id
# `sha256:abc...` (and tolerates a missing-prefix on either side).
#
# Fail-closed semantics (P1-3): on the "we ARE checking" path, every anomaly
# is a binding FAILURE (all_match=false, exit 1):
#   - `docker compose ps` returns non-zero        → FAIL
#   - a line of compose ps output can't be parsed → FAIL (parse_errors)
#   - zero running containers found               → FAIL (not "nothing to bind")
#   - any of the 10 expected services missing     → FAIL (missing_services)
#   - a service's container exists but isn't      → FAIL
#     State.Running=true
#   - running image_id not in manifest            → FAIL
# The only legitimate skips are the explicit env flag
# PILOT107_SKIP_IMAGE_BINDING_CHECK=1 and manifest-absent (deployment context).
#
# Output (stdout): a single JSON object:
#   {
#     "manifest_path": "..." or null,
#     "release_revision_full": "<40-char SHA or ''>",
#     "release_revision_short": "<12-char SHA or ''>",
#     "running_images": [ {container, service, config_image, image_id,
#                          manifest_content_digest, matches_manifest} ],
#     "all_match": true|false,
#     "skipped": true|false,
#     "skip_reason": "..." or null,
#     "error": "..." or null,            # P1-3: top-level error reason
#     "missing_services": [...],          # P1-3: expected services not running
#     "parse_errors": [...],              # P1-3: unparseable compose ps lines
#     "service_instance_counts": {...},   # P2-1: service→instance count
#     "duplicate_services": [...]         # P2-1: services with >1 instance
#   }
#
# Exit codes:
#   0  — check skipped (PILOT107_SKIP_IMAGE_BINDING_CHECK=1 OR manifest absent).
#   1  — any binding failure (compose ps failed, parse error, zero containers,
#        missing service, container not Running, image mismatch).
#
# Env knobs:
#   PILOT107_RELEASE_MANIFEST_PATH    path to RELEASE_MANIFEST.json. If unset,
#                                     defaults to <repo-root>/RELEASE_MANIFEST.json
#                                     (which is the bundle root when run inside
#                                     an extracted bundle).
#   PILOT107_CPU_RC_ENV_FILE          compose env file
#                                     (default: simulator/compose/.env.cpu-rc).
#   PILOT107_CPU_RC_PROJECT_NAME      compose project name
#                                     (default: pilot107-cpu-rc).
#   PILOT107_SKIP_IMAGE_BINDING_CHECK if "1", skip and exit 0 with skipped=true.
set -euo pipefail

# The 10 cpu-rc services (must match accept-runtime-bundle.sh's CPU_RC_SERVICES
# and the compose files). If any is not found running by `docker compose ps`,
# that is a binding failure — see "missing service" path below.
EXPECTED_SERVICES=(
  mariadb
  slurmdbd
  slurmctld
  worker-1
  slurmrestd
  pilot107-command-gateway
  pilot107-api
  pilot107-worker
  pilot107-web
  pilot107-reverse-proxy
)

# ----- skip handling ---------------------------------------------------------
if [[ "${PILOT107_SKIP_IMAGE_BINDING_CHECK:-0}" == "1" ]]; then
  python3 - <<'PY'
import json
print(json.dumps({
    "manifest_path": None,
    "release_revision_full": "",
    "release_revision_short": "",
    "running_images": [],
    "all_match": True,
    "skipped": True,
    "skip_reason": "PILOT107_SKIP_IMAGE_BINDING_CHECK=1",
    "error": None,
    "missing_services": [],
    "parse_errors": [],
    "service_instance_counts": {},
    "duplicate_services": [],
}, ensure_ascii=False))
PY
  exit 0
fi

# ----- manifest path resolution ---------------------------------------------
script_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
manifest="${PILOT107_RELEASE_MANIFEST_PATH:-$script_root/RELEASE_MANIFEST.json}"

if [[ ! -f "$manifest" ]]; then
  echo "WARNING: RELEASE_MANIFEST.json not found at $manifest; skipping image binding check" >&2
  python3 - "$manifest" <<'PY'
import json, sys
print(json.dumps({
    "manifest_path": sys.argv[1],
    "release_revision_full": "",
    "release_revision_short": "",
    "running_images": [],
    "all_match": True,
    "skipped": True,
    "skip_reason": "manifest absent: %s" % sys.argv[1],
    "error": None,
    "missing_services": [],
    "parse_errors": [],
    "service_instance_counts": {},
    "duplicate_services": [],
}, ensure_ascii=False))
PY
  exit 0
fi

# ----- compose context -------------------------------------------------------
compose_dir="$script_root/simulator/compose"
env_file="${PILOT107_CPU_RC_ENV_FILE:-$compose_dir/.env.cpu-rc}"
project_name="${PILOT107_CPU_RC_PROJECT_NAME:-pilot107-cpu-rc}"

# Hand the EXPECTED_SERVICES list to Python by joining on spaces; the service
# names contain no spaces so this is safe.
expected_services_arg="${EXPECTED_SERVICES[*]}"

python3 - "$manifest" "$project_name" "$env_file" "$compose_dir" "$expected_services_arg" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
project_name = sys.argv[2]
env_file = sys.argv[3]
compose_dir = Path(sys.argv[4])
expected_services = sys.argv[5].split()

manifest = json.loads(manifest_path.read_text())
release_revision_full = manifest.get("release_revision", "") or ""
release_revision_short = release_revision_full[:12] if release_revision_full else ""

# Map manifest reference (tag) -> content_digest (image Id, sha256:...).
manifest_refs = {}
for rec in manifest.get("images", []):
    ref = rec.get("reference")
    if ref:
        manifest_refs[ref] = rec.get("content_digest", "") or ""


def norm_digest(d):
    """Normalize a digest to bare lower-case hex (strip optional sha256: prefix)."""
    if not d:
        return ""
    d = d.strip()
    if d.startswith("sha256:"):
        d = d[len("sha256:"):]
    return d.lower()


def emit(report):
    """Print the JSON report on stdout (single document, caller reads it)."""
    print(json.dumps(report, indent=2, ensure_ascii=False))


def fail(report, message):
    """Mark the report as a binding failure, emit it, print a stderr summary,
    and exit 1. `message` is a short human-readable summary line."""
    report["all_match"] = False
    report["skipped"] = False
    report["skip_reason"] = None
    print("FAIL: %s (release_revision=%s)" % (message, release_revision_full), file=sys.stderr)
    emit(report)
    sys.exit(1)


# Top-level report scaffold. Fields are filled in as we go; on any anomaly we
# call fail() which sets all_match=False and exits 1.
report = {
    "manifest_path": str(manifest_path),
    "release_revision_full": release_revision_full,
    "release_revision_short": release_revision_short,
    "running_images": [],
    "all_match": True,
    "skipped": False,
    "skip_reason": None,
    "error": None,
    "missing_services": [],
    "parse_errors": [],
    "service_instance_counts": {},
    "duplicate_services": [],
}

compose_cmd = [
    "docker", "compose",
    "--project-name", project_name,
    "--env-file", env_file,
    "-f", str(compose_dir / "compose.yml"),
    "-f", str(compose_dir / "compose.competition.yml"),
    "-f", str(compose_dir / "compose.cpu-rc.yml"),
    "--profile", "competition",
]

# Enumerate running containers. `docker compose ps --format json` emits one
# JSON object per line (NDJSON) in compose v2.
ps = subprocess.run(
    compose_cmd + ["ps", "--format", "json"],
    capture_output=True, text=True,
)

# P1-3 fix #1: compose ps itself failed — fail-closed. Do NOT attempt to
# parse stdout (which may be empty or partial) when the invocation errored.
if ps.returncode != 0:
    report["error"] = "compose ps failed (rc=%d): %s" % (
        ps.returncode, (ps.stderr or "").strip()[:500],
    )
    fail(report, "docker compose ps failed (rc=%d)" % ps.returncode)

# P1-3 fix #3: JSON parse failure → fail-closed. Record the offending line in
# parse_errors rather than silently dropping it.
containers = []
for line in ps.stdout.splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        containers.append(json.loads(line))
    except json.JSONDecodeError as e:
        report["parse_errors"].append({
            "line": line[:200],
            "error": str(e),
        })

if report["parse_errors"]:
    report["error"] = "compose ps emitted %d unparseable line(s)" % len(report["parse_errors"])
    fail(report, report["error"])

# P1-3 fix #4: zero containers → fail-closed. This is NOT "nothing to bind" —
# if we got here the manifest exists and the check is not skipped, so an empty
# running set means the stack is down, which is a binding failure.
if not containers:
    report["error"] = "no running containers found"
    fail(report, "no running cpu-rc containers found by compose ps")

# Index the found containers by compose service name so we can assert each of
# the 10 expected services is present. P2-1 (round 8): track ALL instances per
# service — a service with >1 running instance is a binding failure because one
# instance may have the correct image while another has a wrong one, and the
# old code let the later container silently overwrite the earlier one.
by_service = {}
service_instances = {}
for c in containers:
    def get(*keys):
        for k in keys:
            if k in c and c[k] not in (None, ""):
                return c[k]
        return ""
    service = get("Service", "service")
    if not service:
        # `Names` may be "pilot107-cpu-rc-api-1"; we can't reliably recover
        # the service from it, so leave blank and let the missing-service
        # check below catch it.
        service = ""
    service_instances.setdefault(service, []).append(c)
    # Keep the first instance as the representative (for the image check).
    # The duplicate-instance check below runs before the image check, so if
    # there are duplicates we fail before ever reading by_service[service].
    if service not in by_service:
        by_service[service] = c

# P2-1 (round 8): record the instance counts in the report for evidence.
report["service_instance_counts"] = {
    s: len(insts) for s, insts in service_instances.items()
}

# P2-1 (round 8): fail-closed if any expected service has >1 running instance.
# These are single-instance services in the CPU-RC topology; a duplicate means
# an unexpected scale event where one instance may escape image verification.
duplicate = [s for s in expected_services if len(service_instances.get(s, [])) > 1]
if duplicate:
    report["duplicate_services"] = duplicate
    report["error"] = "duplicate service instances: %s" % ", ".join(
        "%s(%d)" % (s, len(service_instances[s])) for s in duplicate
    )
    for s in duplicate:
        print(
            "FAIL: service %s has %d running instances; expected exactly 1 (release_revision=%s)"
            % (s, len(service_instances[s]), release_revision_full),
            file=sys.stderr,
        )
    fail(report, report["error"])

# P1-3 fix #2: assert all 10 expected services are running.
missing = [s for s in expected_services if s not in by_service]
if missing:
    report["missing_services"] = missing
    report["error"] = "missing %d expected service(s): %s" % (len(missing), ", ".join(missing))
    for s in missing:
        print(
            "FAIL: expected service %s is not running (release_revision=%s)"
            % (s, release_revision_full),
            file=sys.stderr,
        )
    fail(report, report["error"])

running_images = []
all_match = True

for service in expected_services:
    c = by_service[service]

    def get(*keys):
        for k in keys:
            if k in c and c[k] not in (None, ""):
                return c[k]
        return ""
    name = get("Name", "name", "Container")
    cid = get("ID", "Id", "id", "ContainerID", "ContainerId")
    if not cid:
        # Fall back to the container name; docker inspect accepts names too.
        cid = name
    if not cid:
        # Should not happen — compose ps gave us a row with no usable id.
        all_match = False
        running_images.append({
            "container": name or "",
            "service": service,
            "config_image": "",
            "image_id": "",
            "manifest_content_digest": "",
            "matches_manifest": False,
        })
        print(
            "FAIL: service %s has no usable container id in compose ps output (release_revision=%s)"
            % (service, release_revision_full),
            file=sys.stderr,
        )
        continue

    # P1-3 fix #5: each service must be Running. If the container exists but
    # isn't Running, that's a binding failure (we can't trust .Image on a
    # stopped container). compose ps's State field is authoritative for the
    # row; we additionally re-assert via docker inspect so a race between ps
    # and inspect (container stopped in between) is also caught.
    ps_state = get("State", "state")
    if ps_state and str(ps_state).lower() != "running":
        all_match = False
        running_images.append({
            "container": name or cid,
            "service": service,
            "config_image": "",
            "image_id": "",
            "manifest_content_digest": "",
            "matches_manifest": False,
        })
        print(
            "FAIL: service %s container %s is not Running (state=%s; release_revision=%s)"
            % (service, name or cid, ps_state, release_revision_full),
            file=sys.stderr,
        )
        continue

    inspect_running = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", cid],
        capture_output=True, text=True,
    )
    if inspect_running.returncode != 0 or inspect_running.stdout.strip() != "true":
        all_match = False
        running_images.append({
            "container": name or cid,
            "service": service,
            "config_image": "",
            "image_id": "",
            "manifest_content_digest": "",
            "matches_manifest": False,
        })
        print(
            "FAIL: service %s container %s State.Running is not true (release_revision=%s)"
            % (service, name or cid, release_revision_full),
            file=sys.stderr,
        )
        continue

    # Image ID (sha256:...) and the image reference (tag) the container was
    # started with.
    image_id = subprocess.run(
        ["docker", "inspect", "--format", "{{.Image}}", cid],
        capture_output=True, text=True,
    ).stdout.strip()
    config_image = subprocess.run(
        ["docker", "inspect", "--format", "{{.Config.Image}}", cid],
        capture_output=True, text=True,
    ).stdout.strip()

    # Primary match: container's config_image tag must equal a manifest
    # reference, and the running image_id must equal that record's
    # content_digest.
    expected_digest = manifest_refs.get(config_image, "")
    matches = bool(expected_digest) and norm_digest(image_id) == norm_digest(expected_digest)
    manifest_digest_for_record = expected_digest

    # Secondary match: if the tag doesn't match any manifest reference but
    # the running image_id IS one of the manifest content_digests (e.g. the
    # tag was re-pointed but the same image is still running), still count
    # it as a match but surface the tag in the record so callers can see the
    # discrepancy.
    if not matches:
        for ref, dg in manifest_refs.items():
            if dg and norm_digest(dg) == norm_digest(image_id):
                matches = True
                manifest_digest_for_record = dg
                break

    # P1-3 fix #6: every running image id must exist in the manifest. Any
    # mismatch → all_match=false → exit 1.
    if not matches:
        all_match = False
        print(
            "FAIL: container %s running image %s not in RELEASE_MANIFEST.json (release_revision=%s)"
            % (name or cid, image_id, release_revision_full),
            file=sys.stderr,
        )

    running_images.append({
        "container": name or cid,
        "service": service,
        "config_image": config_image,
        "image_id": image_id,
        "manifest_content_digest": manifest_digest_for_record,
        "matches_manifest": matches,
    })

report["running_images"] = running_images
report["all_match"] = all_match
emit(report)

# Human-readable summary to stderr (stdout is reserved for the JSON document).
if all_match:
    print("all running containers match RELEASE_MANIFEST.json release_revision=%s" % release_revision_full, file=sys.stderr)
# Per-container FAIL lines were already printed above as each mismatch was
# discovered; no need to reprint them here.

sys.exit(0 if all_match else 1)
PY
