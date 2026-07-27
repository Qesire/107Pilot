#!/usr/bin/env bash
set -euo pipefail

# Submit the minimum explicitly authorized real-107 acceptance set.  The
# command surface is fixed: one success, one exit-42 failure, and one sleeping
# job cancelled by its own recorded id.  It never uses sudo or project files.

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target="${PILOT107_REAL107_SSH_TARGET:-}"
workdir="${PILOT107_REAL107_WORKDIR:-}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
output_dir="${PILOT107_REAL107_JOB_OUTPUT_DIR:-$root/artifacts/probes/real107-jobs-$stamp}"

usage() {
  cat <<'EOF'
Usage:
  PILOT107_REAL107_SSH_TARGET=<ssh-config-alias> \
  PILOT107_REAL107_WORKDIR=<private-home>/pilot107-smoke-<label> \
  bash scripts/smoke-real107-ssh-jobs.sh

The fixed acceptance set uses account=stu, partition=Students,
qos=qos_stu_default, one CPU and a two-minute limit. It submits exactly three
jobs: success, expected exit-42 failure, and a sleep job cancelled by this
script. Remote evidence remains in the requested private directory and is
copied to a new local artifact directory.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi
if [[ ! "$target" =~ ^[A-Za-z0-9_.@:-]+$ ]]; then
  echo "PILOT107_REAL107_SSH_TARGET must be a configured, safe SSH alias" >&2
  exit 2
fi
if [[ ! "$workdir" =~ ^(/public/home/[A-Za-z0-9._-]+|/home/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)/pilot107-smoke-[A-Za-z0-9._-]+$ ]]; then
  echo "PILOT107_REAL107_WORKDIR must be a new private home/pilot107-smoke-* directory" >&2
  exit 2
fi
if [[ -e "$output_dir" ]]; then
  echo "refusing to overwrite existing output directory: $output_dir" >&2
  exit 2
fi

mkdir -p "$output_dir/jobs"
ssh_options=(
  -o BatchMode=yes
  -o ConnectTimeout=15
  -o ServerAliveInterval=10
  -o ServerAliveCountMax=2
)
remote() {
  ssh "${ssh_options[@]}" -- "$target" "$@"
}
remote_user="$(remote whoami)"
if [[ ! "$remote_user" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "remote whoami returned an unsafe username" >&2
  exit 1
fi
case "$workdir" in
  "/public/home/$remote_user/pilot107-smoke-"*|"/home/"*/"$remote_user/pilot107-smoke-"*) ;;
  *)
    echo "PILOT107_REAL107_WORKDIR must belong to the authenticated remote user" >&2
    exit 2
    ;;
esac

cat > "$output_dir/jobs/success.sbatch" <<'EOF'
#!/usr/bin/env bash
#SBATCH --account=stu
#SBATCH --partition=Students
#SBATCH --qos=qos_stu_default
#SBATCH --time=00:02:00
#SBATCH --cpus-per-task=1
#SBATCH --output=success-%j.out
#SBATCH --error=success-%j.err
set -euo pipefail
umask 077
hostname
printf 'pilot107-real107-success\n' > success.txt
EOF
cat > "$output_dir/jobs/failure.sbatch" <<'EOF'
#!/usr/bin/env bash
#SBATCH --account=stu
#SBATCH --partition=Students
#SBATCH --qos=qos_stu_default
#SBATCH --time=00:02:00
#SBATCH --cpus-per-task=1
#SBATCH --output=failure-%j.out
#SBATCH --error=failure-%j.err
set -euo pipefail
printf 'pilot107-real107-expected-failure\n' >&2
exit 42
EOF
cat > "$output_dir/jobs/cancel.sbatch" <<'EOF'
#!/usr/bin/env bash
#SBATCH --account=stu
#SBATCH --partition=Students
#SBATCH --qos=qos_stu_default
#SBATCH --time=00:02:00
#SBATCH --cpus-per-task=1
#SBATCH --output=cancel-%j.out
#SBATCH --error=cancel-%j.err
set -euo pipefail
printf 'pilot107-real107-cancel-started\n'
sleep 120
printf 'pilot107-real107-cancel-missed\n' >&2
EOF

remote "umask 077; test ! -e '$workdir'; mkdir -m 700 '$workdir'"
scp "${ssh_options[@]}" -- "$output_dir/jobs/success.sbatch" "$output_dir/jobs/failure.sbatch" \
  "$output_dir/jobs/cancel.sbatch" "$target:$workdir/"

submit() {
  local script_name="$1"
  local reply job_id
  reply="$(remote "cd '$workdir' && sbatch --parsable '$script_name'")"
  job_id="${reply%%;*}"
  if [[ ! "$job_id" =~ ^[0-9]+$ ]]; then
    echo "unexpected sbatch --parsable reply for $script_name: $reply" >&2
    exit 1
  fi
  printf '%s\n' "$job_id"
}

active_jobs=()
cleanup_active_jobs() {
  local job_id
  for job_id in "${active_jobs[@]}"; do
    remote "scancel '$job_id'" >/dev/null 2>&1 || true
  done
}
trap cleanup_active_jobs EXIT

job_state() {
  local job_id="$1"
  remote "sacct -n -X -P -j '$job_id' --format=JobIDRaw,State,ExitCode" \
    | awk -F'|' -v expected="$job_id" '$1 == expected {print $2 "|" $3; exit}'
}
wait_terminal() {
  local job_id="$1" expected_state="$2" expected_exit="$3"
  local deadline=$((SECONDS + 150)) state_exit raw_state state exit_code
  while ((SECONDS < deadline)); do
    state_exit="$(job_state "$job_id")"
    if [[ -n "$state_exit" ]]; then
      raw_state="${state_exit%%|*}"
      # sacct may report a cancellation as "CANCELLED by <uid>". Preserve the
      # raw accounting line in evidence but compare its documented base state.
      state="${raw_state%%[ +]*}"
      exit_code="${state_exit#*|}"
      case "$state" in
        COMPLETED|FAILED|CANCELLED|TIMEOUT|OUT_OF_MEMORY|NODE_FAIL)
          if [[ "$state" != "$expected_state" || ( "$expected_exit" != "*" && "$exit_code" != "$expected_exit" ) ]]; then
            echo "job $job_id expected $expected_state/$expected_exit, got $state/$exit_code" >&2
            exit 1
          fi
          printf '%s|%s|%s\n' "$job_id" "$raw_state" "$exit_code"
          return 0
          ;;
      esac
    fi
    sleep 2
  done
  echo "job $job_id did not reach a terminal state before timeout" >&2
  exit 1
}
wait_running() {
  local job_id="$1" deadline=$((SECONDS + 60)) state
  while ((SECONDS < deadline)); do
    state="$(remote "squeue -h -j '$job_id' -o '%T'" || true)"
    if [[ "$state" == "RUNNING" ]]; then
      return 0
    fi
    sleep 2
  done
  echo "job $job_id did not reach RUNNING before cancel timeout" >&2
  exit 1
}

success_id="$(submit success.sbatch)"
active_jobs+=("$success_id")
failure_id="$(submit failure.sbatch)"
active_jobs+=("$failure_id")
success_result="$(wait_terminal "$success_id" COMPLETED 0:0)"
failure_result="$(wait_terminal "$failure_id" FAILED 42:0)"

cancel_id="$(submit cancel.sbatch)"
active_jobs+=("$cancel_id")
wait_running "$cancel_id"
remote "scancel '$cancel_id'"
cancel_result="$(wait_terminal "$cancel_id" CANCELLED '*')"
active_jobs=()

{
  printf 'workdir=%s\n' "$workdir"
  printf 'success=%s\n' "$success_result"
  printf 'failure=%s\n' "$failure_result"
  printf 'cancelled=%s\n' "$cancel_result"
} | tee "$output_dir/summary.txt"

scp -r "${ssh_options[@]}" -- "$target:$workdir" "$output_dir/remote-workdir"
printf 'real107 job evidence=%s\n' "$output_dir"
