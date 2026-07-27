# SSH code-context transport

107Pilot can optionally obtain source windows from the worktree that actually
executes a Run. The same authenticated transport now also powers the formal
`real107-ssh` Slurm backend and remote Evidence collector. Agent source reads
remain read-only and fail closed; neither path stores an SSH password, OTP, or
private key.

## Enable on the 107Pilot VM

Add these values to the VM's untracked `.env.cpu-rc` (or deployment-specific
environment file).  Use the exact project root, not a broad home directory.

```dotenv
PILOT107_CODE_CONTEXT_TRANSPORT=ssh
PILOT107_CODE_CONTEXT_ALLOWED_ROOTS=/public/home/pb23061276/project-name
PILOT107_CODE_CONTEXT_SSH_TARGET=pb23061276@114.214.255.132
PILOT107_CODE_CONTEXT_SSH_CONTROL_PATH=/var/lib/pilot107/ssh/real107.sock
PILOT107_CODE_CONTEXT_SSH_PORT=22
```

The API and Worker images contain only an SSH client. The authenticated control
socket lives in their shared persistent `/var/lib/pilot107` volume and is owned
by the same unprivileged `pilot107` UID used by both processes.

Before starting a connection, put a host key that has been verified through an
independent channel in this path inside `pilot107-api`:

```text
/var/lib/pilot107/ssh/known_hosts
```

Do not use `StrictHostKeyChecking=no`, and do not copy the operator's private
key, password, or Google Authenticator value into Docker, `.env`, or the
database.

For the full Slurm backend, set the owner-bound values and include the formal
overlay:

```dotenv
PILOT107_SSH_TARGET=pb23061276@114.214.255.132
PILOT107_SSH_PORTAL_OWNER=pb23061276
PILOT107_SSH_SLURM_USER=pb23061276
PILOT107_SSH_OWNER_ROOTS=/home/scc/pb23061276/private-107pilot
```

Then run the helper from the project root:

```bash
PILOT107_CPU_RC_PROJECT_NAME=pilot107-cpu-rc \
PILOT107_COMPOSE_ENV_FILE=simulator/compose/.env.cpu-rc \
PILOT107_COMPOSE_FILES=simulator/compose/compose.yml:simulator/compose/compose.competition.yml:simulator/compose/compose.cpu-rc.yml:simulator/compose/compose.real107-ssh.yml \
PILOT107_COMPOSE_PROFILE=competition \
bash scripts/manage-real107-ssh.sh start
```

`start` keeps the interactive terminal attached long enough for the operator
to perform Google Authenticator verification, then backgrounds an SSH
ControlMaster with a 30-second keepalive.  Check or deliberately end it with:

```bash
bash scripts/manage-real107-ssh.sh status
bash scripts/manage-real107-ssh.sh stop
```

The helper verifies that remote `whoami` equals the configured Slurm identity.
It deliberately does not reconnect after a network loss or container restart:
another MFA challenge requires an explicit operator action. While the master is
absent, the connection API reports `auth_required`; Agent source reads return
`ssh_auth_required`, and remote Run/Worker calls fail closed without trying an
interactive authentication flow.

## What can be read

Only fixed projections are allowed: repository `HEAD`, porcelain status,
tracked-file manifest, and selected regular source files below the configured
root.  A traceback or compiler location selects at most three source windows
by default.  `.git`, `.env*`, key/certificate files, generated dependency
trees, paths outside the root, and symlinks are refused.

Every returned code chunk is bound to a `codesnap_*` worktree fingerprint and
a `code://` reference.  The current implementation has no remote write,
checkout, build, patch-apply, or job-submit operation.  Hotfix creation must
remain a separately approved workflow that applies an Agent-proposed diff in
an isolated worktree and records the resulting package manifest.
