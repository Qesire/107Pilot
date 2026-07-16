#!/usr/bin/env bash
#SBATCH --job-name=pilot107-probe
#SBATCH -p Students
#SBATCH --qos=qos_stu_medium_2gpu
#SBATCH --gres=gpu:A100:1
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=00:05:00
#SBATCH --output=real107-probe-%j.out
#SBATCH --error=real107-probe-%j.err

set -euo pipefail

cd "${SLURM_SUBMIT_DIR:-$(pwd)}"

# Prefer a short-lived token generated inside the job. The stdout is parsed by
# probe_real107_snapshot.py and is not written to probe artifacts.
export PILOT107_REAL107_TOKEN_COMMAND="${PILOT107_REAL107_TOKEN_COMMAND:-scontrol token lifespan=600}"

export PILOT107_REAL107_BASE_URL="${PILOT107_REAL107_BASE_URL:-http://107.ustc.edu.cn:6820}"
export PILOT107_REAL107_API_VERSION="${PILOT107_REAL107_API_VERSION:-v0.0.41}"
export PILOT107_REAL107_USERNAME="${PILOT107_REAL107_USERNAME:-${USER:-unknown}}"
export PILOT107_REAL107_OUT_DIR="${PILOT107_REAL107_OUT_DIR:-real107-probe-output/${SLURM_JOB_ID:-manual}}"

python3 probe_real107_snapshot.py \
  --base-url "${PILOT107_REAL107_BASE_URL}" \
  --api-version "${PILOT107_REAL107_API_VERSION}" \
  --username "${PILOT107_REAL107_USERNAME}" \
  --out-dir "${PILOT107_REAL107_OUT_DIR}"
