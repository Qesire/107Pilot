"""REST JWT token providers for the Slurm simulator.

The simulator's slurmrestd is started with ``rest_auth/jwt`` and rejects any
request that does not carry a real JWT minted by ``scontrol token``. A token
provider mints that JWT on demand through the simulator command executor and
caches it until shortly before expiry, so the worker/API do not mint a fresh
token per REST call.

Security invariants (see ``docs/phase-1/auth_decision.md``):

* The token is NEVER logged, printed, persisted, or included in error
  messages. ``mint_token`` returns it only to the caller.
* The cache is in-process memory only.

The protocol and the ``StaticTokenProvider`` test double live here so Lane 4b-ii
can wire a concrete provider into ``api/service.py`` / ``worker/service.py``
without importing the simulator implementation.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pilot107.adapters.slurm import SlurmTransportError

if TYPE_CHECKING:
    from pilot107.adapters.slurm import SimulatorExecutor

# Re-mint when the cached token has fewer than this many seconds left, so a
# race between mint and use does not send an already-expired token.
_MIN_REMAINING_SECONDS = 60


class RestTokenProvider(Protocol):
    """Mint a Slurm REST JWT for a user."""

    def mint_token(self, *, user: str, lifespan_seconds: int = 3600) -> str:
        """Return a JWT string for ``user``.

        Implementations should cache and refresh the token internally; callers
        may invoke this on every request without paying a fresh ``scontrol``
        round-trip each time. The returned token MUST NOT be logged by the
        implementation.
        """
        ...


@dataclass
class _CachedToken:
    token: str
    expires_at: float


class SimulatorRestTokenProvider:
    """Mint JWTs by running ``scontrol token`` inside the simulator.

    Uses a ``SimulatorExecutor`` (e.g. ``DockerComposeExecutor``) to run
    ``scontrol token lifespan=<n>`` as ``user`` inside the simulator container.
    The ``scontrol`` binary derives the JWT ``sun`` claim from the invoking
    Unix user, so the command MUST run as ``user`` for slurmrestd to accept the
    resulting token under ``X-SLURM-USER-NAME: <user>``.

    Tokens are cached per user in-memory and re-mint only when the cached token
    has fewer than ``_MIN_REMAINING_SECONDS`` seconds of life left. A
    ``threading.Lock`` guards the cache so concurrent worker threads share one
    mint per expiry window.
    """

    def __init__(
        self,
        *,
        executor: SimulatorExecutor,
        timeout_seconds: float = 10.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._executor = executor
        self._timeout_seconds = timeout_seconds
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._cache: dict[str, _CachedToken] = {}

    def mint_token(self, *, user: str, lifespan_seconds: int = 3600) -> str:
        if lifespan_seconds <= 0:
            raise SlurmTransportError(f"lifespan_seconds must be positive: {lifespan_seconds}")
        now = self._clock()
        with self._lock:
            cached = self._cache.get(user)
            if cached is not None and (cached.expires_at - now) > _MIN_REMAINING_SECONDS:
                return cached.token
            token = self._mint_uncached(user=user, lifespan_seconds=lifespan_seconds)
            self._cache[user] = _CachedToken(
                token=token,
                expires_at=now + lifespan_seconds,
            )
            return token

    def _mint_uncached(self, *, user: str, lifespan_seconds: int) -> str:
        result = self._executor.run(
            ["scontrol", "token", f"lifespan={lifespan_seconds}"],
            user=user,
            timeout_seconds=self._timeout_seconds,
        )
        if result.returncode != 0:
            # Intentionally do NOT include stdout/stderr: scontrol has been
            # observed to echo the token to stderr on some builds.
            raise SlurmTransportError(
                f"scontrol token failed for user {user} (rc={result.returncode})"
            )
        token = _parse_slurm_jwt(result.stdout)
        if not token:
            raise SlurmTransportError(
                f"scontrol token output for user {user} did not contain SLURM_JWT="
            )
        return token


def _parse_slurm_jwt(stdout: str) -> str | None:
    """Extract the JWT from ``scontrol token`` output.

    ``scontrol token lifespan=N`` prints a single line of the form
    ``SLURM_JWT=<jwt>``. We match the prefix on any line and return the value
    verbatim. We deliberately avoid JWT structure validation here: slurmrestd
    is the authority, and parsing the JWT would risk leaking the token into a
    log line.
    """
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("SLURM_JWT="):
            value = stripped[len("SLURM_JWT="):].strip()
            if value:
                return value
    return None


class StaticTokenProvider:
    """Test double that always returns a pre-set token.

    Useful for unit tests and for probes that want to inject a token minted
    out-of-band (e.g. by shelling out to ``docker exec`` directly).
    """

    def __init__(self, token: str) -> None:
        self._token = token

    def mint_token(self, *, user: str, lifespan_seconds: int = 3600) -> str:
        del user, lifespan_seconds  # static
        return self._token
