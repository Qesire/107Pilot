#!/usr/bin/env bash
#
# Install (or uninstall) a 107Pilot systemd unit for the cpu-rc or competition
# stack on a host VM. The unit templates under scripts/systemd/ contain the
# placeholder __PILOT107_REPO_ROOT__; this script substitutes the real repo
# path into a temp copy and installs the result to /etc/systemd/system/.
#
# Usage:
#   sudo bash scripts/install-systemd-units.sh install <profile> [repo-path]
#   sudo bash scripts/install-systemd-units.sh uninstall <profile>
#
#   profile   cpu-rc | competition
#   repo-path absolute path to the repo checkout on the VM (default: current
#             working directory)
#
# The script creates /etc/pilot107/<profile>.env (mode 0600, owner root) on
# install if it does not already exist, with a PILOT107_PUBLIC_URL= placeholder
# plus a comment listing the vars start-<profile>.sh consumes. Operators must
# fill in PILOT107_PUBLIC_URL before `systemctl start`.
set -euo pipefail

usage() {
  cat >&2 <<EOF
Usage:
  $0 install <cpu-rc|competition> [repo-path]
  $0 uninstall <cpu-rc|competition>
EOF
  exit 2
}

unit_name_for() {
  case "$1" in
    cpu-rc)       echo "pilot107-cpu-rc" ;;
    competition)  echo "pilot107-competition" ;;
    *)            return 1 ;;
  esac
}

env_required_vars_comment() {
  case "$1" in
    cpu-rc)
      cat <<'EOF'
# Environment for the pilot107-cpu-rc systemd unit.
# start-cpu-rc.sh reads PILOT107_PUBLIC_URL from the environment; the unit's
# EnvironmentFile directive points here, so this file is the single source for
# that value. Do NOT commit it; it lives outside the repo at
# /etc/pilot107/cpu-rc.env with mode 0600.
#
# Required:
#   PILOT107_PUBLIC_URL   Full public origin the browser uses to reach the
#                         deployment, e.g. https://pilot.example.edu:8443
#                         (scheme://host:port; no trailing path). The BFF CSRF
#                         origin check compares against this.
# Optional (override script defaults):
#   PILOT107_CPU_RC_PROJECT_NAME     compose project name (default pilot107-cpu-rc)
#   PILOT107_CPU_RC_ENV_FILE         path to compose env file
#   PILOT107_SKIP_BUILD=1            skip image build on start
#   PILOT107_SKIP_ORIGIN_VALIDATE=1  skip the BFF CSRF origin probe
EOF
      ;;
    competition)
      cat <<'EOF'
# Environment for the pilot107-competition systemd unit.
# start-competition.sh does not strictly require PILOT107_PUBLIC_URL (unlike
# cpu-rc), but this file is the single source for any operator-supplied
# overrides. Do NOT commit it; it lives outside the repo at
# /etc/pilot107/competition.env with mode 0600.
#
# Optional (override script defaults):
#   PILOT107_COMPETITION_ENV_FILE  path to compose env file
#   PILOT107_SKIP_BUILD=1          skip image build on start
#   PILOT107_PUBLIC_URL            full public origin (scheme://host:port)
EOF
      ;;
  esac
}

write_env_template() {
  local env_path="$1" profile="$2"
  {
    env_required_vars_comment "$profile"
    printf '\nPILOT107_PUBLIC_URL=\n'
  } >"$env_path"
}

cmd_install() {
  local profile="${1:-}"
  local repo_path="${2:-$(pwd)}"
  [[ -n "$profile" ]] || usage
  local unit_name
  unit_name="$(unit_name_for "$profile")" || usage

  if [[ "$EUID" -ne 0 ]]; then
    echo "install must run as root (need /etc/systemd/system and /etc/pilot107)" >&2
    exit 1
  fi

  repo_path="$(cd "$repo_path" 2>/dev/null && pwd -P)" || {
    echo "repo path not found or not a directory: ${2:-$(pwd)}" >&2
    exit 1
  }
  # Sanity check: a start script for the chosen profile must exist.
  local start_script
  case "$profile" in
    cpu-rc)      start_script="start-cpu-rc.sh" ;;
    competition) start_script="start-competition.sh" ;;
  esac
  [[ -f "$repo_path/scripts/$start_script" ]] || {
    echo "expected $repo_path/scripts/$start_script not found" >&2
    exit 1
  }

  local template="$repo_path/scripts/systemd/${unit_name}.service"
  [[ -f "$template" ]] || { echo "template not found: $template" >&2; exit 1; }

  install -d -m 0755 /etc/pilot107
  local env_path="/etc/pilot107/${profile}.env"
  if [[ -f "$env_path" ]]; then
    echo "existing env file preserved: $env_path"
  else
    write_env_template "$env_path" "$profile"
    chmod 0600 "$env_path"
    chown root:root "$env_path"
    echo "created env template: $env_path (edit PILOT107_PUBLIC_URL before start)"
  fi

  local tmp
  tmp="$(mktemp)"
  local cleanup_command
  printf -v cleanup_command 'rm -f -- %q' "$tmp"
  trap "$cleanup_command" EXIT
  # Substitute the repo-root placeholder into a temp copy; do not mutate the
  # in-repo template.
  sed "s|__PILOT107_REPO_ROOT__|${repo_path}|g" "$template" >"$tmp"
  install -m 0644 "$tmp" "/etc/systemd/system/${unit_name}.service"
  rm -f -- "$tmp"
  trap - EXIT

  systemctl daemon-reload
  systemctl enable "${unit_name}.service"

  echo
  echo "Installed: /etc/systemd/system/${unit_name}.service"
  echo "Env file:  $env_path"
  echo
  echo "Next steps:"
  echo "  1. Edit $env_path and set PILOT107_PUBLIC_URL (scheme://host:port)."
  echo "  2. systemctl start ${unit_name}.service"
  echo "  3. systemctl status ${unit_name}.service"
  echo "  4. journalctl -u ${unit_name}.service -f"
  echo
  echo "The unit is enabled, so a VM reboot will auto-bring the stack up via"
  echo "systemd (replacing the previous manual 'docker update --restart' workaround)."
}

cmd_uninstall() {
  local profile="${1:-}"
  [[ -n "$profile" ]] || usage
  local unit_name
  unit_name="$(unit_name_for "$profile")" || usage

  if [[ "$EUID" -ne 0 ]]; then
    echo "uninstall must run as root" >&2
    exit 1
  fi

  local env_path="/etc/pilot107/${profile}.env"
  local unit_path="/etc/systemd/system/${unit_name}.service"

  echo "About to uninstall:"
  echo "  unit: $unit_path"
  echo "  env:  $env_path"
  printf 'Proceed? [y/N] '
  read -r reply
  if [[ "${reply:-}" != "y" && "${reply:-}" != "Y" ]]; then
    echo "aborted"
    exit 0
  fi

  if systemctl is-active --quiet "${unit_name}.service" 2>/dev/null; then
    echo "stopping ${unit_name}.service (runs docker compose down)..."
    systemctl stop "${unit_name}.service" || true
  fi
  systemctl disable "${unit_name}.service" 2>/dev/null || true

  rm -f "$unit_path"
  if [[ -f "$env_path" ]]; then
    rm -f "$env_path"
    echo "removed env file: $env_path"
  fi
  systemctl daemon-reload

  echo "Uninstalled ${unit_name}. Re-run install to redeploy."
}

main() {
  local subcmd="${1:-}"
  shift || true
  case "$subcmd" in
    install)   cmd_install "${1:-}" "${2:-}" ;;
    uninstall) cmd_uninstall "${1:-}" ;;
    *)         usage ;;
  esac
}

main "$@"
