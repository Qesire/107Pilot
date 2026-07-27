#!/usr/bin/env bash
set -euo pipefail

# Submit exactly one fixed, short GPU runtime probe to an authorized real107
# account. It uses a fresh private directory, never reads project files, and
# does not create or retain REST tokens.

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'EOF'
Usage:
  PILOT107_REAL107_SSH_TARGET=<ssh-config-alias> \
  PILOT107_REAL107_WORKDIR=/home/<group>/<user>/pilot107-compute-<label> \
  bash scripts/probe-real107-ssh-compute.sh

The probe submits one fixed Students/qos_stu_medium_2gpu A100 job with one CPU
and a two-minute walltime. It captures only GPU/runtime, selected SLURM
variables, and fixed filesystem metadata, then leaves the private evidence
directory in place and copies it into a local artifact directory.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

target="${PILOT107_REAL107_SSH_TARGET:-}"
workdir="${PILOT107_REAL107_WORKDIR:-}"
max_polls="${PILOT107_REAL107_COMPUTE_MAX_POLLS:-60}"
if [[ ! "$target" =~ ^[A-Za-z0-9_.@:-]+$ ]]; then
  echo "PILOT107_REAL107_SSH_TARGET must be a configured, safe SSH alias" >&2
  exit 2
fi
if [[ ! "$workdir" =~ ^/(home|public/home)/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pilot107-compute-[A-Za-z0-9_.-]+$ ]]; then
  echo "PILOT107_REAL107_WORKDIR must be a fresh private pilot107-compute directory" >&2
  exit 2
fi
if [[ ! "$max_polls" =~ ^[1-9][0-9]?$ ]]; then
  echo "PILOT107_REAL107_COMPUTE_MAX_POLLS must be between 1 and 99" >&2
  exit 2
fi

probe="$root/scripts/real107_probe/probe_real107_compute_runtime.py"
sbatch_file="$root/scripts/real107_probe/real107_compute_runtime_probe.sbatch"
if [[ ! -r "$probe" || ! -r "$sbatch_file" ]]; then
  echo "missing fixed compute probe assets" >&2
  exit 1
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
output_dir="${PILOT107_REAL107_COMPUTE_OUTPUT_DIR:-$root/artifacts/probes/real107-compute-ssh-$stamp}"
if [[ -e "$output_dir" ]]; then
  echo "refusing to overwrite existing output directory: $output_dir" >&2
  exit 2
fi
mkdir -p "$output_dir"

ssh_options=(-o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=10 -o ServerAliveCountMax=2)
remote_user="$(ssh "${ssh_options[@]}" -- "$target" 'whoami')"
if [[ ! "$remote_user" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  echo "remote whoami result was unsafe" >&2
  exit 1
fi
if [[ ! "$workdir" =~ ^/(home|public/home)/[A-Za-z0-9_.-]+/$remote_user/pilot107-compute-[A-Za-z0-9_.-]+$ ]]; then
  echo "workdir must belong to the authenticated remote user" >&2
  exit 2
fi

ssh "${ssh_options[@]}" -- "$target" "test ! -e '$workdir' && mkdir -m 700 -p '$workdir'"
scp "${ssh_options[@]}" -- "$probe" "$sbatch_file" "$target:$workdir/"
job_id="$(ssh "${ssh_options[@]}" -- "$target" "cd '$workdir' && sbatch --parsable real107_compute_runtime_probe.sbatch")"
job_id="${job_id%%;*}"
if [[ ! "$job_id" =~ ^[0-9]+$ ]]; then
  echo "sbatch did not return a numeric job id: $job_id" >&2
  exit 1
fi

state=""
raw_state=""
exit_code=""
for ((attempt = 1; attempt <= max_polls; attempt += 1)); do
  row="$(ssh "${ssh_options[@]}" -- "$target" "sacct -nP -X -j '$job_id' -o JobIDRaw,State,ExitCode | head -n 1")"
  IFS='|' read -r observed_id raw_state exit_code _ <<<"$row"
  state="${raw_state%%[ +]*}"
  if [[ "$observed_id" == "$job_id" && "$state" == "COMPLETED" && "$exit_code" == "0:0" ]]; then
    break
  fi
  sleep 2
done
if [[ "$state" != "COMPLETED" || "$exit_code" != "0:0" ]]; then
  echo "compute probe did not complete successfully: job=$job_id state=$raw_state exit=$exit_code" >&2
  exit 1
fi

scp -r "${ssh_options[@]}" -- "$target:$workdir/." "$output_dir/remote-workdir/"
result_file="$(find "$output_dir/remote-workdir" -maxdepth 1 -name 'compute-probe-*.out' -type f -print -quit)"
if [[ -z "$result_file" || ! -s "$result_file" ]]; then
  echo "compute probe output was missing" >&2
  exit 1
fi
if ! grep -q '^PILOT107_COMPUTE_PROBE_JSON=' "$result_file"; then
  echo "compute probe output did not contain the required JSON record" >&2
  exit 1
fi
printf 'job_id=%s\nstate=%s\nexit_code=%s\nworkdir=%s\n' "$job_id" "$raw_state" "$exit_code" "$workdir" >"$output_dir/summary.txt"
printf 'real107 compute probe=%s\n' "$output_dir"
