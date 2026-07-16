#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image="${SLURM_SIM_IMAGE:-pilot107/slurm-sim:local}"

docker build \
  -t "$image" \
  -f "$root/simulator/images/slurm/Dockerfile" \
  "$root/simulator/images/slurm"
