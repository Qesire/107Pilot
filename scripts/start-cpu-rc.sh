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

# The BFF CSRF origin check compares the browser's Origin header against
# PILOT107_WEB_PUBLIC_ORIGIN. The operator must supply the full public origin
# (scheme://host:port) the browser will actually use; do not fall back to a
# hardcoded IP, which would silently deny browser writes.
if [[ -z "${PILOT107_PUBLIC_URL:-}" ]]; then
  echo "PILOT107_PUBLIC_URL is unset or empty. Set it to the full public origin" >&2
  echo "the browser uses to reach the deployment, e.g. https://pilot.example.edu:8443" >&2
  exit 1
fi
# Derive scheme://host:port (strip any trailing path/query/fragment).
public_origin="$(printf '%s' "$PILOT107_PUBLIC_URL" | sed -E 's#^(https?://[^/]+).*$#\1#')"
if [[ -z "$public_origin" ]]; then
  echo "could not derive origin from PILOT107_PUBLIC_URL=$PILOT107_PUBLIC_URL" >&2
  exit 1
fi

bash "$root/scripts/init-local-secrets.sh"
secret_gid="$(stat -c '%g' "$compose_dir/secrets/proxy-hmac.local")"
sed -i -e "s/^PILOT107_SECRET_GID=.*/PILOT107_SECRET_GID=$secret_gid/" "$env_file"
# Ensure the BFF knows the public origin the browser uses for CSRF checks.
if grep -q '^PILOT107_WEB_PUBLIC_ORIGIN=' "$env_file"; then
  sed -i -e "s|^PILOT107_WEB_PUBLIC_ORIGIN=.*|PILOT107_WEB_PUBLIC_ORIGIN=$public_origin|" "$env_file"
else
  printf '\nPILOT107_WEB_PUBLIC_ORIGIN=%s\n' "$public_origin" >>"$env_file"
fi
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
chmod 0644 "$cert_dir/tls.crt"
chmod 0640 "$cert_dir/tls.key"
# The reverse-proxy runs as UID 10700; let the shared secret GID read the private key.
if [[ -n "${secret_gid:-}" ]]; then
  chgrp "$secret_gid" "$cert_dir/tls.key" 2>/dev/null || true
fi

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

# Confirm slurmrestd (started in this wave) reaches Running before the app
# health loop. Cap at ~30s.
slurmrestd_id=""
for _ in $(seq 1 15); do
  slurmrestd_id="$("${compose[@]}" ps -q slurmrestd | head -n1)"
  if [[ -n "$slurmrestd_id" ]] \
     && [[ "$(docker inspect --format '{{.State.Running}}' "$slurmrestd_id" 2>/dev/null || true)" == "true" ]]; then
    break
  fi
  slurmrestd_id=""
  sleep 2
done
if [[ -z "$slurmrestd_id" ]]; then
  "${compose[@]}" logs --tail=100 slurmrestd >&2 || true
  echo "slurmrestd did not reach Running state" >&2
  exit 1
fi

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

# Slurm partition visibility check (warning only). worker-1 runs slurmd and
# has the Slurm client tools installed; matches the style of apply-cpu-rc-profile.sh.
sinfo_out="$("${compose[@]}" exec -T worker-1 sinfo -h -o '%P' 2>/dev/null || true)"
if [[ -z "${sinfo_out//[[:space:]]/}" ]]; then
  echo "WARNING: sinfo on worker-1 reports no visible partitions; Slurm may not be fully ready." >&2
else
  echo "sinfo partitions visible on worker-1: $(printf '%s' "$sinfo_out" | tr '\n' ' ')"
fi

# Validate the public origin the browser will use against the BFF CSRF check.
# Non-destructive GET only; self-signed certs are accepted. A 403 or
# CSRF.ORIGIN_DENIED means PILOT107_PUBLIC_URL does not match what the BFF
# expects, which would silently break browser writes.
if [[ "${PILOT107_SKIP_ORIGIN_VALIDATE:-0}" != "1" ]]; then
  if ! python3 - "$PILOT107_PUBLIC_URL" <<'PY'; then
import sys, ssl, urllib.error, urllib.request
base = sys.argv[1]
url = base + "/api/v1/health/ready"
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
req = urllib.request.Request(url, headers={"Origin": base})
try:
    with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
        status, body = r.status, r.read().decode("utf-8", "replace")
except urllib.error.HTTPError as e:
    status, body = e.code, e.read().decode("utf-8", "replace")
except Exception as e:
    print("WARNING: origin validation probe could not reach %s: %s" % (url, e), file=sys.stderr)
    sys.exit(0)
if status == 403 or "CSRF.ORIGIN_DENIED" in body:
    print("ERROR: public origin %s is denied by the BFF CSRF origin check (status=%s)." % (base, status), file=sys.stderr)
    print("Set PILOT107_PUBLIC_URL to the origin the browser uses (scheme://host:port).", file=sys.stderr)
    sys.exit(1)
sys.exit(0)
PY
    exit 1
  fi
fi

echo "CPU-only 8C/16G release candidate is running: https://127.0.0.1:${https_port:-8443}/"
