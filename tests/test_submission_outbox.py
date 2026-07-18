from __future__ import annotations

import multiprocessing
import os
import tempfile
import unittest
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pilot107.adapters.slurm import (
    InMemorySlurmBackend,
    JobSnapshot,
    SlurmTransportError,
    SubmissionStrategy,
    SubmitIntent,
    SubmitReceipt,
)
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.core.resources import ResourcePlan
from pilot107.core.run_service import (
    RunService,
    RunSubmitRequest,
    SubmissionRecoveryRequiredError,
    WorkflowPolicy,
)
from pilot107.core.run_store import RunStore
from pilot107.core.states import RunState


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 18, 1, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class SimulatedProcessCrash(BaseException):
    pass


class CrashAfterExternalSubmitBackend(InMemorySlurmBackend):
    def __init__(self) -> None:
        super().__init__()
        self.submit_calls = 0
        self.submitted_job_id: str | None = None

    def submit(self, intent: SubmitIntent) -> SubmitReceipt:
        self.submit_calls += 1
        receipt = super().submit(intent)
        self.submitted_job_id = receipt.job_id
        if self.submit_calls == 1:
            raise SimulatedProcessCrash("process died after external submit")
        return receipt


class RecordingBackend(InMemorySlurmBackend):
    def __init__(self) -> None:
        super().__init__()
        self.submit_calls = 0

    def submit(self, intent: SubmitIntent) -> SubmitReceipt:
        self.submit_calls += 1
        return super().submit(intent)


class AmbiguousTransportBackend(InMemorySlurmBackend):
    def __init__(self) -> None:
        super().__init__()
        self.submit_calls = 0

    def submit(self, intent: SubmitIntent) -> SubmitReceipt:
        self.submit_calls += 1
        raise SlurmTransportError("submit response timed out")


class ProcessRecordingBackend:
    def __init__(self, side_effect_path: Path) -> None:
        self.side_effect_path = side_effect_path

    def submit(self, intent: SubmitIntent) -> SubmitReceipt:
        descriptor = os.open(
            self.side_effect_path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(descriptor, f"{intent.job_name}\n".encode())
        finally:
            os.close(descriptor)
        return SubmitReceipt(
            job_id="process-job-1",
            run_state=RunState.SUBMITTED,
            strategy=SubmissionStrategy.DEMO,
            raw_response={"backend": "process-test"},
        )

    def get_job(self, *, user: str, job_id: str) -> JobSnapshot:
        raise AssertionError("get_job is not used by the dispatch process test")

    def cancel(self, *, user: str, job_id: str) -> JobSnapshot:
        raise AssertionError("cancel is not used by the dispatch process test")


def _dispatch_in_process(db_path: str, side_effect_path: str, dispatcher_id: str) -> None:
    path = Path(db_path)
    service = RunService(
        store=RunStore(path),
        backend=ProcessRecordingBackend(Path(side_effect_path)),
        control_repository=SQLiteControlRepository(path),
        dispatcher_id=dispatcher_id,
    )
    batch = service.dispatch_due_submissions(limit=1)
    if batch.errors:
        raise RuntimeError(batch.errors[0].message)


class ReconcileBackend:
    def __init__(self, job_ids: Sequence[str]) -> None:
        self.job_ids = tuple(job_ids)
        self.calls: list[tuple[str, str, float]] = []

    def find_jobs_by_marker(
        self,
        *,
        user: str,
        job_name_marker: str,
        since_timestamp: float,
    ) -> Sequence[str]:
        self.calls.append((user, job_name_marker, since_timestamp))
        return self.job_ids


class CrashBeforeAcknowledgeRepository(SQLiteControlRepository):
    def __init__(self, db_path: Path, *, clock: MutableClock) -> None:
        super().__init__(db_path, clock=clock)
        self.crash_once = True

    def acknowledge(self, *, message_id: str, owner: str, fencing_token: int) -> None:
        if self.crash_once:
            self.crash_once = False
            raise SimulatedProcessCrash("process died before outbox ack")
        super().acknowledge(
            message_id=message_id,
            owner=owner,
            fencing_token=fencing_token,
        )


class SubmissionOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "pilot107.db"
        self.clock = MutableClock()
        self.run_store = RunStore(self.db_path)
        self.control = SQLiteControlRepository(self.db_path, clock=self.clock)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_submit_enqueues_dispatches_and_acknowledges_durably(self) -> None:
        backend = RecordingBackend()
        service = self._service(backend=backend, dispatcher_id="api-a")
        run = service.prepare(self._request(), run_id="run_sync")

        submitted = service.submit_prepared(run.run_id)

        self.assertEqual(submitted.state, RunState.SUBMITTED)
        self.assertEqual(backend.submit_calls, 1)
        messages = self.control.claim_outbox(
            owner="worker-late",
            limit=10,
            lease_seconds=30,
            topics=("run.submit",),
        )
        self.assertEqual(messages, [])
        submitting = [
            event
            for event in self.run_store.list_events(run.run_id)
            if event.event_type == "run.submitting"
        ][0]
        self.assertEqual(submitting.payload["lease_owner"], "api-a")
        self.assertEqual(submitting.payload["fencing_token"], 1)

    def test_worker_dispatches_message_left_after_enqueue_only_crash(self) -> None:
        backend = RecordingBackend()
        api = self._service(backend=backend, dispatcher_id="api-a")
        run = api.prepare(self._request(), run_id="run_enqueued")
        message = api.enqueue_submission(run.run_id)
        self.assertEqual(self.control.get_outbox(message.message_id).state, "pending")

        worker = self._service(backend=backend, dispatcher_id="worker-a")
        batch = worker.dispatch_due_submissions(limit=10)

        self.assertEqual(batch.checked, 1)
        self.assertEqual(batch.errors, [])
        self.assertEqual(batch.succeeded[0].state, RunState.SUBMITTED)
        self.assertEqual(backend.submit_calls, 1)

    def test_two_dispatchers_claim_one_submission_exactly_once(self) -> None:
        backend = RecordingBackend()
        api = self._service(backend=backend, dispatcher_id="api-a")
        run = api.prepare(self._request(), run_id="run_concurrent_dispatch")
        api.enqueue_submission(run.run_id)
        workers = (
            self._service(backend=backend, dispatcher_id="worker-a"),
            self._service(backend=backend, dispatcher_id="worker-b"),
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            batches = list(executor.map(lambda worker: worker.dispatch_due_submissions(), workers))

        self.assertEqual(sum(batch.checked for batch in batches), 1)
        self.assertEqual(sum(len(batch.succeeded) for batch in batches), 1)
        self.assertEqual(sum(len(batch.errors) for batch in batches), 0)
        self.assertEqual(backend.submit_calls, 1)
        self.assertEqual(self.run_store.get_run(run.run_id).state, RunState.SUBMITTED)

    def test_enqueue_validation_does_not_duplicate_dependency_audit_event(self) -> None:
        backend = RecordingBackend()
        service = self._service(backend=backend, dispatcher_id="api-a")
        dependency = service.submit(
            self._request(),
        )
        backend.advance_job(
            job_id=dependency.job_id or "",
            raw_state="COMPLETED",
            exit_code="0:0",
        )
        service.reconcile_once(dependency.run_id)
        request = self._request()
        child = service.prepare(
            RunSubmitRequest(
                owner=request.owner,
                workdir=request.workdir,
                script=request.script,
                resource_plan=request.resource_plan,
                workflow=WorkflowPolicy(dependencies=(dependency.run_id,)),
            ),
            run_id="run_dependency_outbox",
        )

        service.submit_prepared(child.run_id)

        dependency_events = [
            event
            for event in self.run_store.list_events(child.run_id)
            if event.event_type == "workflow.dependencies_resolved"
        ]
        self.assertEqual(len(dependency_events), 1)

    def test_two_spawned_worker_processes_emit_one_external_submit(self) -> None:
        backend = RecordingBackend()
        producer = self._service(backend=backend, dispatcher_id="api-a")
        run = producer.prepare(self._request(), run_id="run_process_dispatch")
        producer.enqueue_submission(run.run_id)
        side_effect_path = Path(self.tempdir.name) / "external-submits.log"
        context = multiprocessing.get_context("spawn")
        processes = [
            context.Process(
                target=_dispatch_in_process,
                args=(str(self.db_path), str(side_effect_path), f"worker-{index}"),
            )
            for index in range(2)
        ]

        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)

        self.assertTrue(all(not process.is_alive() for process in processes))
        self.assertEqual([process.exitcode for process in processes], [0, 0])
        external_submits = side_effect_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(external_submits), 1)
        self.assertRegex(external_submits[0], r"^pilot107-run-[0-9a-f]{20}$")
        self.assertEqual(self.run_store.get_run(run.run_id).job_id, "process-job-1")

    def test_crash_after_external_submit_recovers_by_unique_marker_without_resubmit(self) -> None:
        backend = CrashAfterExternalSubmitBackend()
        api = self._service(backend=backend, dispatcher_id="api-a")
        run = api.prepare(self._request(), run_id="run_external_crash")

        with self.assertRaises(SimulatedProcessCrash):
            api.submit_prepared(run.run_id)

        self.assertEqual(self.run_store.get_run(run.run_id).state, RunState.SUBMITTING)
        self.assertEqual(backend.submit_calls, 1)
        assert backend.submitted_job_id is not None
        self.clock.advance(61)
        reconcile = ReconcileBackend([backend.submitted_job_id])
        worker = self._service(
            backend=backend,
            dispatcher_id="worker-b",
            reconcile_backend=reconcile,
        )

        batch = worker.dispatch_due_submissions(limit=10)

        self.assertEqual(batch.errors, [])
        self.assertEqual(batch.succeeded[0].job_id, backend.submitted_job_id)
        self.assertEqual(backend.submit_calls, 1)
        self.assertEqual(len(reconcile.calls), 1)
        self.assertRegex(reconcile.calls[0][1], r"^pilot107-run-[0-9a-f]{20}$")
        fences = [
            event.payload["fencing_token"]
            for event in self.run_store.list_events(run.run_id)
            if event.event_type == "run.submitting"
        ]
        self.assertEqual(fences, [1, 2])

    def test_crash_after_run_write_is_recovered_by_ack_only(self) -> None:
        backend = RecordingBackend()
        crashing_control = CrashBeforeAcknowledgeRepository(
            self.db_path,
            clock=self.clock,
        )
        api = self._service(
            backend=backend,
            dispatcher_id="api-a",
            control=crashing_control,
        )
        run = api.prepare(self._request(), run_id="run_ack_crash")

        with self.assertRaises(SimulatedProcessCrash):
            api.submit_prepared(run.run_id)

        written = self.run_store.get_run(run.run_id)
        self.assertEqual(written.state, RunState.SUBMITTED)
        self.assertEqual(backend.submit_calls, 1)
        self.clock.advance(61)
        worker = self._service(backend=backend, dispatcher_id="worker-b")

        batch = worker.dispatch_due_submissions(limit=10)

        self.assertEqual(batch.errors, [])
        self.assertEqual(batch.succeeded[0].job_id, written.job_id)
        self.assertEqual(backend.submit_calls, 1)

    def test_recovery_without_reconcile_dead_letters_and_marks_uncertain(self) -> None:
        backend = CrashAfterExternalSubmitBackend()
        api = self._service(
            backend=backend,
            dispatcher_id="api-a",
            max_attempts=2,
        )
        run = api.prepare(self._request(), run_id="run_no_reconcile")
        message = api.enqueue_submission(run.run_id)
        with self.assertRaises(SimulatedProcessCrash):
            api.submit_prepared(run.run_id)
        self.clock.advance(61)
        worker = self._service(
            backend=backend,
            dispatcher_id="worker-b",
            max_attempts=2,
        )

        batch = worker.dispatch_due_submissions(limit=10)

        self.assertEqual(batch.checked, 1)
        self.assertEqual(len(batch.errors), 1)
        self.assertEqual(
            self.run_store.get_run(run.run_id).state,
            RunState.SUBMISSION_UNCERTAIN,
        )
        persisted_message = self.control.get_outbox(message.message_id)
        self.assertEqual(persisted_message.state, "dead_letter")
        self.assertEqual(backend.submit_calls, 1)

    def test_ambiguous_transport_never_automatically_resubmits(self) -> None:
        backend = AmbiguousTransportBackend()
        reconcile = ReconcileBackend([])
        api = self._service(
            backend=backend,
            dispatcher_id="api-a",
            reconcile_backend=reconcile,
            max_attempts=2,
        )
        run = api.prepare(self._request(), run_id="run_ambiguous_transport")

        with self.assertRaises(SubmissionRecoveryRequiredError):
            api.submit_prepared(run.run_id)
        worker = self._service(
            backend=backend,
            dispatcher_id="worker-b",
            reconcile_backend=reconcile,
            max_attempts=2,
        )
        batch = worker.dispatch_due_submissions(limit=10)

        self.assertEqual(backend.submit_calls, 1)
        self.assertEqual(len(reconcile.calls), 2)
        self.assertEqual(len(batch.errors), 1)
        self.assertEqual(
            self.run_store.get_run(run.run_id).state,
            RunState.SUBMISSION_UNCERTAIN,
        )

    def _service(
        self,
        *,
        backend: InMemorySlurmBackend,
        dispatcher_id: str,
        control: SQLiteControlRepository | None = None,
        reconcile_backend: ReconcileBackend | None = None,
        max_attempts: int = 5,
    ) -> RunService:
        return RunService(
            store=self.run_store,
            backend=backend,
            control_repository=control or self.control,
            dispatcher_id=dispatcher_id,
            submission_lease_seconds=60,
            submission_retry_delay_seconds=0,
            submission_max_attempts=max_attempts,
            idempotency_reconcile_enabled=reconcile_backend is not None,
            reconcile_backend=reconcile_backend,
        )

    @staticmethod
    def _request() -> RunSubmitRequest:
        return RunSubmitRequest(
            owner="alice",
            workdir=Path("/public/home/alice"),
            script="#!/bin/bash\nhostname\n",
            resource_plan=ResourcePlan(
                partition="debug",
                qos="normal",
                nodes=1,
                ntasks=1,
                cpus_per_task=1,
                time_limit="00:05:00",
            ),
        )


if __name__ == "__main__":
    unittest.main()
