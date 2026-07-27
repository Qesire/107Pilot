#!/usr/bin/env bash
#
# Start, check, or stop the owner-bound ControlMaster used by the formal
# real107-ssh API and Worker backend. Authentication remains interactive.

set -euo pipefail

action="${1:-status}"
if [[ "$action" != "start" && "$action" != "status" && "$action" != "stop" ]]; then
  echo "usage: $0 {start|status|stop}" >&2
  exit 2
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

compose=(docker compose)
if [[ -n "${PILOT107_CPU_RC_PROJECT_NAME:-}" ]]; then
  compose+=(--project-name "$PILOT107_CPU_RC_PROJECT_NAME")
fi
if [[ -n "${PILOT107_COMPOSE_ENV_FILE:-}" ]]; then
  compose+=(--env-file "$PILOT107_COMPOSE_ENV_FILE")
fi
if [[ -n "${PILOT107_COMPOSE_FILES:-}" ]]; then
  IFS=':' read -r -a compose_files <<<"$PILOT107_COMPOSE_FILES"
  for compose_file in "${compose_files[@]}"; do
    compose+=(-f "$compose_file")
  done
fi
if [[ -n "${PILOT107_COMPOSE_PROFILE:-}" ]]; then
  compose+=(--profile "$PILOT107_COMPOSE_PROFILE")
fi

"${compose[@]}" exec pilot107-api python3 - "$action" <<'PY'
import os
import re
import subprocess
import sys
from pathlib import Path

action = sys.argv[1]
target = os.environ.get("PILOT107_SSH_TARGET", "").strip()
socket = os.environ.get(
    "PILOT107_SSH_CONTROL_PATH", "/var/lib/pilot107/ssh/real107.sock"
).strip()
known_hosts_value = os.environ.get(
    "PILOT107_SSH_KNOWN_HOSTS_FILE", "/var/lib/pilot107/ssh/known_hosts"
).strip()
port = os.environ.get("PILOT107_SSH_PORT", "22").strip()
expected_user = os.environ.get("PILOT107_SSH_SLURM_USER", "").strip()

if not target or not re.fullmatch(r"[A-Za-z0-9_.:@%+-]+", target):
    raise SystemExit("PILOT107_SSH_TARGET is unset or unsafe")
if not socket.startswith("/") or "\x00" in socket:
    raise SystemExit("PILOT107_SSH_CONTROL_PATH must be an absolute path")
if not known_hosts_value.startswith("/") or "\x00" in known_hosts_value:
    raise SystemExit("PILOT107_SSH_KNOWN_HOSTS_FILE must be an absolute path")
if not expected_user or not re.fullmatch(r"[A-Za-z0-9_.-]+", expected_user):
    raise SystemExit("PILOT107_SSH_SLURM_USER is unset or unsafe")
try:
    port_number = int(port)
except ValueError as exc:
    raise SystemExit("PILOT107_SSH_PORT must be an integer") from exc
if not 1 <= port_number <= 65535:
    raise SystemExit("PILOT107_SSH_PORT is outside the valid range")

socket_path = Path(socket)
known_hosts = Path(known_hosts_value)
base = ["ssh", "-S", str(socket_path), "-p", str(port_number)]
if action == "status":
    raise SystemExit(
        subprocess.run([*base, "-O", "check", target], check=False).returncode
    )
if action == "stop":
    raise SystemExit(
        subprocess.run([*base, "-O", "exit", target], check=False).returncode
    )

if not known_hosts.is_file():
    raise SystemExit(
        f"missing {known_hosts}; independently verify and install the host key first"
    )
socket_path.parent.mkdir(parents=True, exist_ok=True)
command = [
    "ssh",
    "-M",
    "-N",
    "-f",
    "-S",
    str(socket_path),
    "-p",
    str(port_number),
    "-o",
    "ControlMaster=yes",
    "-o",
    "ControlPersist=4h",
    "-o",
    "ServerAliveInterval=30",
    "-o",
    "ServerAliveCountMax=3",
    "-o",
    "StrictHostKeyChecking=yes",
    "-o",
    f"UserKnownHostsFile={known_hosts}",
    target,
]
started = subprocess.run(command, check=False)
if started.returncode != 0:
    raise SystemExit(started.returncode)
identity = subprocess.run(
    [
        *base,
        "-o",
        "BatchMode=yes",
        "-o",
        "ControlMaster=no",
        target,
        "--",
        "whoami",
    ],
    check=False,
    text=True,
    capture_output=True,
)
if identity.returncode != 0 or identity.stdout.strip() != expected_user:
    subprocess.run([*base, "-O", "exit", target], check=False)
    raise SystemExit("authenticated SSH identity does not match PILOT107_SSH_SLURM_USER")
print("real107 SSH master active for the configured Slurm identity")
PY
