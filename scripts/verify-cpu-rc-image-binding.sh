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
# Output (stdout): a single JSON object:
#   {
#     "manifest_path": "...",
#     "release_revision_full": "<40-char SHA or ''>",
#     "release_revision_short": "<12-char SHA or ''>",
#     "running_images": [
#       {
#         "container": "<name>",
#         "service": "<compose service>",
#         "config_image": "<tag the container was started with>",
#         "image_id": "sha256:...",
#         "manifest_content_digest": "sha256:..." or "",
#         "matches_manifest": true|false
#       }
#     ],
#     "all_match": true|false,
#     "skipped": true|false,
#     "skip_reason": "..." or null
#   }
#
# Exit codes:
#   0  — all running images match manifest, OR check skipped (manifest absent
#        or PILOT107_SKIP_IMAGE_BINDING_CHECK=1).
#   1  — at least one running image does NOT match the manifest. A clear
#        per-container FAIL message is printed to stderr.
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
}, ensure_ascii=False))
PY
  exit 0
fi

# ----- compose context -------------------------------------------------------
compose_dir="$script_root/simulator/compose"
env_file="${PILOT107_CPU_RC_ENV_FILE:-$compose_dir/.env.cpu-rc}"
project_name="${PILOT107_CPU_RC_PROJECT_NAME:-pilot107-cpu-rc}"

python3 - "$manifest" "$project_name" "$env_file" "$compose_dir" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
project_name = sys.argv[2]
env_file = sys.argv[3]
compose_dir = Path(sys.argv[4])

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
containers = []
for line in ps.stdout.splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        containers.append(json.loads(line))
    except json.JSONDecodeError:
        pass

running_images = []
all_match = True

for c in containers:
    # Tolerate field-name casing differences across compose versions.
    def get(*keys):
        for k in keys:
            if k in c and c[k] not in (None, ""):
                return c[k]
        return ""
    service = get("Service", "service", "Names")
    name = get("Name", "name", "Container")
    cid = get("ID", "Id", "id", "ContainerID", "ContainerId")
    if not cid:
        # Fall back to the container name; docker inspect accepts names too.
        cid = name
    if not cid:
        continue
    state = get("State", "state")
    # Skip exited/dead containers — only running ones can bind to a digest.
    if state and str(state).lower() not in ("running", "healthy", "restarting"):
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

    if not matches:
        all_match = False

    running_images.append({
        "container": name or cid,
        "service": service,
        "config_image": config_image,
        "image_id": image_id,
        "manifest_content_digest": manifest_digest_for_record,
        "matches_manifest": matches,
    })

report = {
    "manifest_path": str(manifest_path),
    "release_revision_full": release_revision_full,
    "release_revision_short": release_revision_short,
    "running_images": running_images,
    "all_match": all_match,
    "skipped": False,
    "skip_reason": None,
}
print(json.dumps(report, indent=2, ensure_ascii=False))

# Human-readable summary to stderr (stdout is reserved for the JSON document).
if not running_images:
    print("WARNING: no running cpu-rc containers found by compose ps; nothing to bind", file=sys.stderr)
elif all_match:
    print("all running containers match RELEASE_MANIFEST.json release_revision=%s" % release_revision_full, file=sys.stderr)
else:
    for r in running_images:
        if not r["matches_manifest"]:
            print(
                "FAIL: container %s running image %s not in RELEASE_MANIFEST.json (release_revision=%s)"
                % (r["container"], r["image_id"], release_revision_full),
                file=sys.stderr,
            )

sys.exit(0 if all_match else 1)
PY
