#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_dir="$root/simulator/compose"
env_file="${PILOT107_CPU_RC_ENV_FILE:-$compose_dir/.env.cpu-rc}"
template="$compose_dir/.env.cpu-rc.example"
project_name="${PILOT107_CPU_RC_PROJECT_NAME:-pilot107-cpu-rc}"

if [[ ! -f "$env_file" ]]; then
  cp "$template" "$env_file"
  root_password="$(openssl rand -hex 24)"
  slurm_password="$(openssl rand -hex 24)"
  jwt="$(openssl rand -hex 32)"
  gateway_token="$(openssl rand -hex 32)"
  sed -i \
    -e "s/REPLACE_WITH_RANDOM_ROOT_PASSWORD/$root_password/" \
    -e "s/REPLACE_WITH_RANDOM_SLURM_PASSWORD/$slurm_password/" \
    -e "s/REPLACE_WITH_RANDOM_JWT/$jwt/" \
    -e "s/REPLACE_WITH_RANDOM_GATEWAY_TOKEN/$gateway_token/" \
    "$env_file"
  chmod 0600 "$env_file"
  echo "created $env_file with generated local credentials"
fi
if grep -q 'REPLACE_WITH_' "$env_file"; then
  echo "refusing to start with placeholder credentials in $env_file" >&2
  exit 1
fi

bash "$root/scripts/init-local-secrets.sh"
secret_gid="$(stat -c '%g' "$compose_dir/secrets/proxy-hmac.local")"
sed -i -e "s/^PILOT107_SECRET_GID=.*/PILOT107_SECRET_GID=$secret_gid/" "$env_file"
slurm_password="$(awk -F= '$1 == "MARIADB_PASSWORD" {print substr($0, index($0, "=") + 1)}' "$env_file")"
if [[ -z "$slurm_password" || "$slurm_password" == *$'\n'* ]]; then
  echo "invalid MARIADB_PASSWORD in $env_file" >&2
  exit 1
fi
slurmdbd_conf="$compose_dir/secrets/slurmdbd-cpu-rc.conf"
umask 077
rm -f "$slurmdbd_conf"
{
  printf '%s\n' \
    'AuthType=auth/munge' \
    'AuthAltTypes=auth/jwt' \
    'AuthAltParameters=jwt_key=/etc/slurm/jwt_hs256.key' \
    'AuthInfo=/var/run/munge/munge.socket.2' \
    'SlurmUser=slurm' \
    'DebugLevel=verbose' \
    'LogFile=/var/log/slurm/slurmdbd.log' \
    'PidFile=/var/run/slurm/slurmdbd.pid' \
    'StorageType=accounting_storage/mysql' \
    'StorageHost=mariadb' \
    'StoragePort=3306' \
    'StorageUser=slurm'
  printf 'StoragePass=%s\n' "$slurm_password"
  printf '%s\n' 'StorageLoc=slurm_acct_db'
} >"$slurmdbd_conf"
chmod 0600 "$slurmdbd_conf"

cert_dir="$compose_dir/certs"
mkdir -p "$cert_dir"
if [[ ! -f "$cert_dir/tls.crt" || ! -f "$cert_dir/tls.key" ]]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=localhost" \
    -keyout "$cert_dir/tls.key" -out "$cert_dir/tls.crt" >/dev/null 2>&1
fi
chmod 0644 "$cert_dir/tls.crt" "$cert_dir/tls.key"

if [[ "${PILOT107_SKIP_BUILD:-0}" != "1" ]]; then
  bash "$root/scripts/build-cpu-rc-images.sh"
fi

compose=(
  docker compose
  --project-name "$project_name"
  --env-file "$env_file"
  -f "$compose_dir/compose.yml"
  -f "$compose_dir/compose.competition.yml"
  -f "$compose_dir/compose.cpu-rc.yml"
  --profile competition
)

"${compose[@]}" up -d mariadb slurmdbd slurmctld worker-1
bash "$root/scripts/apply-cpu-rc-profile.sh"
"${compose[@]}" up -d \
  slurmrestd pilot107-command-gateway pilot107-api pilot107-worker \
  pilot107-web pilot107-reverse-proxy

for service in mariadb pilot107-command-gateway pilot107-api pilot107-worker pilot107-web pilot107-reverse-proxy; do
  for _ in $(seq 1 60); do
    container_id="$("${compose[@]}" ps --all -q "$service")"
    if [[ -z "$container_id" ]]; then
      sleep 2
      continue
    fi
    running="$(docker inspect --format '{{.State.Running}}' "$container_id")"
    if [[ "$running" != "true" ]]; then
      "${compose[@]}" logs --tail=100 "$service" >&2
      echo "$service exited before becoming healthy" >&2
      exit 1
    fi
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id")"
    if [[ "$status" == "healthy" || "$status" == "none" ]]; then
      break
    fi
    sleep 2
  done
  if [[ "$status" != "healthy" && "$status" != "none" ]]; then
    "${compose[@]}" logs --tail=100 "$service" >&2
    exit 1
  fi
done

https_port="$(awk -F= '/^PILOT107_HTTPS_PORT=/{print $2}' "$env_file" | tail -1)"
echo "CPU-only 8C/16G release candidate is running: https://127.0.0.1:${https_port:-8443}/"
