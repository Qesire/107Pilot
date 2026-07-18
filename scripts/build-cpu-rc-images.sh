#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
revision="${PILOT107_CPU_RC_REVISION:-$(git -C "$root" rev-parse --short=12 HEAD)}"
slurm_source="${PILOT107_CPU_RC_SLURM_SOURCE:-pilot107/slurm-sim:25.11-real107}"

docker image inspect "$slurm_source" >/dev/null
docker tag "$slurm_source" "pilot107/slurm-sim:cpu-rc-$revision"
docker tag "$slurm_source" pilot107/slurm-sim:cpu-rc

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
