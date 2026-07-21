#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
revision="${PILOT107_CPU_RC_REVISION:-$(git -C "$root" rev-parse --short=12 HEAD)}"

# Slurm simulator image: by default BUILD it from the pinned-digest Dockerfile
# (simulator/images/slurm/Dockerfile) so the digest pin actually participates in
# the bundle. The optional PILOT107_CPU_RC_SLURM_SOURCE escape hatch lets
# environments with a pre-built/cached slurm image skip the (slow) source build
# — but it must be set explicitly AND non-empty. Default = build from source.
if [[ -n "${PILOT107_CPU_RC_SLURM_SOURCE:-}" ]]; then
  docker image inspect "$PILOT107_CPU_RC_SLURM_SOURCE" >/dev/null
  docker tag "$PILOT107_CPU_RC_SLURM_SOURCE" "pilot107/slurm-sim:cpu-rc-$revision"
else
  docker build \
    -t "pilot107/slurm-sim:cpu-rc-$revision" \
    -f "$root/simulator/images/slurm/Dockerfile" \
    "$root/simulator/images/slurm"
fi
docker tag "pilot107/slurm-sim:cpu-rc-$revision" pilot107/slurm-sim:cpu-rc

PILOT107_API_IMAGE="pilot107/api:cpu-rc-$revision" \
PILOT107_WORKER_IMAGE="pilot107/worker:cpu-rc-$revision" \
PILOT107_WEB_IMAGE="pilot107/web:cpu-rc-$revision" \
  bash "$root/scripts/build-app-images.sh"

docker tag "pilot107/api:cpu-rc-$revision" pilot107/api:cpu-rc
docker tag "pilot107/worker:cpu-rc-$revision" pilot107/worker:cpu-rc
docker tag "pilot107/web:cpu-rc-$revision" pilot107/web:cpu-rc

echo "CPU RC images built for revision $revision"
printf '%s\n' \
  "pilot107/slurm-sim:cpu-rc-$revision" \
  "pilot107/api:cpu-rc-$revision" \
  "pilot107/worker:cpu-rc-$revision" \
  "pilot107/web:cpu-rc-$revision"
