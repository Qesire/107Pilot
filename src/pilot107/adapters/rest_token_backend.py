"""Token-minting wrapper around :class:`RestNativeSlurmBackend`.

Lane 4b-ii wires the REST backend into the service layer. The adapter
(:mod:`pilot107.adapters.slurm`) holds a static ``token`` set at construction
time. For the simulator, the JWT must be minted per-user via ``scontrol token``
and refreshed before expiry (see :mod:`pilot107.adapters.rest_token`).

Rather than touching the adapter (owned by Lane 2), this module provides a
thin composition wrapper, :class:`TokenMintingRestBackend`, that:

* holds an inner :class:`RestNativeSlurmBackend` and a
  :class:`RestTokenProvider`;
* before each ``submit`` / ``get_job`` / ``cancel`` call, mints a fresh token
  for the run user and assigns it to ``self._inner.token`` (the provider
  caches, so this is cheap);
* exposes :meth:`find_jobs_by_marker` so the idempotency reconciliation helper
  in :mod:`pilot107.core.submission_reconcile` can query recent jobs by
  name + user + submit-time window.

Security invariants (see ``docs/phase-1/auth_decision.md``):

* The minted token is NEVER stored in config, the DB, logs, the
  :class:`SubmitReceipt` / :class:`JobSnapshot` raw_response, or error
  messages. It lives only on ``self._inner.token`` for the duration of one
  HTTP call.
* ``find_jobs_by_marker`` mints its own token the same way and returns only
  job_ids — never the token or the full job payload.
"""

from __future__ import annotations

from typing import Any, Protocol

from pilot107.adapters.rest_token import RestTokenProvider
from pilot107.adapters.slurm import (
    HttpResponse,
    JobSnapshot,
    SlurmTransportError,
    SubmitIntent,
    SubmitReceipt,
)

# RunService treats this as a safe prefix and appends a stable digest of the
# run_id. Every adapter carries that explicit per-run name into Slurm, so
# reconciliation does not conflate concurrent submits from the same user.
DEFAULT_JOB_NAME_MARKER = "pilot107-run"


class _RestInnerBackend(Protocol):
    """Structural type for the inner REST backend the wrapper delegates to.

    Matches :class:`RestNativeSlurmBackend` but allows tests to substitute a
    fake that records token assignments without inheriting from the real
    adapter.
    """

    token: str | None

    @property
    def transport(self) -> Any:
        ...

    @property
    def api_version(self) -> str:
        ...

    def submit(self, intent: SubmitIntent) -> SubmitReceipt: ...

    def get_job(self, *, user: str, job_id: str) -> JobSnapshot: ...

    def cancel(self, *, user: str, job_id: str) -> JobSnapshot: ...


class TokenMintingRestBackend:
    """Composition wrapper that injects a per-user JWT before every REST call.

    Implements the :class:`SlurmBackend` protocol by delegating to the inner
    :class:`RestNativeSlurmBackend`. The token is minted from ``provider``
    for the run ``user`` and assigned to ``self._inner.token`` immediately
    before the delegated call. The provider caches per user, so repeated
    calls within the cache window do not re-shell-out to ``scontrol``.
    """

    def __init__(
        self,
        *,
        inner: _RestInnerBackend,
        provider: RestTokenProvider,
    ) -> None:
        self._inner = inner
        self._provider = provider

    # ------------------------------------------------------------------ #
    # SlurmBackend protocol
    # ------------------------------------------------------------------ #

    def submit(self, intent: SubmitIntent) -> SubmitReceipt:
        self._inner.token = self._provider.mint_token(user=intent.user)
        return self._inner.submit(intent)

    def get_job(self, *, user: str, job_id: str) -> JobSnapshot:
        self._inner.token = self._provider.mint_token(user=user)
        return self._inner.get_job(user=user, job_id=job_id)

    def cancel(self, *, user: str, job_id: str) -> JobSnapshot:
        self._inner.token = self._provider.mint_token(user=user)
        return self._inner.cancel(user=user, job_id=job_id)

    # ------------------------------------------------------------------ #
    # Reconciliation support
    # ------------------------------------------------------------------ #

    def find_jobs_by_marker(
        self,
        *,
        user: str,
        job_name_marker: str,
        since_timestamp: float,
    ) -> list[str]:
        """Return job_ids of recent jobs matching ``name`` + ``user`` + time window.

        Queries ``GET /slurm/{api_version}/jobs`` (the slurmrestd active-jobs
        view) and filters client-side by ``name == job_name_marker``,
        ``user_name == user``, and ``submit_time >= since_timestamp``. The
        token is minted the same way as for submit/get_job/cancel.

        Returns only the matching ``job_id`` strings — never the token or the
        full job payloads — so reconciliation can be logged without leaking
        credentials or large response bodies.
        """
        token = self._provider.mint_token(user=user)
        self._inner.token = token
        response = self._inner.transport.request(
            "GET",
            f"/slurm/{self._inner.api_version}/jobs",
            token=token,
        )
        if response.status >= 400:
            raise SlurmTransportError(
                f"reconcile jobs query failed: HTTP {response.status}"
            )
        return _filter_jobs_by_marker(
            response=response,
            user=user,
            job_name_marker=job_name_marker,
            since_timestamp=since_timestamp,
        )


def _filter_jobs_by_marker(
    *,
    response: HttpResponse,
    user: str,
    job_name_marker: str,
    since_timestamp: float,
) -> list[str]:
    """Extract matching job_ids from a ``GET /slurm/.../jobs`` response.

    slurmrestd returns ``{"jobs": [...]}`` where each job has ``job_id``,
    ``name``, ``user_name`` (sometimes ``user``), and ``submit_time``. The
    ``submit_time`` field may be a plain number or a ``{"number": ..., "set": ...}``
    object depending on the API version; both are handled.
    """
    payload: dict[str, Any] = response.payload or {}
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        return []
    matches: list[str] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        name = str(job.get("name") or "")
        if name != job_name_marker:
            continue
        owner = str(job.get("user_name") or job.get("user") or "")
        if owner != user:
            continue
        submit_time = _extract_submit_time(job.get("submit_time"))
        if submit_time is None or submit_time < since_timestamp:
            continue
        job_id = job.get("job_id")
        if job_id is None:
            continue
        matches.append(str(job_id))
    return matches


def _extract_submit_time(value: Any) -> float | None:
    """Normalize slurmrestd ``submit_time`` to a unix epoch float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        number = value.get("number")
        if isinstance(number, (int, float)):
            return float(number)
    return None


__all__ = [
    "DEFAULT_JOB_NAME_MARKER",
    "TokenMintingRestBackend",
]
