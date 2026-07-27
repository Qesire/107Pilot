#!/bin/bash
#SBATCH --account=stu
#SBATCH --partition=Students
#SBATCH --qos=qos_stu_medium_2gpu
#SBATCH --gres=gpu:A100:1
#SBATCH --cpus-per-task=1
#SBATCH --time=00:02:00
#SBATCH --job-name=pilot107-compute-probe
#SBATCH --output=compute-probe-%j.out
#SBATCH --error=compute-probe-%j.err

set -eu
cd "${SLURM_SUBMIT_DIR:?Slurm did not provide a submit directory}"
exec python3 ./probe_real107_compute_runtime.py
