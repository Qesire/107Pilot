#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
out_root="${PILOT107_BUNDLE_DIR:-$root/artifacts/deployment}"
bundle_name="107pilot-competition-bundle-${timestamp}"
work_dir="$out_root/$bundle_name"
archive="$out_root/${bundle_name}.tar.gz"

mkdir -p "$out_root"
rm -rf "$work_dir" "$archive"
mkdir -p "$work_dir"

if [[ "${PILOT107_SKIP_BUILD:-0}" != "1" ]]; then
  bash "$root/scripts/build-slurm-sim-image.sh"
  bash "$root/scripts/build-app-images.sh"
fi

mkdir -p \
  "$work_dir/scripts" \
  "$work_dir/simulator" \
  "$work_dir/docs/phase-0" \
  "$work_dir/docs/phase-1" \
  "$work_dir/apps" \
  "$work_dir/data" \
  "$work_dir/images"

cp -a "$root/src" "$work_dir/src"
cp -a "$root/apps/." "$work_dir/apps/"
cp -a "$root/data/known_errors" "$work_dir/data/known_errors"
cp -a "$root/data/submission_templates" "$work_dir/data/submission_templates"
cp -a "$root/simulator/compose" "$work_dir/simulator/compose"
cp "$root/pyproject.toml" "$work_dir/"
cp "$root/README.md" "$work_dir/"
cp "$root/uv.lock" "$work_dir/" 2>/dev/null || true

for script in \
  build-app-images.sh \
  build-slurm-sim-image.sh \
  check-app-images.sh \
  check-competition.sh \
  import-competition-images.sh \
  load_competition.py \
  preflight-competition-vm.sh \
  smoke_competition_web.py \
  start-competition-app-node.sh \
  start-competition.sh \
  start-competition-slurm-host.sh \
  stop-competition-app-node.sh \
  stop-competition.sh \
  stop-competition-slurm-host.sh
do
  cp "$root/scripts/$script" "$work_dir/scripts/"
done

for doc in \
  competition_deployment_check_report.md \
  competition_deployment_plan.md \
  load_capacity_report.md \
  server_questions.md
do
  cp "$root/docs/phase-0/$doc" "$work_dir/docs/phase-0/"
done

for doc in \
  error_library.md \
  interface_hardening_status.md \
  submission_templates.md
do
  cp "$root/docs/phase-1/$doc" "$work_dir/docs/phase-1/"
done

rm -f "$work_dir/simulator/compose/.env.competition"
rm -rf "$work_dir/simulator/compose/certs"
find "$work_dir" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$work_dir" -type f -name '*.pyc' -delete

cat > "$work_dir/README_DEPLOY.md" <<'EOF'
# 107Pilot Competition Bundle

This bundle is prepared for the VM test step.

## Import images

```bash
bash scripts/import-competition-images.sh
```

## Preflight

```bash
bash scripts/preflight-competition-vm.sh --require-images
```

## Start

Single-VM profile:

```bash
PILOT107_SKIP_BUILD=1 bash scripts/start-competition.sh
```

Two-machine profile:

On the Slurm host:

```bash
PILOT107_SKIP_BUILD=1 bash scripts/start-competition-slurm-host.sh
```

On the app node, edit `simulator/compose/.env.competition` and set:

```text
PILOT107_REMOTE_COMMAND_GATEWAY_URL=http://<slurm-host-ip>:18090
```

Then run:

```bash
PILOT107_SKIP_BUILD=1 bash scripts/start-competition-app-node.sh
```

## Smoke

```bash
bash scripts/check-competition.sh
```

The local self-signed certificate is generated on first start. Replace
`simulator/compose/certs/tls.crt` and `tls.key` with school-provided
certificates for formal competition deployment.
EOF

if [[ "${PILOT107_EXPORT_IMAGES:-1}" == "1" ]]; then
  docker save \
    pilot107/slurm-sim:local \
    pilot107/api:local \
    pilot107/worker:local \
    pilot107/web:local \
    | gzip -c > "$work_dir/images/pilot107-images.tar.gz"
  cat > "$work_dir/images/images.txt" <<'EOF'
pilot107/slurm-sim:local
pilot107/api:local
pilot107/worker:local
pilot107/web:local
EOF
else
  cat > "$work_dir/images/README.md" <<'EOF'
# Images omitted

This bundle was exported with `PILOT107_EXPORT_IMAGES=0`.
Build or import the four required images before starting:

- pilot107/slurm-sim:local
- pilot107/api:local
- pilot107/worker:local
- pilot107/web:local
EOF
fi

(
  cd "$work_dir"
  find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS
)

tar -C "$out_root" -czf "$archive" "$bundle_name"
sha256sum "$archive" > "$archive.sha256"

echo "bundle_dir=$work_dir"
echo "bundle_archive=$archive"
echo "bundle_sha256=$archive.sha256"
