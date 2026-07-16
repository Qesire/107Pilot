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
  bash "$root/scripts/build-slurm-sim-image.sh"
  bash "$root/scripts/build-app-images.sh"
fi

compose=(
  docker compose
  --env-file "$env_file"
  -f "$compose_dir/compose.yml"
  -f "$compose_dir/compose.competition.yml"
  --profile competition
)

"${compose[@]}" up -d \
  mariadb \
  slurmdbd \
  slurmctld \
  slurmrestd \
  worker-1 \
  worker-2 \
  pilot107-command-gateway \
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

wait_healthy mariadb
wait_healthy pilot107-command-gateway
wait_healthy pilot107-api
wait_healthy pilot107-worker
wait_healthy pilot107-web
wait_healthy pilot107-reverse-proxy

https_port="$(awk -F= '/^PILOT107_HTTPS_PORT=/{print $2}' "$env_file" | tail -1)"
https_port="${https_port:-8443}"
echo "competition profile is running: https://127.0.0.1:${https_port}/"
