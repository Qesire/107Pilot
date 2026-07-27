import tempfile
import unittest
from pathlib import Path
from typing import NoReturn

from pilot107.adapters.slurm import (
    InMemorySlurmBackend,
    SlurmBackendOwnershipError,
    SlurmTransportError,
)
from pilot107.core.diagnosis import DiagnosisService
from pilot107.core.resources import ResourcePlan
from pilot107.core.run_service import RunService, RunSubmitRequest
from pilot107.core.run_store import RunRecord, RunStore
from pilot107.core.states import CollectionState, DiagnosisState, RunState
from pilot107.worker.evidence import EvidenceArtifact, EvidenceCollectionResult, EvidenceStore
from pilot107.worker.runtime_worker import (
    RuntimeReconcileWorker,
    WorkerErrorCode,
    classify_worker_exception,
)


def _plan() -> ResourcePlan:
    return ResourcePlan(
        partition="debug",
        qos="normal",
        nodes=1,
        ntasks=1,
        cpus_per_task=1,
        time_limit="00:05:00",
    )


class FailingGetJobBackend(InMemorySlurmBackend):
    def get_job(self, *, user: str, job_id: str) -> NoReturn:
        raise SlurmTransportError("temporary unavailable")


class ExpiredTokenBackend(InMemorySlurmBackend):
    def get_job(self, *, user: str, job_id: str) -> NoReturn:
        raise SlurmTransportError("401 token expired")


class ForeignBackendJob(InMemorySlurmBackend):
    def get_job(self, *, user: str, job_id: str) -> NoReturn:
        raise SlurmBackendOwnershipError("demo backend does not own job_id: 1000")


class FakeTaskHandler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def collect(self, *, run: RunRecord, task_type: str) -> EvidenceCollectionResult:
        self.calls.append((run.run_id, task_type))
        return EvidenceCollectionResult(
            run_id=run.run_id,
            task_type=task_type,
            artifacts=[
                EvidenceArtifact(
                    logical_path=f"{task_type}.json",
                    path=Path("/tmp/evidence.json"),
                    size_bytes=2,
                    sha256="0" * 64,
                    content_type="application/json",
                )
            ],
        )


class DiagnosingTaskHandler:
    def __init__(self, *, store: RunStore, evidence_store: EvidenceStore) -> None:
        self.store = store
        self.evidence_store = evidence_store

    def collect(self, *, run: RunRecord, task_type: str) -> EvidenceCollectionResult:
        artifacts: list[EvidenceArtifact] = []
        if task_type == "logs_finalize":
            artifact = self.evidence_store.write_text(
                run_id=run.run_id,
                logical_path="logs/stderr.tail.txt",
                content="ModuleNotFoundError: No module named 'torch'\n",
                content_type="text/plain",
            )
            self.store.upsert_evidence_objects(
                run.run_id,
                [
                    {
                        "object_id": "ev_worker_stderr",
                        "category": "logs",
                        "logical_path": artifact.logical_path,
                        "store_path": str(artifact.path),
                        "source_uri": f"evidence://runs/{run.run_id}/{artifact.logical_path}",
                        "sha256": artifact.sha256,
                        "size_bytes": artifact.size_bytes,
                        "mime_type": artifact.content_type,
                        "collection_status": "collected",
                        "mutable_during_run": False,
                    }
                ],
            )
            artifacts.append(artifact)
        return EvidenceCollectionResult(
            run_id=run.run_id,
            task_type=task_type,
            artifacts=artifacts,
        )


class MissingTokenTaskHandler:
    def collect(self, *, run: RunRecord, task_type: str) -> EvidenceCollectionResult:
        raise SlurmTransportError("missing token for evidence transport")


class TemporaryTaskFailureHandler:
    def collect(self, *, run: RunRecord, task_type: str) -> EvidenceCollectionResult:
        raise SlurmTransportError("gateway rate limit exceeded")


class RuntimeReconcileWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self._tmp.name) / "pilot107.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_tick_reconciles_active_run_to_terminal_state(self) -> None:
        backend = InMemorySlurmBackend()
        service = RunService(store=self.store, backend=backend)
        run = service.submit(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_plan(),
            )
        )
        backend.advance_job(job_id=run.job_id or "", raw_state="COMPLETED", exit_code="0:0")

        result = RuntimeReconcileWorker(service=service).tick()

        self.assertEqual(result.checked, 1)
        self.assertEqual(result.terminal, 1)
        self.assertEqual(result.errors, [])
        self.assertEqual(self.store.get_run(run.run_id).state, RunState.SUCCEEDED)

    def test_terminal_runs_are_not_reprocessed(self) -> None:
        backend = InMemorySlurmBackend()
        service = RunService(store=self.store, backend=backend)
        run = service.submit(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_plan(),
            )
        )
        backend.advance_job(job_id=run.job_id or "", raw_state="COMPLETED", exit_code="0:0")
        worker = RuntimeReconcileWorker(service=service)
        worker.tick()

        result = worker.tick()

        self.assertEqual(result.checked, 0)
        self.assertEqual(result.terminal, 0)

    def test_backend_error_is_reported_without_terminal_transition(self) -> None:
        service = RunService(store=self.store, backend=FailingGetJobBackend())
        run = service.submit(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_plan(),
            )
        )

        result = RuntimeReconcileWorker(service=service).tick()

        self.assertEqual(result.checked, 1)
        self.assertEqual(result.terminal, 0)
        self.assertEqual(result.errors[0].run_id, run.run_id)
        self.assertEqual(result.errors[0].code, WorkerErrorCode.SLURM_BACKEND_ERROR.value)
        self.assertTrue(result.errors[0].retryable)
        self.assertFalse(result.errors[0].auth_required)
        self.assertEqual(self.store.get_run(run.run_id).state, RunState.SUBMITTED)

    def test_backend_ownership_loss_is_quarantined_without_unhealthy_retry_loop(self) -> None:
        service = RunService(store=self.store, backend=ForeignBackendJob())
        run = service.submit(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_plan(),
            )
        )

        result = RuntimeReconcileWorker(service=service).tick()

        self.assertEqual(result.checked, 1)
        self.assertEqual(result.terminal, 1)
        self.assertEqual(result.errors, [])
        quarantined = self.store.get_run(run.run_id)
        self.assertEqual(quarantined.state, RunState.ORPHANED)
        self.assertEqual(quarantined.terminal_state, "BACKEND_OWNERSHIP_LOST")
        self.assertEqual(self.store.list_active_job_runs(), [])

    def test_expired_auth_backend_error_records_auth_required_event(self) -> None:
        service = RunService(store=self.store, backend=ExpiredTokenBackend())
        run = service.submit(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_plan(),
            )
        )

        result = RuntimeReconcileWorker(service=service).tick()

        self.assertEqual(result.errors[0].code, WorkerErrorCode.AUTH_EXPIRED.value)
        self.assertFalse(result.errors[0].retryable)
        self.assertTrue(result.errors[0].auth_required)
        event = self.store.list_events(run.run_id)[-1]
        self.assertEqual(event.event_type, "worker.run_error")
        self.assertEqual(event.payload["code"], WorkerErrorCode.AUTH_EXPIRED.value)
        self.assertTrue(event.payload["auth_required"])

    def test_tick_processes_due_collection_tasks_when_handler_is_configured(self) -> None:
        backend = InMemorySlurmBackend()
        service = RunService(store=self.store, backend=backend)
        run = service.submit(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_plan(),
            )
        )
        backend.advance_job(job_id=run.job_id or "", raw_state="COMPLETED", exit_code="0:0")
        handler = FakeTaskHandler()

        result = RuntimeReconcileWorker(service=service, task_handler=handler).tick()

        self.assertEqual(result.checked, 1)
        self.assertEqual(result.terminal, 1)
        self.assertEqual(result.tasks_checked, 7)
        self.assertEqual(result.tasks_succeeded, 7)
        self.assertEqual(result.task_errors, [])
        self.assertEqual(len(handler.calls), 7)
        states = {task["state"] for task in self.store.list_collection_tasks(run.run_id)}
        self.assertEqual(states, {"succeeded"})
        self.assertEqual(self.store.get_run(run.run_id).collection_state, CollectionState.SUCCEEDED)

    def test_auth_required_collection_error_marks_task_non_retryable(self) -> None:
        backend = InMemorySlurmBackend()
        service = RunService(store=self.store, backend=backend)
        run = service.submit(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_plan(),
            )
        )
        backend.advance_job(job_id=run.job_id or "", raw_state="COMPLETED", exit_code="0:0")
        worker = RuntimeReconcileWorker(
            service=service,
            task_handler=MissingTokenTaskHandler(),
            batch_size=1,
        )

        result = worker.tick()

        self.assertEqual(result.task_errors[0].code, WorkerErrorCode.AUTH_REQUIRED.value)
        self.assertFalse(result.task_errors[0].retryable)
        self.assertTrue(result.task_errors[0].auth_required)
        task = self.store.get_collection_task(result.task_errors[0].task_id)
        self.assertEqual(task.state, "failed_permanent")
        event = self.store.list_events(run.run_id)[-1]
        self.assertEqual(event.event_type, "collection.task_failed")
        self.assertEqual(event.payload["error_code"], WorkerErrorCode.AUTH_REQUIRED.value)
        self.assertTrue(event.payload["auth_required"])

    def test_retryable_collection_error_gets_retry_backoff(self) -> None:
        backend = InMemorySlurmBackend()
        service = RunService(store=self.store, backend=backend)
        run = service.submit(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_plan(),
            )
        )
        backend.advance_job(job_id=run.job_id or "", raw_state="COMPLETED", exit_code="0:0")
        worker = RuntimeReconcileWorker(
            service=service,
            task_handler=TemporaryTaskFailureHandler(),
            batch_size=1,
        )

        result = worker.tick()

        task = self.store.get_collection_task(result.task_errors[0].task_id)
        self.assertEqual(task.state, "failed_retryable")
        self.assertIsNotNone(task.next_attempt_at)
        self.assertGreater(str(task.next_attempt_at), task.updated_at)
        event = self.store.list_events(run.run_id)[-1]
        self.assertEqual(event.payload["retry_delay_seconds"], 1)

    def test_rate_limit_error_is_not_auth_required(self) -> None:
        classification = classify_worker_exception(
            SlurmTransportError("gateway /run failed: {'error': 'rate limit exceeded'}"),
            default_code=WorkerErrorCode.EVIDENCE_COLLECTION_ERROR,
            default_retryable=True,
        )

        self.assertEqual(classification.code, WorkerErrorCode.EVIDENCE_COLLECTION_ERROR)
        self.assertTrue(classification.retryable)
        self.assertFalse(classification.auth_required)

    def test_run_until_idle_is_worker_method(self) -> None:
        backend = InMemorySlurmBackend()
        service = RunService(store=self.store, backend=backend)
        run = service.submit(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_plan(),
            )
        )
        backend.advance_job(job_id=run.job_id or "", raw_state="COMPLETED", exit_code="0:0")
        worker = RuntimeReconcileWorker(
            service=service,
            task_handler=FakeTaskHandler(),
        )

        result = worker.run_until_idle(max_ticks=4, interval_seconds=0)

        self.assertEqual(result.terminal, 1)
        self.assertEqual(result.tasks_succeeded, 7)
        self.assertEqual(self.store.get_run(run.run_id).collection_state, CollectionState.SUCCEEDED)

    def test_worker_auto_triggers_diagnosis_after_collection_succeeds(self) -> None:
        backend = InMemorySlurmBackend()
        service = RunService(store=self.store, backend=backend)
        run = service.submit(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\npython train.py\n",
                resource_plan=_plan(),
            )
        )
        backend.advance_job(job_id=run.job_id or "", raw_state="FAILED", exit_code="1:0")
        evidence_store = EvidenceStore(Path(self._tmp.name) / "evidence")
        worker = RuntimeReconcileWorker(
            service=service,
            task_handler=DiagnosingTaskHandler(store=self.store, evidence_store=evidence_store),
            diagnosis_service=DiagnosisService(store=self.store),
        )

        result = worker.tick()

        self.assertEqual(result.tasks_succeeded, 7)
        self.assertEqual(result.diagnoses_checked, 1)
        self.assertEqual(result.diagnoses_succeeded, 1)
        final = self.store.get_run(run.run_id)
        self.assertEqual(final.collection_state, CollectionState.SUCCEEDED)
        self.assertEqual(final.diagnosis_state, DiagnosisState.SUCCEEDED)
        self.assertIn(
            "RUNTIME.PYTHON_PACKAGE_MISSING",
            {record.rule_id for record in self.store.list_diagnoses(run.run_id)},
        )


if __name__ == "__main__":
    unittest.main()
