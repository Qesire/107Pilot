#!/usr/bin/env bash
#
# Start, check, or stop the MFA-authenticated SSH ControlMaster used only by
# the opt-in read-only code-context transport.  This intentionally runs the
# master *inside* pilot107-api so the API and the master share UID 10700 and
# the socket is never exposed on a host TCP port.

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

# The Python snippet receives only an operator-selected action.  Target, port,
# and socket path come from the API container environment; no secret is echoed
# by this script and no reconnect loop is attempted after MFA expires.
"${compose[@]}" exec pilot107-api python3 - "$action" <<'PY'
import os
import subprocess
import sys
from pathlib import Path

action = sys.argv[1]
target = os.environ.get("PILOT107_CODE_CONTEXT_SSH_TARGET", "").strip()
socket = os.environ.get(
    "PILOT107_CODE_CONTEXT_SSH_CONTROL_PATH", "/var/lib/pilot107/ssh/real107.sock"
).strip()
port = os.environ.get("PILOT107_CODE_CONTEXT_SSH_PORT", "22").strip()
if not target:
    raise SystemExit("PILOT107_CODE_CONTEXT_SSH_TARGET is unset")
if not socket.startswith("/") or "\x00" in socket:
    raise SystemExit("PILOT107_CODE_CONTEXT_SSH_CONTROL_PATH must be an absolute path")
try:
    port_number = int(port)
except ValueError as exc:
    raise SystemExit("PILOT107_CODE_CONTEXT_SSH_PORT must be an integer") from exc
if not 1 <= port_number <= 65535:
    raise SystemExit("PILOT107_CODE_CONTEXT_SSH_PORT is outside the valid range")

socket_path = Path(socket)
base = ["ssh", "-S", str(socket_path), "-p", str(port_number), target]
if action == "status":
    raise SystemExit(subprocess.run([*base[:-1], "-O", "check", target], check=False).returncode)
if action == "stop":
    raise SystemExit(subprocess.run([*base[:-1], "-O", "exit", target], check=False).returncode)

known_hosts = socket_path.parent / "known_hosts"
if not known_hosts.is_file():
    raise SystemExit(
        f"missing {known_hosts}; verify and install the remote host key before starting the master"
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
raise SystemExit(subprocess.run(command, check=False).returncode)
PY
