#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
revision="$(git -C "$root" rev-parse HEAD)"
short_revision="${revision:0:12}"
out_root="${PILOT107_BUNDLE_DIR:-$root/artifacts/deployment}"
bundle_name="107pilot-cpu-rc-${short_revision}-${timestamp}"
work_dir="$out_root/$bundle_name"
archive="$out_root/${bundle_name}.tar.gz"

if ! git -C "$root" diff --quiet || ! git -C "$root" diff --cached --quiet; then
  echo "refusing to export a bundle from a dirty tracked worktree" >&2
  exit 1
fi

# P2-4 (round 8): untracked files in the bundle source dirs can leak into the
# exported bundle and escape review. Refuse to export if any untracked files
# exist under src/, apps/, config/, or services/ — the source dirs copied into
# the bundle.
# Untracked files elsewhere (artifacts/, docs/) do not ship in the bundle and
# are tolerated.
untracked_in_bundle="$(git -C "$root" status --porcelain --untracked-files -- src apps config services | grep '^??' || true)"
if [[ -n "$untracked_in_bundle" ]]; then
  echo "refusing to export a bundle with untracked files in src/ apps/ config/ services/:" >&2
  printf '%s\n' "$untracked_in_bundle" >&2
  echo "commit or remove these files before exporting" >&2
  exit 1
fi

mkdir -p "$out_root"
rm -rf "$work_dir"
rm -f "$archive" "$archive.sha256"
mkdir -p "$work_dir" "$work_dir/images" "$work_dir/scripts" "$work_dir/sbom"

if [[ "${PILOT107_SKIP_BUILD:-0}" != "1" ]]; then
  PILOT107_CPU_RC_REVISION="$short_revision" bash "$root/scripts/build-cpu-rc-images.sh"
fi

images=(
  "pilot107/slurm-sim:cpu-rc-$short_revision"
  "pilot107/api:cpu-rc-$short_revision"
  "pilot107/worker:cpu-rc-$short_revision"
  "pilot107/web:cpu-rc-$short_revision"
  "pilot107/agentd:cpu-rc-$short_revision"
)
for image in "${images[@]}"; do
  docker image inspect "$image" >/dev/null
done

cp -a "$root/src" "$work_dir/src"
cp -a "$root/apps" "$work_dir/apps"
cp -a "$root/config" "$work_dir/config"
cp -a "$root/services" "$work_dir/services"
mkdir -p "$work_dir/data" "$work_dir/simulator"
cp -a "$root/data/known_errors" "$work_dir/data/"
cp -a "$root/data/submission_templates" "$work_dir/data/"
# Copy the compose dir. A real copy failure (disk full, missing source) MUST
# fail the export, so we do NOT swallow cp errors here. Some 0600/0700 secret
# files under simulator/compose/{certs,secrets} may emit permission-denied
# warnings; those are expected and get cleaned below — we tolerate the
# warnings but still propagate any non-zero cp status by retrying the copy
# for the non-secret subtree below if the initial copy aborts early.
if ! cp -a "$root/simulator/compose" "$work_dir/simulator/" 2>/tmp/pilot107-export-cp.err; then
  # Retry excluding the secret/cert dirs that may be unreadable; if THIS also
  # fails, it is a real error (disk full, missing source) and we abort.
  if ! rsync -a --exclude 'certs/' --exclude 'secrets/' \
        "$root/simulator/compose/" "$work_dir/simulator/compose/" 2>>/tmp/pilot107-export-cp.err; then
    echo "failed to copy simulator/compose:" >&2
    cat /tmp/pilot107-export-cp.err >&2
    exit 1
  fi
fi
# Clean generated secrets/certs/env that should not be in the bundle.
# (These are recreated by start-cpu-rc.sh on the target.)
rm -f "$work_dir/simulator/compose/.env.cpu-rc"
find "$work_dir/simulator/compose" -maxdepth 1 -type f -name '.env*' ! -name '.env.cpu-rc.example' -delete
rm -rf "$work_dir/simulator/compose/certs" "$work_dir/simulator/compose/secrets"
mkdir -p "$work_dir/simulator/compose/certs" "$work_dir/simulator/compose/secrets"
cp "$root/simulator/compose/certs/README.md" "$work_dir/simulator/compose/certs/" 2>/dev/null || true
cp "$root/simulator/compose/secrets/README.md" "$work_dir/simulator/compose/secrets/" 2>/dev/null || true
rm -f "$work_dir/simulator/compose/slurm/jwt_hs256.key" 2>/dev/null || true
cp -a "$root/docs/operations" "$work_dir/docs-operations"
cp -a "$root/docs/phase-3" "$work_dir/docs-phase-3"
cp "$root/pyproject.toml" "$root/README.md" "$work_dir/"
[[ ! -f "$root/uv.lock" ]] || cp "$root/uv.lock" "$work_dir/"

for script in \
  accept-cpu-rc-release.sh \
  accept-runtime-bundle.sh \
  accept-source-release.sh \
  apply-cpu-rc-profile.sh \
  build-app-images.sh \
  build-cpu-rc-images.sh \
  check-cpu-rc.sh \
  control-plane-recovery.py \
  export-cpu-rc-bundle.sh \
  import-cpu-rc-images.sh \
  init-local-secrets.sh \
  install-systemd-units.sh \
  load_competition.py \
  preflight-cpu-rc-vm.sh \
  scan-array-artifacts.py \
  smoke_auto_capsule.py \
  smoke-auto-capsule.sh \
  smoke-cpu-rc-remediation.sh \
  smoke_cpu_rc_remediation.py \
  smoke-restart-volume-recovery.sh \
  smoke_restart_volume_recovery.py \
  smoke_competition_web.py \
  start-cpu-rc.sh \
  stop-cpu-rc.sh \
  verify-cpu-rc-image-binding.sh
do
  cp "$root/scripts/$script" "$work_dir/scripts/"
done

# systemd unit templates + install doc
cp -a "$root/scripts/systemd" "$work_dir/scripts/systemd"

find "$work_dir" -type d \( -name node_modules -o -name '*.egg-info' \) -prune -exec rm -rf {} +
find "$work_dir" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$work_dir" -type f -name '*.pyc' -delete

env_template="$work_dir/simulator/compose/.env.cpu-rc.example"
sed -i \
  -e "s|^SLURM_SIM_IMAGE=.*|SLURM_SIM_IMAGE=${images[0]}|" \
  -e "s|^PILOT107_API_IMAGE=.*|PILOT107_API_IMAGE=${images[1]}|" \
  -e "s|^PILOT107_WORKER_IMAGE=.*|PILOT107_WORKER_IMAGE=${images[2]}|" \
  -e "s|^PILOT107_WEB_IMAGE=.*|PILOT107_WEB_IMAGE=${images[3]}|" \
  -e "s|^PILOT107_REVERSE_PROXY_IMAGE=.*|PILOT107_REVERSE_PROXY_IMAGE=${images[3]}|" \
  -e "s|^PILOT107_AGENTD_IMAGE=.*|PILOT107_AGENTD_IMAGE=${images[4]}|" \
  "$env_template"

printf '%s\n' "${images[@]}" >"$work_dir/images/images.txt"
docker save "${images[@]}" | gzip -1 >"$work_dir/images/pilot107-cpu-rc-images.tar.gz"
docker run --rm "${images[1]}" python3 -m pip list --format=json >"$work_dir/sbom/python-packages.json"
cp "$root/package-lock.json" "$work_dir/sbom/web-package-lock.json"
cp "$root/services/pilot-agentd/package-lock.json" "$work_dir/sbom/agentd-package-lock.json"

python3 - "$work_dir" "$revision" "${images[@]}" <<'PY'
import datetime
import json
import pathlib
import subprocess
import sys

bundle = pathlib.Path(sys.argv[1])
revision = sys.argv[2]
images = sys.argv[3:]
records = []
for image in images:
    payload = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
    records.append({
        "reference": image,
        "content_digest": payload["Id"],
        "repo_digests": payload.get("RepoDigests") or [],
        "created": payload.get("Created"),
        "architecture": payload.get("Architecture"),
        "os": payload.get("Os"),
    })
manifest = {
    "schema": "pilot107.cpu_rc_release.v1",
    "release_revision": revision,
    "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "evidence_scope": "D1 local CPU-only profile; not VM and not real 107",
    "target": {"host_cpu": 8, "host_memory_gib": 16, "slurm_cpu": 4, "slurm_memory_gib": 6},
    "capability_profile": "config/platform_profiles/cpu-only-8c16g.json",
    "control_migrations": ["003g.001", "003g.002"],
    "images": records,
}
(bundle / "RELEASE_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
PY

cat >"$work_dir/README_DEPLOY.md" <<EOF
# 107Pilot CPU-only Release Candidate

Revision: \`$revision\`

Evidence scope: D1 local CPU-only profile. This is prepared for a future 8C/16G
VM test; it has not been uploaded, deployed to a VM, or connected to real 107.

## Import and start

\`\`\`bash
sha256sum -c SHA256SUMS
bash scripts/import-cpu-rc-images.sh
PILOT107_SKIP_BUILD=1 bash scripts/start-cpu-rc.sh
bash scripts/check-cpu-rc.sh
\`\`\`

The start script creates local credentials and a 30-day self-signed certificate.
It refuses placeholder credentials. For a formal deployment replace the local
certificate and complete the external identity/platform admission checks.

## Stop

\`\`\`bash
bash scripts/stop-cpu-rc.sh
\`\`\`

## Recovery

Use \`scripts/control-plane-recovery.py\` as documented in
\`docs-operations/control_plane_observability.md\`. Recovery must be rehearsed
against a copy and followed by \`bash scripts/check-cpu-rc.sh\`.
EOF

assert_required_files_exist() {
  local missing=()
  local required=(
    "scripts/accept-source-release.sh"
    "scripts/accept-runtime-bundle.sh"
    "scripts/check-cpu-rc.sh"
    "scripts/start-cpu-rc.sh"
    "scripts/build-cpu-rc-images.sh"
    "scripts/smoke-cpu-rc-remediation.sh"
    "scripts/smoke-auto-capsule.sh"
    "scripts/smoke-restart-volume-recovery.sh"
    "scripts/preflight-cpu-rc-vm.sh"
    "scripts/import-cpu-rc-images.sh"
    "scripts/init-local-secrets.sh"
    "scripts/stop-cpu-rc.sh"
    "scripts/apply-cpu-rc-profile.sh"
    "scripts/verify-cpu-rc-image-binding.sh"
    "RELEASE_MANIFEST.json"
    "SHA256SUMS"
    "images/pilot107-cpu-rc-images.tar.gz"
    "images/images.txt"
    "sbom/agentd-package-lock.json"
    "simulator/compose/compose.cpu-rc.yml"
    "simulator/compose/compose.yml"
    "simulator/compose/compose.competition.yml"
    "simulator/compose/slurm-cpu-rc/slurm.conf"
    "simulator/compose/slurm-cpu-rc/cgroup.conf"
    "simulator/compose/.env.cpu-rc.example"
    "src"
    "apps"
    "services/pilot-agentd"
    "pyproject.toml"
  )
  for rel in "${required[@]}"; do
    if [[ ! -e "$work_dir/$rel" ]]; then
      missing+=("$rel")
    fi
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    echo "export-cpu-rc-bundle: required files missing from bundle:" >&2
    printf '  %s\n' "${missing[@]}" >&2
    return 1
  fi
}

(
  cd "$work_dir"
  find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum >SHA256SUMS
  sha256sum -c SHA256SUMS >/dev/null
)
# Assert required files exist BEFORE tar (after RELEASE_MANIFEST.json and
# SHA256SUMS are generated). A missing file here means a copy step silently
# dropped it — fail the export rather than ship an incomplete bundle.
assert_required_files_exist
tar -C "$out_root" -czf "$archive" "$bundle_name"
sha256sum "$archive" >"$archive.sha256"

echo "bundle_dir=$work_dir"
echo "bundle_archive=$archive"
echo "bundle_sha256=$archive.sha256"
