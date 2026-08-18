#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
secret_dir="$root/simulator/compose/secrets"

install -d -m 0700 "$secret_dir"
for secret_name in proxy-hmac.local agent-capability-hmac.local; do
  secret_path="$secret_dir/$secret_name"
  if [[ ! -f "$secret_path" ]]; then
    umask 077
    openssl rand -hex 32 >"$secret_path"
  fi
  chmod 0640 "$secret_path"
  printf 'Local HMAC secret is ready at %s\n' "$secret_path"
done
