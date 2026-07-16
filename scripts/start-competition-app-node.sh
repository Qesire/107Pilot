#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_dir="$root/simulator/compose"
env_file="${PILOT107_COMPETITION_ENV_FILE:-$compose_dir/.env.competition}"
env_template="$compose_dir/.env.competition.example"

if [[ ! -f "$env_file" ]]; then
  cp "$env_template" "$env_file"
  echo "created $env_file from template"
fi

if ! grep -q '^PILOT107_REMOTE_COMMAND_GATEWAY_URL=' "$env_file"; then
  cat >>"$env_file" <<'EOF'

# Two-machine app-node mode: set this to the Slurm host command-gateway endpoint.
PILOT107_REMOTE_COMMAND_GATEWAY_URL=http://<slurm-host-ip>:18090
EOF
  echo "added PILOT107_REMOTE_COMMAND_GATEWAY_URL placeholder to $env_file"
fi

remote_gateway="$(awk -F= '/^PILOT107_REMOTE_COMMAND_GATEWAY_URL=/{print $2}' "$env_file" | tail -1)"
if [[ -z "$remote_gateway" || "$remote_gateway" == *"<slurm-host-ip>"* ]]; then
  echo "edit $env_file and set PILOT107_REMOTE_COMMAND_GATEWAY_URL before starting app node" >&2
  exit 2
fi

cert_dir="$compose_dir/certs"
mkdir -p "$cert_dir"
if [[ ! -f "$cert_dir/tls.crt" || ! -f "$cert_dir/tls.key" ]]; then
  openssl req \
    -x509 \
    -newkey rsa:2048 \
    -nodes \
    -days 30 \
    -subj "/CN=localhost" \
    -keyout "$cert_dir/tls.key" \
    -out "$cert_dir/tls.crt" >/dev/null 2>&1
  chmod 0644 "$cert_dir/tls.key"
  chmod 0644 "$cert_dir/tls.crt"
  echo "generated self-signed TLS certificate under $cert_dir"
fi
chmod 0644 "$cert_dir/tls.key" "$cert_dir/tls.crt"

if [[ "${PILOT107_SKIP_BUILD:-0}" != "1" ]]; then
  bash "$root/scripts/build-app-images.sh"
fi

compose=(
  docker compose
  --env-file "$env_file"
  -f "$compose_dir/compose.competition-app-node.yml"
)

"${compose[@]}" up -d \
  pilot107-api \
  pilot107-worker \
  pilot107-web \
  pilot107-reverse-proxy

wait_healthy() {
  local service="$1"
  for _ in $(seq 1 60); do
    local container_id
    container_id="$("${compose[@]}" ps -q "$service")"
    if [[ -n "$container_id" ]]; then
      local status
      status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id")"
      if [[ "$status" == "healthy" || "$status" == "none" ]]; then
        return 0
      fi
    fi
    sleep 2
  done
  "${compose[@]}" logs --tail=100 "$service" >&2
  echo "$service did not become healthy" >&2
  return 1
}

wait_healthy pilot107-api
wait_healthy pilot107-worker
wait_healthy pilot107-web
wait_healthy pilot107-reverse-proxy

https_port="$(awk -F= '/^PILOT107_HTTPS_PORT=/{print $2}' "$env_file" | tail -1)"
https_port="${https_port:-8443}"
echo "competition app node is running: https://127.0.0.1:${https_port}/"
