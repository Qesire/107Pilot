"""Tests for Lane 4b-ii service wiring.

Covers:
1. ``TokenMintingRestBackend`` — token injected per-call via the provider,
   never present in the receipt.
2. WorkDirPreflight integration — BLOCK finding rejects submit, no backend
   call is made.
3. Idempotency reconciliation — submit timeout triggers reconcile with
   bound / not_found / uncertain outcomes.
"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pilot107.adapters.rest_token import StaticTokenProvider
from pilot107.adapters.rest_token_backend import (
    DEFAULT_JOB_NAME_MARKER,
    TokenMintingRestBackend,
)
from pilot107.adapters.slurm import (
    JobSnapshot,
    ResourcePlan,
    SlurmBackend,
    SlurmBackendError,
    SlurmTransportError,
    SubmitIntent,
    SubmitReceipt,
)
from pilot107.core.run_service import (
    RunService,
    RunSubmitRequest,
    SubmissionUncertainError,
    WorkDirPreflightError,
)
from pilot107.core.run_store import RunStore
from pilot107.core.states import RunState
from pilot107.core.submission_reconcile import ReconcileBackend

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


@dataclass
class _RecordingInnerBackend:
    """Records the token set on ``self.token`` at each call time."""

    token: str | None = None
    api_version: str = "v0.0.40"
    transport: Any = None
    submit_calls: list[SubmitIntent] = field(default_factory=list)
    get_job_calls: list[tuple[str, str]] = field(default_factory=list)
    cancel_calls: list[tuple[str, str]] = field(default_factory=list)
    fail_submit_with: SlurmBackendError | None = None

    def submit(self, intent: SubmitIntent) -> SubmitReceipt:
        self.submit_calls.append(intent)
        if self.fail_submit_with is not None:
            raise self.fail_submit_with
        return SubmitReceipt(
            job_id="999",
            run_state=RunState.SUBMITTED,
            strategy=__import__(
                "pilot107.adapters.slurm", fromlist=["SubmissionStrategy"]
            ).SubmissionStrategy.REST_NATIVE,
            raw_response={"job_id": "999"},
        )

    def get_job(self, *, user: str, job_id: str) -> JobSnapshot:
        self.get_job_calls.append((user, job_id))
        return JobSnapshot(
            job_id=job_id,
            owner=user,
            run_state=RunState.PENDING,
            raw_state_flags=["PENDING"],
            raw_response={},
        )

    def cancel(self, *, user: str, job_id: str) -> JobSnapshot:
        self.cancel_calls.append((user, job_id))
        return JobSnapshot(
            job_id=job_id,
            owner=user,
            run_state=RunState.CANCELLED,
            raw_state_flags=["CANCELLED"],
            raw_response={},
        )


class _NeverCalledBackend:
    """Backend whose submit should never be reached when preflight blocks."""

    def submit(self, intent: SubmitIntent) -> SubmitReceipt:
        raise AssertionError("backend.submit should not be called when preflight blocks")

    def get_job(self, *, user: str, job_id: str) -> JobSnapshot:
        raise AssertionError("get_job should not be called")

    def cancel(self, *, user: str, job_id: str) -> JobSnapshot:
        raise AssertionError("cancel should not be called")


@dataclass
class _ReconcileOnlyBackend:
    """A ReconcileBackend double returning canned job_ids."""

    job_ids: list[str]
    calls: list[dict[str, Any]] = field(default_factory=list)

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
        return self.job_ids


class _FakePathChecker:
    """PathChecker that reports the workdir as non-existent + parent missing."""

    def exists(self, path: str | Path) -> bool:
        return False

    def is_dir(self, path: str | Path) -> bool:
        return False

    def readable(self, path: str | Path) -> bool:
        return False

    def executable(self, path: str | Path) -> bool:
        return False

    def writable(self, path: str | Path) -> bool:
        return False


def _plan() -> ResourcePlan:
    return ResourcePlan(
        partition="debug",
        qos="normal",
        nodes=1,
        ntasks=1,
        cpus_per_task=1,
        time_limit="00:05:00",
    )


def _make_service(
    *,
    backend: SlurmBackend,
    workdir_preflight_enabled: bool = False,
    preflight_path_checker: Any = None,
    preflight_allowed_roots: tuple[str, ...] = ("/public/home/alice",),
    preflight_shared_roots: tuple[str, ...] = ("/public",),
    preflight_local_roots: tuple[str, ...] = ("/tmp",),
    idempotency_reconcile_enabled: bool = False,
    reconcile_backend: ReconcileBackend | None = None,
) -> tuple[RunService, RunStore, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    store = RunStore(Path(tmp.name) / "pilot107.db")
    service = RunService(
        store=store,
        backend=backend,
        workdir_preflight_enabled=workdir_preflight_enabled,
        preflight_allowed_roots=preflight_allowed_roots,
        preflight_shared_roots=preflight_shared_roots,
        preflight_local_roots=preflight_local_roots,
        preflight_path_checker=preflight_path_checker,
        idempotency_reconcile_enabled=idempotency_reconcile_enabled,
        reconcile_backend=reconcile_backend,
    )
    return service, store, tmp


# --------------------------------------------------------------------------- #
# 1. TokenMintingRestBackend
# --------------------------------------------------------------------------- #


class TokenMintingRestBackendTests(unittest.TestCase):
    def test_submit_mints_token_and_sets_on_inner_before_call(self) -> None:
        inner = _RecordingInnerBackend()
        provider = StaticTokenProvider(token="jwt-abc")
        wrapper = TokenMintingRestBackend(inner=inner, provider=provider)

        receipt = wrapper.submit(
            SubmitIntent(
                user="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_plan(),
            )
        )

        self.assertEqual(inner.token, "jwt-abc")
        self.assertEqual(len(inner.submit_calls), 1)
        # Token must NOT appear in the receipt
        self.assertNotIn("jwt-abc", str(receipt.raw_response))
        self.assertNotIn("jwt-abc", repr(receipt))

    def test_get_job_mints_token_per_user(self) -> None:
        inner = _RecordingInnerBackend()
        provider = StaticTokenProvider(token="jwt-bob")
        wrapper = TokenMintingRestBackend(inner=inner, provider=provider)

        wrapper.get_job(user="bob", job_id="42")

        self.assertEqual(inner.token, "jwt-bob")
        self.assertEqual(inner.get_job_calls, [("bob", "42")])

    def test_cancel_mints_token(self) -> None:
        inner = _RecordingInnerBackend()
        provider = StaticTokenProvider(token="jwt-cancel")
        wrapper = TokenMintingRestBackend(inner=inner, provider=provider)

        wrapper.cancel(user="alice", job_id="42")

        self.assertEqual(inner.token, "jwt-cancel")
        self.assertEqual(inner.cancel_calls, [("alice", "42")])


# --------------------------------------------------------------------------- #
# 2. WorkDirPreflight integration
# --------------------------------------------------------------------------- #


class WorkDirPreflightIntegrationTests(unittest.TestCase):
    def test_block_finding_rejects_submit_and_skips_backend(self) -> None:
        service, _, _tmp = _make_service(
            backend=_NeverCalledBackend(),  # type: ignore[arg-type]
            workdir_preflight_enabled=True,
            preflight_path_checker=_FakePathChecker(),
            preflight_allowed_roots=("/public/home/alice",),
            preflight_shared_roots=("/public",),
            preflight_local_roots=("/tmp",),
        )

        with self.assertRaises(WorkDirPreflightError) as ctx:
            service.submit(
                RunSubmitRequest(
                    owner="alice",
                    workdir=Path("/public/home/alice/nonexistent"),
                    script="#!/bin/bash\nhostname\n",
                    resource_plan=_plan(),
                )
            )

        # The finding list should contain a BLOCK finding
        codes = {f.code for f in ctx.exception.findings}
        self.assertIn("WORKDIR_PARENT_NOT_FOUND", codes)

    def test_tmp_workdir_blocked_by_pure_path_check(self) -> None:
        service, _, _tmp = _make_service(
            backend=_NeverCalledBackend(),  # type: ignore[arg-type]
            workdir_preflight_enabled=True,
            preflight_path_checker=None,  # pure-path only
        )

        with self.assertRaises(WorkDirPreflightError) as ctx:
            service.submit(
                RunSubmitRequest(
                    owner="alice",
                    workdir=Path("/tmp/alice-job"),
                    script="#!/bin/bash\nhostname\n",
                    resource_plan=_plan(),
                )
            )

        codes = {f.code for f in ctx.exception.findings}
        self.assertIn("WORKDIR_LOCAL_TMP", codes)

    def test_preflight_disabled_allows_submit(self) -> None:
        inner = _RecordingInnerBackend()
        service, _, _tmp = _make_service(
            backend=inner,  # type: ignore[arg-type]
            workdir_preflight_enabled=False,
        )

        run = service.submit(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/tmp/whatever"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_plan(),
            )
        )

        self.assertEqual(run.state, RunState.SUBMITTED)
        self.assertEqual(len(inner.submit_calls), 1)


# --------------------------------------------------------------------------- #
# 3. Idempotency reconciliation
# --------------------------------------------------------------------------- #


class IdempotencyReconcileTests(unittest.TestCase):
    def test_concurrent_runs_use_distinct_stable_reconciliation_markers(self) -> None:
        inner = _RecordingInnerBackend()
        service, _, _tmp = _make_service(backend=inner)  # type: ignore[arg-type]
        request = RunSubmitRequest(
            owner="alice",
            workdir=Path("/public/home/alice"),
            script="#!/bin/bash\nhostname\n",
            resource_plan=_plan(),
        )
        first = service.prepare(request, run_id="run_first")
        second = service.prepare(request, run_id="run_second")

        service.submit_prepared(first.run_id)
        service.submit_prepared(second.run_id)

        markers = [intent.job_name for intent in inner.submit_calls]
        self.assertEqual(len(set(markers)), 2)
        self.assertTrue(all(marker and marker.startswith("pilot107-run-") for marker in markers))

    def test_timeout_then_single_match_binds_job(self) -> None:
        inner = _RecordingInnerBackend(
            fail_submit_with=SlurmTransportError("timeout"),
        )
        reconcile_backend = _ReconcileOnlyBackend(job_ids=["555"])
        service, store, _tmp = _make_service(
            backend=inner,  # type: ignore[arg-type]
            idempotency_reconcile_enabled=True,
            reconcile_backend=reconcile_backend,
        )
        run_id = service.prepare(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_plan(),
            )
        ).run_id

        result = service.submit_prepared(run_id)

        self.assertEqual(result.job_id, "555")
        self.assertEqual(result.state, RunState.SUBMITTED)
        # The original submit was attempted, then reconcile found one match
        self.assertEqual(len(inner.submit_calls), 1)
        self.assertEqual(len(reconcile_backend.calls), 1)
        marker = reconcile_backend.calls[0]["job_name_marker"]
        self.assertIsInstance(marker, str)
        self.assertRegex(marker, r"^pilot107-run-[0-9a-f]{20}$")
        self.assertEqual(inner.submit_calls[0].job_name, marker)

    def test_timeout_then_zero_matches_retries_submit(self) -> None:
        # First submit raises transport error; reconcile finds nothing;
        # retry submit succeeds (fail_submit_with cleared for the retry).
        inner = _RecordingInnerBackend()

        class _RetryBackend:
            """Wraps inner, first submit raises, second succeeds."""

            def __init__(self) -> None:
                self._inner = inner
                self._first = True

            @property
            def token(self) -> str | None:
                return self._inner.token

            @token.setter
            def token(self, value: str | None) -> None:
                self._inner.token = value

            def submit(self, intent: SubmitIntent) -> SubmitReceipt:
                if self._first:
                    self._first = False
                    raise SlurmTransportError("timeout")
                return self._inner.submit(intent)

            def get_job(self, *, user: str, job_id: str) -> JobSnapshot:
                return self._inner.get_job(user=user, job_id=job_id)

            def cancel(self, *, user: str, job_id: str) -> JobSnapshot:
                return self._inner.cancel(user=user, job_id=job_id)

        reconcile_backend = _ReconcileOnlyBackend(job_ids=[])
        service, _, _tmp = _make_service(
            backend=_RetryBackend(),  # type: ignore[arg-type]
            idempotency_reconcile_enabled=True,
            reconcile_backend=reconcile_backend,
        )
        run_id = service.prepare(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_plan(),
            )
        ).run_id

        result = service.submit_prepared(run_id)

        self.assertEqual(result.state, RunState.SUBMITTED)
        self.assertEqual(result.job_id, "999")
        # Reconcile was called, then retry submit succeeded
        self.assertEqual(len(reconcile_backend.calls), 1)
        self.assertEqual(len(inner.submit_calls), 1)

    def test_timeout_then_multiple_matches_raises_uncertain(self) -> None:
        inner = _RecordingInnerBackend(
            fail_submit_with=SlurmTransportError("timeout"),
        )
        reconcile_backend = _ReconcileOnlyBackend(job_ids=["100", "200"])
        service, store, _tmp = _make_service(
            backend=inner,  # type: ignore[arg-type]
            idempotency_reconcile_enabled=True,
            reconcile_backend=reconcile_backend,
        )
        run_id = service.prepare(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_plan(),
            )
        ).run_id

        with self.assertRaises(SubmissionUncertainError) as ctx:
            service.submit_prepared(run_id)

        self.assertEqual(ctx.exception.job_ids, ["100", "200"])
        # Run state should be SUBMISSION_UNCERTAIN
        run = store.get_run(run_id)
        self.assertEqual(run.state, RunState.SUBMISSION_UNCERTAIN)

    def test_timeout_without_reconcile_enabled_marks_submit_failed(self) -> None:
        inner = _RecordingInnerBackend(
            fail_submit_with=SlurmTransportError("timeout"),
        )
        service, store, _tmp = _make_service(
            backend=inner,  # type: ignore[arg-type]
            idempotency_reconcile_enabled=False,
            reconcile_backend=None,
        )
        run_id = service.prepare(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_plan(),
            )
        ).run_id

        with self.assertRaises(SlurmTransportError):
            service.submit_prepared(run_id)

        self.assertEqual(store.get_run(run_id).state, RunState.SUBMIT_FAILED)

    def test_default_job_name_marker_is_pilot107_run(self) -> None:
        self.assertEqual(DEFAULT_JOB_NAME_MARKER, "pilot107-run")


if __name__ == "__main__":
    unittest.main()
