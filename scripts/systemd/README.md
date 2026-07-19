# 107Pilot systemd units

systemd service templates that bring a 107Pilot stack (cpu-rc release
candidate or competition) up on VM boot via `docker compose`, replacing the
manual `docker update --restart=unless-stopped` workaround used on the S1 VM.
The compose definitions already set per-container restart policies, so the
unit's job is just "bring the stack up on boot, tear it down on shutdown".

## Files

| File | Purpose |
| --- | --- |
| `pilot107-cpu-rc.service` | Template unit for the cpu-RC stack |
| `pilot107-competition.service` | Template unit for the competition stack |
| `../install-systemd-units.sh` | Install / uninstall the templates on a VM |

Both templates contain the literal placeholder `__PILOT107_REPO_ROOT__`. The
install script substitutes the real repo path into a temp copy (the in-repo
template is never mutated) and writes the result to
`/etc/systemd/system/<unit>.service`.

## What the units do

- `Type=oneshot` + `RemainAfterExit=yes` — the canonical pattern for a
  `docker compose up -d` wrapper. The unit becomes "active" once the stack is
  up and stays active while it runs.
- `ExecStart` → `scripts/start-cpu-rc.sh` (or `start-competition.sh`). These
  scripts build images (unless `PILOT107_SKIP_BUILD=1`), run
  `docker compose up -d`, and poll healthchecks before returning.
- `ExecStop` → `scripts/stop-cpu-rc.sh` (or `stop-competition.sh`), which runs
  `docker compose -p <project> -f ... down` against the same project name and
  overlays used by `ExecStart`, so the unit tears down exactly what it brought
  up.
- `Wants=`/`After=` `network-online.target docker.service` so the daemon and
  networking are ready before compose starts.
- `Restart=on-failure RestartSec=10` to absorb transient docker-daemon hiccups
  during boot. Once the stack is up, per-container restart policies in compose
  handle individual container crashes; the unit itself stays active.
- `TimeoutStartSec=20min` because cold first-run image builds can take several
  minutes; the systemd default of 90s is too short.
- `EnvironmentFile=/etc/pilot107/<profile>.env` is the single source for
  `PILOT107_PUBLIC_URL` and any operator overrides. No IP or secret is baked
  into the unit.

| Profile | Compose invocation |
| --- | --- |
| cpu-rc | `docker compose -p pilot107-cpu-rc --env-file .env.cpu-rc -f compose.yml -f compose.competition.yml -f compose.cpu-rc.yml --profile competition up -d` |
| competition | `docker compose --env-file .env.competition -f compose.yml -f compose.competition.yml --profile competition up -d` |

The project name for cpu-rc is `pilot107-cpu-rc` (the default of
`PILOT107_CPU_RC_PROJECT_NAME`). The competition profile does not set an
explicit project name, so docker compose derives it from the repo directory.

## Install

```sh
# cpu-RC, repo checked out at /opt/107pilot
sudo bash scripts/install-systemd-units.sh install cpu-rc /opt/107pilot

# competition, repo in the current directory
sudo bash scripts/install-systemd-units.sh install competition
```

The script will:

1. Resolve the repo path to an absolute path (default: current directory).
2. Create `/etc/pilot107/<profile>.env` (mode 0600, owner root) from a
   template if it does not already exist. The template lists the required and
   optional variables for that profile.
3. Substitute `__PILOT107_REPO_ROOT__` into a temp copy of the unit template
   and install it to `/etc/systemd/system/<unit>.service` (mode 0644).
4. Run `systemctl daemon-reload` and `systemctl enable <unit>`.

After install, edit the env file and start the unit:

```sh
sudo $EDITOR /etc/pilot107/cpu-rc.env        # set PILOT107_PUBLIC_URL=...
sudo systemctl start pilot107-cpu-rc.service
```

### Required environment variables

`/etc/pilot107/cpu-rc.env`:

| Variable | Required | Notes |
| --- | --- | --- |
| `PILOT107_PUBLIC_URL` | **yes** | Full public origin the browser uses, e.g. `https://pilot.example.edu:8443`. The BFF CSRF origin check compares against this; `start-cpu-rc.sh` refuses to run without it. |
| `PILOT107_CPU_RC_PROJECT_NAME` | no | Override compose project name (default `pilot107-cpu-rc`). |
| `PILOT107_CPU_RC_ENV_FILE` | no | Override path to compose env file. |
| `PILOT107_SKIP_BUILD=1` | no | Skip image build on start. |
| `PILOT107_SKIP_ORIGIN_VALIDATE=1` | no | Skip the BFF CSRF origin probe. |

`/etc/pilot107/competition.env`:

| Variable | Required | Notes |
| --- | --- | --- |
| `PILOT107_PUBLIC_URL` | no | `start-competition.sh` does not enforce it, but you should set it for a browser-facing deployment. |
| `PILOT107_COMPETITION_ENV_FILE` | no | Override path to compose env file. |
| `PILOT107_SKIP_BUILD=1` | no | Skip image build on start. |

## Status & logs

```sh
systemctl status pilot107-cpu-rc.service
journalctl -u pilot107-cpu-rc.service -f          # follow
journalctl -u pilot107-cpu-rc.service --since today
docker compose -p pilot107-cpu-rc ps              # per-container status
```

For competition, substitute `pilot107-competition` / drop the `-p` flag.

## Uninstall

```sh
sudo bash scripts/install-systemd-units.sh uninstall cpu-rc
```

This stops the unit (which runs `docker compose down`), disables it, removes
`/etc/systemd/system/<unit>.service` and `/etc/pilot107/<profile>.env`, and
runs `systemctl daemon-reload`. It prompts for confirmation first.

## Reboot behaviour

`systemctl enable` wires the unit into `multi-user.target`, so a VM reboot
auto-brings the stack up via systemd — the previous manual
`docker update --restart=unless-stopped` workaround is no longer needed.
