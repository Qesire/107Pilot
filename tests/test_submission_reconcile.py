"""Tests for ``pilot107.core.submission_reconcile``.

Covers the three outcome states (bound / not_found / uncertain) and the
time-window logic, using a fake ``ReconcileBackend`` that returns canned
job_id lists.
"""

from __future__ import annotations

import unittest
from collections.abc import Sequence

from pilot107.core.submission_reconcile import (
    ReconcileResult,
    reconcile_submission,
)


class FakeReconcileBackend:
    """Returns a pre-set list of job_ids regardless of query parameters."""

    def __init__(self, job_ids: list[str]) -> None:
        self._job_ids = job_ids
        self.calls: list[dict[str, object]] = []

    def find_jobs_by_marker(
        self,
        *,
        user: str,
        job_name_marker: str,
        since_timestamp: float,
    ) -> Sequence[str]:
        self.calls.append(
            {
                "user": user,
                "job_name_marker": job_name_marker,
                "since_timestamp": since_timestamp,
            }
        )
        return self._job_ids


class ReconcileSubmissionTests(unittest.TestCase):
    def test_single_match_returns_bound(self) -> None:
        backend = FakeReconcileBackend(["42"])
        result = reconcile_submission(
            backend=backend,
            user="alice",
            job_name_marker="pilot107-run",
            submitted_after=1000.0,
        )
        self.assertEqual(result, ReconcileResult(state="bound", job_id="42", matches=("42",)))
        self.assertEqual(len(backend.calls), 1)
        call = backend.calls[0]
        self.assertEqual(call["user"], "alice")
        self.assertEqual(call["job_name_marker"], "pilot107-run")
        # time_window buffer subtracted from submitted_after
        self.assertEqual(call["since_timestamp"], 1000.0 - 60.0)

    def test_zero_matches_returns_not_found(self) -> None:
        backend = FakeReconcileBackend([])
        result = reconcile_submission(
            backend=backend,
            user="alice",
            job_name_marker="pilot107-run",
            submitted_after=1000.0,
        )
        self.assertEqual(result.state, "not_found")
        self.assertIsNone(result.job_id)
        self.assertEqual(result.matches, ())

    def test_multiple_matches_returns_uncertain(self) -> None:
        backend = FakeReconcileBackend(["42", "43"])
        result = reconcile_submission(
            backend=backend,
            user="alice",
            job_name_marker="pilot107-run",
            submitted_after=1000.0,
        )
        self.assertEqual(result.state, "uncertain")
        self.assertIsNone(result.job_id)
        self.assertEqual(result.matches, ("42", "43"))

    def test_custom_time_window_applied(self) -> None:
        backend = FakeReconcileBackend([])
        reconcile_submission(
            backend=backend,
            user="alice",
            job_name_marker="pilot107-run",
            submitted_after=2000.0,
            time_window_seconds=120.0,
        )
        self.assertEqual(backend.calls[0]["since_timestamp"], 2000.0 - 120.0)

    def test_negative_time_window_rejected(self) -> None:
        backend = FakeReconcileBackend([])
        with self.assertRaises(ValueError):
            reconcile_submission(
                backend=backend,
                user="alice",
                job_name_marker="pilot107-run",
                submitted_after=1000.0,
                time_window_seconds=-1.0,
            )


if __name__ == "__main__":
    unittest.main()
