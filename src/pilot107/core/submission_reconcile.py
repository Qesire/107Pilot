"""Idempotency reconciliation for REST submit timeouts.

Implements §5 of ``docs/phase-1/submission_strategy.md``. When a
``backend.submit`` call raises :class:`SlurmTransportError` (timeout /
transport failure), the job may or may not have been created at the slurmrestd
side. The service must reconcile before retrying:

* query recent jobs by ``job_name_marker`` + ``user`` + submit-time window;
* exactly one match → bind that ``job_id`` (treat submit as succeeded);
* zero matches → safe to retry the submit;
* two or more matches → ``SUBMISSION_UNCERTAIN`` (surface to the user).

This module is backend-agnostic: it talks to a :class:`ReconcileBackend`
protocol that exposes :meth:`find_jobs_by_marker`. The REST wrapper
(:class:`pilot107.adapters.rest_token_backend.TokenMintingRestBackend`)
implements it; tests inject a fake.

Token safety: reconciliation never returns or logs tokens. The backend's
``find_jobs_by_marker`` returns only ``job_id`` strings.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol


class ReconcileBackend(Protocol):
    """Backend capable of finding recent jobs by marker + user + time window."""

    def find_jobs_by_marker(
        self,
        *,
        user: str,
        job_name_marker: str,
        since_timestamp: float,
    ) -> Sequence[str]:
        """Return job_ids matching the marker for ``user`` submitted at or
        after ``since_timestamp`` (unix epoch seconds)."""
        ...


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of a reconciliation attempt.

    * ``state == "bound"`` — exactly one candidate; ``job_id`` is set and the
      service should treat the submit as succeeded.
    * ``state == "not_found"`` — zero candidates; the submit did not land and
      a single retry is safe.
    * ``state == "uncertain"`` — two or more candidates; the run should be
      marked ``SUBMISSION_UNCERTAIN`` and surfaced to the user.
    """

    state: Literal["bound", "not_found", "uncertain"]
    job_id: str | None
    matches: tuple[str, ...]


def reconcile_submission(
    *,
    backend: ReconcileBackend,
    user: str,
    job_name_marker: str,
    submitted_after: float,
    time_window_seconds: float = 60.0,
) -> ReconcileResult:
    """Reconcile a timed-out submit by querying recent jobs.

    ``submitted_after`` is the wall-clock unix timestamp captured just before
    the original ``backend.submit`` attempt. ``time_window_seconds`` is the
    lookback buffer applied to ``submitted_after`` to absorb clock skew
    between the 107Pilot process and slurmrestd.
    """
    if time_window_seconds < 0:
        raise ValueError("time_window_seconds must not be negative")
    since_timestamp = submitted_after - time_window_seconds
    matches = tuple(
        backend.find_jobs_by_marker(
            user=user,
            job_name_marker=job_name_marker,
            since_timestamp=since_timestamp,
        )
    )
    if len(matches) == 1:
        return ReconcileResult(state="bound", job_id=matches[0], matches=matches)
    if not matches:
        return ReconcileResult(state="not_found", job_id=None, matches=())
    return ReconcileResult(state="uncertain", job_id=None, matches=matches)


__all__ = [
    "ReconcileBackend",
    "ReconcileResult",
    "reconcile_submission",
]
