"""Shared helpers for simulator REST probe/smoke scripts.

These helpers are standalone (not production code): they shell out to
``docker exec`` to mint a real ``scontrol token`` JWT and auto-detect the
simulator's published REST URL. They NEVER persist the token — callers must
keep it in a local variable only.
"""

from __future__ import annotations

import os
import re
import subprocess

SLURMCTLD_CONTAINER = "pilot107-sim-slurmctld-1"
SLURMRESTD_CONTAINER = "pilot107-sim-slurmrestd-1"
DEFAULT_API_VERSION = os.environ.get("PILOT107_SIM_REST_API_VERSION", "v0.0.41")
DEFAULT_REST_USER = os.environ.get("PILOT107_SIM_REST_USER", "alice")
# Probes must not write the token anywhere; this is just the default lifespan.
DEFAULT_TOKEN_LIFESPAN = int(os.environ.get("PILOT107_SIM_REST_TOKEN_LIFESPAN", "600"))


def detect_sim_rest_url() -> str:
    """Return the simulator slurmrestd base URL as seen from the host.

    Order of resolution:

    1. ``PILOT107_SIM_REST_URL`` env var (explicit override).
    2. ``docker port pilot107-sim-slurmrestd-1 6820/tcp`` (auto-detect the
       published host port — the competition env binds 6820 -> 16820).
    3. Fallback ``http://127.0.0.1:6820``.
    """
    explicit = os.environ.get("PILOT107_SIM_REST_URL")
    if explicit:
        return explicit.rstrip("/")
    try:
        result = subprocess.run(
            ["docker", "port", SLURMRESTD_CONTAINER, "6820/tcp"],
            check=False,
            text=True,
            capture_output=True,
            timeout=5,
        )
        line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        # Output looks like "0.0.0.0:16820" or "127.0.0.1:16820".
        match = re.match(r"^([\d.]+):(\d+)$", line)
        if match:
            host, port = match.group(1), match.group(2)
            if host == "0.0.0.0":
                host = "127.0.0.1"
            return f"http://{host}:{port}"
    except (OSError, subprocess.SubprocessError):
        pass
    return "http://127.0.0.1:6820"


def mint_sim_token(
    *,
    user: str = DEFAULT_REST_USER,
    lifespan_seconds: int = DEFAULT_TOKEN_LIFESPAN,
) -> str:
    """Mint a real JWT via ``scontrol token`` inside the slurmctld container.

    Runs ``scontrol token`` as ``user`` so the JWT ``sun`` claim matches the
    ``X-SLURM-USER-NAME`` header the caller will send. The token is returned
    ONLY; it is never printed, logged, or written to disk by this function.
    """
    # Run as `user` via `su -l <user> -c` so scontrol emits sun=<user>.
    command = f"su -l {user} -c 'scontrol token lifespan={lifespan_seconds}'"
    result = subprocess.run(
        ["docker", "exec", SLURMCTLD_CONTAINER, "bash", "-lc", command],
        check=False,
        text=True,
        capture_output=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"scontrol token failed (rc={result.returncode}); "
            "see simulator logs (token intentionally not included)"
        )
    for line in (result.stdout + "\n" + result.stderr).splitlines():
        stripped = line.strip()
        if stripped.startswith("SLURM_JWT="):
            token = stripped[len("SLURM_JWT="):].strip()
            if token:
                return token
    raise RuntimeError("scontrol token output did not contain SLURM_JWT=")
