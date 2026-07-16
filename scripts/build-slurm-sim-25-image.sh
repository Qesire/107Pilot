#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image="${SLURM_SIM_25_IMAGE:-pilot107/slurm-sim:25.11-real107}"
version="${SLURM_SIM_25_VERSION:-25.11.2}"

docker build \
  --build-arg "SLURM_VERSION=$version" \
  -t "$image" \
  -f "$root/simulator/images/slurm/Dockerfile.25.11" \
  "$root/simulator/images/slurm"
