#!/usr/bin/env bash
set -euo pipefail

image="${SLURM_SIM_25_IMAGE:-pilot107/slurm-sim:25.11-real107}"

docker run --rm "$image" bash -lc '
set -euo pipefail
command -v slurmctld
command -v slurmd
command -v slurmdbd
command -v slurmrestd
command -v sbatch
command -v python
python -V
slurmctld -V | grep -F "25.11.2"
id alice
id bob
id pilot107
id slurm
test -r /etc/munge/munge.key
test -r /etc/pilot107/slurm-sim-version-manifest.json
python3 - <<'"'"'PY'"'"'
import json
from pathlib import Path

manifest = json.loads(Path("/etc/pilot107/slurm-sim-version-manifest.json").read_text())
assert manifest["target"]["status"] == "current", manifest
assert manifest["target"]["slurm_version"] == "25.11.2", manifest
assert manifest["fallback"]["slurm_version"].startswith("23.11"), manifest
assert manifest["runtime_fidelity"]["real_gpu_devices"] == "unavailable", manifest
PY
'

echo "slurm simulator 25.11 image ok"
