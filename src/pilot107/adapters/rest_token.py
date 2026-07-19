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

import base64
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pilot107.adapters.slurm import SlurmTransportError

if TYPE_CHECKING:
    from pilot107.adapters.slurm import SimulatorExecutor

# Default re-mint margin: re-mint when the cached token has fewer than this
# many seconds left, so a race between mint and use does not send an
# already-expired token. Overridable via
# ``PILOT107_SLURM_TOKEN_REFRESH_MARGIN_SECONDS``.
DEFAULT_REFRESH_MARGIN_SECONDS = 60

# A snapshot collection failing repeatedly is one thing; a token we cannot
# re-mint is another. We cap the stored error string so a noisy scontrol
# failure cannot blow up the readiness payload or leak an unexpected token.
_MAX_ERROR_LENGTH = 200


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


@dataclass(frozen=True)
class TokenValidity:
    """Read-only view of token validity for the readiness service.

    ``mode`` is one of ``"simulator-minted"``, ``"externally-managed"`` or
    ``"unknown"``. ``remaining_seconds`` / ``expires_at`` are ``None`` when the
    expiry could not be determined (e.g. an externally-supplied JWT whose
    payload is not parseable). ``last_re_mint_error`` is set only by the
    simulator-minted path; an externally-managed token can never be re-minted
    by this process, so its error stays ``None``.
    """

    mode: str
    externally_managed: bool
    minted_at: float | None
    expires_at: float | None
    remaining_seconds: float | None
    last_re_mint_error: str | None
    refresh_margin_seconds: int


class TokenValidityProbe(Protocol):
    """Read-only probe of Slurm REST token validity for readiness."""

    def validity(self) -> TokenValidity:
        ...


@dataclass
class _CachedToken:
    token: str
    expires_at: float  # monotonic clock deadline
    minted_at_wall: float  # wall-clock mint time (for display)
    expires_at_wall: float  # wall-clock expiry (for display)


def _truncate(text: str) -> str:
    return text if len(text) <= _MAX_ERROR_LENGTH else text[:_MAX_ERROR_LENGTH]


class SimulatorRestTokenProvider:
    """Mint JWTs by running ``scontrol token`` inside the simulator.

    Uses a ``SimulatorExecutor`` (e.g. ``DockerComposeExecutor``) to run
    ``scontrol token lifespan=<n>`` as ``user`` inside the simulator container.
    The ``scontrol`` binary derives the JWT ``sun`` claim from the invoking
    Unix user, so the command MUST run as ``user`` for slurmrestd to accept the
    resulting token under ``X-SLURM-USER-NAME: <user>``.

    Tokens are cached per user in-memory and re-mint only when the cached token
    has fewer than ``refresh_margin_seconds`` seconds of life left. A
    ``threading.Lock`` guards the cache so concurrent worker threads share one
    mint per expiry window.

    If a re-mint fails but the cached token is still valid (not past its
    deadline), the cached token is returned and the error is recorded on
    :meth:`validity` so the readiness check can surface it as DEGRADED without
    failing the in-flight REST call. If the cached token is already expired,
    the error is re-raised so the caller does not send a token slurmrestd will
    reject.
    """

    def __init__(
        self,
        *,
        executor: SimulatorExecutor,
        timeout_seconds: float = 10.0,
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] | None = None,
        refresh_margin_seconds: int = DEFAULT_REFRESH_MARGIN_SECONDS,
    ) -> None:
        self._executor = executor
        self._timeout_seconds = timeout_seconds
        self._clock = clock or time.monotonic
        self._wall_clock = wall_clock or time.time
        if refresh_margin_seconds < 0:
            raise ValueError(
                f"refresh_margin_seconds must be non-negative: {refresh_margin_seconds}"
            )
        self._refresh_margin_seconds = refresh_margin_seconds
        self._lock = threading.Lock()
        self._cache: dict[str, _CachedToken] = {}
        self._last_re_mint_error: str | None = None

    def mint_token(self, *, user: str, lifespan_seconds: int = 3600) -> str:
        if lifespan_seconds <= 0:
            raise SlurmTransportError(f"lifespan_seconds must be positive: {lifespan_seconds}")
        now = self._clock()
        with self._lock:
            cached = self._cache.get(user)
            if cached is not None and (cached.expires_at - now) > self._refresh_margin_seconds:
                return cached.token
            try:
                token = self._mint_uncached(user=user, lifespan_seconds=lifespan_seconds)
            except SlurmTransportError as exc:
                # Record the re-mint failure for the readiness probe. The
                # error string from _mint_uncached deliberately excludes
                # stdout/stderr, so it cannot leak the token.
                self._last_re_mint_error = _truncate(str(exc))
                # Fall back to a still-valid cached token if we have one.
                if cached is not None and cached.expires_at > now:
                    return cached.token
                raise
            self._last_re_mint_error = None
            minted_wall = self._wall_clock()
            self._cache[user] = _CachedToken(
                token=token,
                expires_at=now + lifespan_seconds,
                minted_at_wall=minted_wall,
                expires_at_wall=minted_wall + lifespan_seconds,
            )
            return token

    def validity(self) -> TokenValidity:
        """Report validity of the most-recently-minted cached token.

        The provider caches per-user; for readiness we report the freshest
        cached token across all users (the one with the most remaining life).
        ``remaining_seconds`` uses the monotonic clock so it is unaffected by
        wall-clock adjustments. ``minted_at`` / ``expires_at`` are wall-clock
        unix seconds for display only.
        """
        now = self._clock()
        with self._lock:
            best: _CachedToken | None = None
            for cached in self._cache.values():
                if best is None or cached.expires_at > best.expires_at:
                    best = cached
            if best is None:
                return TokenValidity(
                    mode="simulator-minted",
                    externally_managed=False,
                    minted_at=None,
                    expires_at=None,
                    remaining_seconds=None,
                    last_re_mint_error=self._last_re_mint_error,
                    refresh_margin_seconds=self._refresh_margin_seconds,
                )
            return TokenValidity(
                mode="simulator-minted",
                externally_managed=False,
                minted_at=best.minted_at_wall,
                expires_at=best.expires_at_wall,
                remaining_seconds=max(0.0, best.expires_at - now),
                last_re_mint_error=self._last_re_mint_error,
                refresh_margin_seconds=self._refresh_margin_seconds,
            )

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


def _parse_jwt_exp(token: str) -> float | None:
    """Best-effort extraction of the ``exp`` claim from a JWT.

    Decodes the JWT payload (segment 1) WITHOUT verifying the signature —
    slurmrestd is the signature authority. Only the numeric ``exp`` claim is
    returned; the rest of the payload is discarded and never logged. Returns
    ``None`` if the token is not a parseable JWT with an ``exp`` claim.

    We accept this limited parse only for the externally-managed path (a token
    this process did not mint and therefore cannot re-mint) so the readiness
    check can report remaining lifespan. The token string itself never leaves
    this function and is never included in errors or logs.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload_segment = parts[1]
    # JWT uses base64url without padding.
    padding = "=" * (-len(payload_segment) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload_segment + padding)
        claims = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        return float(exp)
    return None


class ExternalSlurmTokenProbe:
    """Validity probe for an externally pre-minted JWT.

    Used when ``PILOT107_SLURM_TOKEN`` injects a token this process did not
    mint (e.g. operator ran ``scontrol token`` out-of-band). The probe reports
    remaining lifespan if the JWT ``exp`` claim is parseable; otherwise it
    reports ``externally managed`` with unknown expiry. It NEVER re-mints — we
    cannot re-mint a token we did not create.
    """

    def __init__(
        self,
        token: str | None,
        *,
        refresh_margin_seconds: int = DEFAULT_REFRESH_MARGIN_SECONDS,
        wall_clock: Callable[[], float] | None = None,
    ) -> None:
        self._token = token
        if refresh_margin_seconds < 0:
            raise ValueError(
                f"refresh_margin_seconds must be non-negative: {refresh_margin_seconds}"
            )
        self._refresh_margin_seconds = refresh_margin_seconds
        self._wall_clock = wall_clock or time.time

    def validity(self) -> TokenValidity:
        token = self._token
        if not token:
            return TokenValidity(
                mode="externally-managed",
                externally_managed=True,
                minted_at=None,
                expires_at=None,
                remaining_seconds=None,
                last_re_mint_error=None,
                refresh_margin_seconds=self._refresh_margin_seconds,
            )
        exp = _parse_jwt_exp(token)
        if exp is None:
            return TokenValidity(
                mode="externally-managed",
                externally_managed=True,
                minted_at=None,
                expires_at=None,
                remaining_seconds=None,
                last_re_mint_error=None,
                refresh_margin_seconds=self._refresh_margin_seconds,
            )
        now = self._wall_clock()
        return TokenValidity(
            mode="externally-managed",
            externally_managed=True,
            minted_at=None,
            expires_at=exp,
            remaining_seconds=exp - now,
            last_re_mint_error=None,
            refresh_margin_seconds=self._refresh_margin_seconds,
        )


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


__all__ = [
    "DEFAULT_REFRESH_MARGIN_SECONDS",
    "ExternalSlurmTokenProbe",
    "RestTokenProvider",
    "SimulatorRestTokenProvider",
    "StaticTokenProvider",
    "TokenValidity",
    "TokenValidityProbe",
]
