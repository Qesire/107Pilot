#!/usr/bin/env bash
# CPU-RC VM preflight (D1 -> S1 gate).
#
# Evidence scope: S1 VM preflight; not real 107.
# Reference: docs/phase-3/revised_execution_plan_20260716.md (G3 acceptance chain, step 1).
#
# Read-only checks against an 8C/16G target host before importing the CPU-RC
# release bundle. Validates: Docker CLI/daemon, docker compose v2 plugin,
# python3, openssl, compose.cpu-rc.yml config, CPU-only profile assertion,
# resource floor (>=20GB disk; warn on <8 CPU / <14 GiB), clock sync, and
# (with --require-images) presence of the four CPU-RC images.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_dir="$root/simulator/compose"
env_file="${PILOT107_CPU_RC_ENV_FILE:-$compose_dir/.env.cpu-rc}"
if [[ ! -f "$env_file" ]]; then
  env_file="$compose_dir/.env.cpu-rc.example"
fi

require_images=0
if [[ "${1:-}" == "--require-images" ]]; then
  require_images=1
fi

failures=0
warnings=0

check() {
  local name="$1"
  shift
  if "$@" >/tmp/pilot107-cpu-rc-preflight.out 2>/tmp/pilot107-cpu-rc-preflight.err; then
    echo "ok   $name"
  else
    echo "fail $name"
    cat /tmp/pilot107-cpu-rc-preflight.err >&2 || true
    failures=$((failures + 1))
  fi
}

warn() {
  echo "warn $1"
  warnings=$((warnings + 1))
}

# --- Core tooling ---
check "docker CLI" command -v docker
check "docker daemon access" docker info
check "docker compose v2" docker compose version
check "python3" command -v python3
check "openssl" command -v openssl

# --- cgroup v2 delegation (required for cpu-rc walltime enforcement) ---
# The cpu-rc compose envelope mounts /sys/fs/cgroup rw and runs containers with
# `cgroup: host`, so Slurm's proctrack/cgroup + task/cgroup can enforce walltime.
# The target host must delegate the host cgroup namespace to Docker containers
# and expose cgroup v2 writably.
cgroup_fs_type="$(stat -fc %T /sys/fs/cgroup 2>/dev/null || true)"
if [[ "$cgroup_fs_type" == "cgroup2fs" ]]; then
  echo "ok   cgroup v2 filesystem at /sys/fs/cgroup"
else
  echo "fail cgroup v2 filesystem at /sys/fs/cgroup: got '${cgroup_fs_type:-unknown}' (expected cgroup2fs)"
  echo "cpu-rc VM preflight: target host must support cgroup v2 delegation to Docker containers for the cpu-rc sim to enforce walltime" >&2
  failures=$((failures + 1))
fi

cgroup_probe_image=""
for candidate in alpine:3.20 alpine:3 "${PILOT107_PREFLIGHT_BASE_IMAGE:-}"; do
  [[ -z "$candidate" ]] && continue
  if docker image inspect "$candidate" >/dev/null 2>&1; then
    cgroup_probe_image="$candidate"
    break
  fi
done
if [[ -z "$cgroup_probe_image" ]]; then
  cgroup_probe_image="alpine:3.20"
fi

cgroup_probe_out="$(docker run --rm --cgroup host \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  "$cgroup_probe_image" \
  sh -c 'touch /sys/fs/cgroup/.pilot107_probe 2>/dev/null && rm -f /sys/fs/cgroup/.pilot107_probe && echo writable' 2>/dev/null || true)"
if [[ "$cgroup_probe_out" == *"writable"* ]]; then
  echo "ok   cgroup v2 writable from Docker container with cgroup: host"
else
  echo "fail cgroup v2 writable from Docker container with cgroup: host"
  echo "cpu-rc VM preflight: target host must support cgroup v2 delegation to Docker containers for the cpu-rc sim to enforce walltime" >&2
  failures=$((failures + 1))
fi

# --- Compose config (CPU-RC profile) ---
check "cpu-rc compose config" docker compose \
  --env-file "$env_file" \
  -f "$compose_dir/compose.yml" \
  -f "$compose_dir/compose.cpu-rc.yml" \
  config

# --- CPU-only profile assertion ---
# GPU recipes must be disabled and no GPU partition declared.
gpu_recipes="$(docker compose \
  --env-file "$env_file" \
  -f "$compose_dir/compose.yml" \
  -f "$compose_dir/compose.cpu-rc.yml" \
  config 2>/dev/null | grep -i 'PILOT107_ALLOW_GPU_RECIPES' | grep -i 'true' || true)"
if [[ -z "$gpu_recipes" ]]; then
  echo "ok   CPU-only profile (PILOT107_ALLOW_GPU_RECIPES not true)"
else
  echo "fail CPU-only profile: PILOT107_ALLOW_GPU_RECIPES=true detected"
  failures=$((failures + 1))
fi

# --- Resource floor ---
required_space_gb="${PILOT107_REQUIRED_FREE_GB:-20}"
available_gb="$(df -BG "$root" | awk 'NR==2 {gsub(/G/, "", $4); print $4}')"
if [[ -n "$available_gb" && "$available_gb" -ge "$required_space_gb" ]]; then
  echo "ok   disk free ${available_gb}GB >= ${required_space_gb}GB"
else
  echo "fail disk free ${available_gb:-unknown}GB < ${required_space_gb}GB"
  failures=$((failures + 1))
fi

cpu_count="$(nproc 2>/dev/null || echo 0)"
if [[ "$cpu_count" -ge 8 ]]; then
  echo "ok   CPU count ${cpu_count} >= 8"
else
  warn "CPU count ${cpu_count} < 8 (target is 8C/16G)"
fi

mem_total_kib="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
mem_total_gib=$(( mem_total_kib / 1024 / 1024 ))
if [[ "$mem_total_gib" -ge 14 ]]; then
  echo "ok   memory ${mem_total_gib} GiB >= 14 GiB"
else
  warn "memory ${mem_total_gib} GiB < 14 GiB (target is 16 GiB, headroom for host)"
fi

# --- Clock sync (warn only) ---
if command -v timedatectl >/dev/null 2>&1; then
  ntp_synced="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo 'unknown')"
  if [[ "$ntp_synced" == "yes" ]]; then
    echo "ok   NTP synchronized"
  else
    warn "NTP not synchronized (run: timedatectl set-ntp true)"
  fi
fi

# --- Port bindability ---
for port_var in PILOT107_HTTP_PORT PILOT107_HTTPS_PORT SLURMRESTD_PORT PILOT107_API_PORT PILOT107_WEB_PORT; do
  raw="$(awk -F= -v key="$port_var" '$1 == key {print $2}' "$env_file" | tail -1)"
  [[ -z "$raw" ]] && continue
  port="${raw##*:}"
  if python3 - "$port" <<'PY'
import socket, sys
port = int(sys.argv[1])
sock = socket.socket()
try:
    sock.bind(("0.0.0.0", port))
except OSError:
    sys.exit(1)
else:
    sys.exit(0)
finally:
    sock.close()
PY
  then
    echo "ok   port $port_var=$raw available"
  else
    echo "warn port $port_var=$raw not available; expected if services already running"
  fi
done

# --- Image presence (opt-in) ---
if [[ "$require_images" == "1" ]]; then
  for image in \
    pilot107/slurm-sim:cpu-rc-9f0187e5ff38 \
    pilot107/api:cpu-rc-9f0187e5ff38 \
    pilot107/worker:cpu-rc-9f0187e5ff38 \
    pilot107/web:cpu-rc-9f0187e5ff38; do
    if docker image inspect "$image" >/dev/null 2>&1; then
      echo "ok   image $image"
    else
      echo "fail image missing: $image"
      failures=$((failures + 1))
    fi
  done
fi

# --- Verdict ---
if [[ "$failures" -ne 0 ]]; then
  echo "cpu-rc VM preflight failed: $failures issue(s), $warnings warning(s)" >&2
  exit 1
fi

echo "cpu-rc VM preflight ok ($warnings warning(s))"
