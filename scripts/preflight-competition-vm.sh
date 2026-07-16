#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_dir="$root/simulator/compose"
env_file="${PILOT107_COMPETITION_ENV_FILE:-$compose_dir/.env.competition}"
if [[ ! -f "$env_file" ]]; then
  env_file="$compose_dir/.env.competition.example"
fi

require_images=0
if [[ "${1:-}" == "--require-images" ]]; then
  require_images=1
fi

failures=0

check() {
  local name="$1"
  shift
  if "$@" >/tmp/pilot107-preflight.out 2>/tmp/pilot107-preflight.err; then
    echo "ok   $name"
  else
    echo "fail $name"
    cat /tmp/pilot107-preflight.err >&2 || true
    failures=$((failures + 1))
  fi
}

check "docker CLI" command -v docker
check "docker daemon access" docker info
check "docker compose" docker compose version
check "python3" command -v python3
check "openssl" command -v openssl

check "competition compose config" docker compose \
  --env-file "$env_file" \
  -f "$compose_dir/compose.yml" \
  -f "$compose_dir/compose.competition.yml" \
  --profile competition \
  config

required_space_gb="${PILOT107_REQUIRED_FREE_GB:-20}"
available_gb="$(df -BG "$root" | awk 'NR==2 {gsub(/G/, "", $4); print $4}')"
if [[ -n "$available_gb" && "$available_gb" -ge "$required_space_gb" ]]; then
  echo "ok   disk free ${available_gb}GB >= ${required_space_gb}GB"
else
  echo "fail disk free ${available_gb:-unknown}GB < ${required_space_gb}GB"
  failures=$((failures + 1))
fi

for port_var in PILOT107_HTTP_PORT PILOT107_HTTPS_PORT SLURMRESTD_PORT PILOT107_API_PORT PILOT107_WEB_PORT; do
  raw="$(awk -F= -v key="$port_var" '$1 == key {print $2}' "$env_file" | tail -1)"
  [[ -z "$raw" ]] && continue
  port="${raw##*:}"
  if python3 - "$port" <<'PY'
import socket
import sys
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
    echo "warn port $port_var=$raw not available; this is expected if services are already running"
  fi
done

if [[ "$require_images" == "1" ]]; then
  for image in pilot107/slurm-sim:local pilot107/api:local pilot107/worker:local pilot107/web:local; do
    if docker image inspect "$image" >/dev/null 2>&1; then
      echo "ok   image $image"
    else
      echo "fail image missing: $image"
      failures=$((failures + 1))
    fi
  done
fi

if [[ "$failures" -ne 0 ]]; then
  echo "preflight failed: $failures issue(s)" >&2
  exit 1
fi

echo "competition VM preflight ok"
