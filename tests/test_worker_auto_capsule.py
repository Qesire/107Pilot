import tempfile
import unittest
from pathlib import Path

from pilot107.adapters.slurm import InMemorySlurmBackend
from pilot107.core.resources import ResourcePlan
from pilot107.core.run_service import RunService, RunSubmitRequest
from pilot107.core.run_store import RunStore
from pilot107.core.states import CollectionState
from pilot107.worker.capsule import CapsuleBuildResult, CapsuleError
from pilot107.worker.evidence import (
    EvidenceArtifact,
    EvidenceCollectionResult,
    EvidenceStore,
)
from pilot107.worker.runtime_worker import RuntimeReconcileWorker
from pilot107.worker.service import WorkerServiceConfig, build_worker_service, config_from_env


def _plan() -> ResourcePlan:
    return ResourcePlan(
        partition="debug",
        qos="normal",
        nodes=1,
        ntasks=1,
        cpus_per_task=1,
        time_limit="00:05:00",
    )


class _FakeTaskHandler:
    """Collects evidence artifacts for every collection task type."""

    def __init__(self, evidence_store: EvidenceStore, store: RunStore) -> None:
        self.evidence_store = evidence_store
        self.store = store

    def collect(self, *, run, task_type: str) -> EvidenceCollectionResult:  # type: ignore[no-untyped-def]
        artifact = self.evidence_store.write_text(
            run_id=run.run_id,
            logical_path=f"{task_type}/artifact.json",
            content=f'{{"task": "{task_type}"}}\n',
            content_type="application/json",
        )
        self.store.upsert_evidence_objects(
            run.run_id,
            [
                {
                    "object_id": f"ev_{task_type}",
                    "category": task_type,
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
        return EvidenceCollectionResult(
            run_id=run.run_id,
            task_type=task_type,
            artifacts=[
                EvidenceArtifact(
                    logical_path=artifact.logical_path,
                    path=artifact.path,
                    size_bytes=artifact.size_bytes,
                    sha256=artifact.sha256,
                    content_type=artifact.content_type,
                )
            ],
        )


class _RecordingCapsuleService:
    """Fake capsule service that records build calls without touching disk."""

    def __init__(self, *, raise_capsule_error: bool = False, raise_other: bool = False) -> None:
        self.calls: list[str] = []
        self.raise_capsule_error = raise_capsule_error
        self.raise_other = raise_other

    def build_raw_capsule(self, run_id: str) -> CapsuleBuildResult:
        self.calls.append(run_id)
        if self.raise_capsule_error:
            raise CapsuleError("run evidence is not fully collected: running")
        if self.raise_other:
            raise RuntimeError("disk on fire")
        return CapsuleBuildResult(
            run_id=run_id,
            capsule_id=f"capsule_{run_id}",
            capsule_dir=Path("/tmp/capsule"),
            manifest_sha256="0" * 64,
            files_copied=1,
        )


def _submit_and_complete(service: RunService, backend: InMemorySlurmBackend) -> str:
    run = service.submit(
        RunSubmitRequest(
            owner="alice",
            workdir=Path("/public/home/alice"),
            script="#!/bin/bash\nhostname\n",
            resource_plan=_plan(),
        )
    )
    backend.advance_job(job_id=run.job_id or "", raw_state="COMPLETED", exit_code="0:0")
    return run.run_id


class WorkerAutoCapsuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = RunStore(root / "pilot107.db")
        self.evidence_store = EvidenceStore(root / "evidence")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _build_worker(
        self,
        capsule_service,
    ) -> tuple[RuntimeReconcileWorker, RunService, str]:
        backend = InMemorySlurmBackend()
        service = RunService(store=self.store, backend=backend)
        run_id = _submit_and_complete(service, backend)
        worker = RuntimeReconcileWorker(
            service=service,
            task_handler=_FakeTaskHandler(self.evidence_store, self.store),
            capsule_service=capsule_service,
        )
        return worker, service, run_id

    def test_auto_capsule_built_after_collection_succeeds(self) -> None:
        capsule_service = _RecordingCapsuleService()
        worker, _service, run_id = self._build_worker(capsule_service)

        result = worker.tick()

        self.assertEqual(result.tasks_succeeded, 7)
        self.assertEqual(result.tasks_checked, 7)
        self.assertEqual(result.task_errors, [])
        self.assertEqual(self.store.get_run(run_id).collection_state, CollectionState.SUCCEEDED)
        self.assertEqual(capsule_service.calls, [run_id])
        self.assertEqual(result.capsule_builds_attempted, 1)
        self.assertEqual(result.capsule_builds_succeeded, 1)
        self.assertEqual(result.capsule_errors, [])
        events = [event.event_type for event in self.store.list_events(run_id)]
        self.assertIn("capsule.auto_build_completed", events)

    def test_auto_capsule_not_called_when_service_is_none(self) -> None:
        worker, _service, run_id = self._build_worker(None)

        result = worker.tick()

        self.assertEqual(result.tasks_succeeded, 7)
        self.assertEqual(self.store.get_run(run_id).collection_state, CollectionState.SUCCEEDED)
        self.assertEqual(result.capsule_builds_attempted, 0)
        self.assertEqual(result.capsule_builds_succeeded, 0)
        self.assertEqual(result.capsule_errors, [])
        events = [event.event_type for event in self.store.list_events(run_id)]
        self.assertNotIn("capsule.auto_build_completed", events)
        self.assertNotIn("capsule.auto_build_skipped", events)
        self.assertNotIn("capsule.auto_build_failed", events)

    def test_capsule_error_is_non_fatal_and_recorded_as_event(self) -> None:
        capsule_service = _RecordingCapsuleService(raise_capsule_error=True)
        worker, _service, run_id = self._build_worker(capsule_service)

        result = worker.tick()

        self.assertEqual(result.tasks_succeeded, 7)
        self.assertEqual(capsule_service.calls, [run_id])
        self.assertEqual(result.capsule_builds_attempted, 1)
        self.assertEqual(result.capsule_builds_succeeded, 0)
        # CapsuleError is non-fatal: no entry in capsule_errors, tick not crashed.
        self.assertEqual(result.capsule_errors, [])
        events = [event.event_type for event in self.store.list_events(run_id)]
        self.assertIn("capsule.auto_build_skipped", events)
        self.assertNotIn("capsule.auto_build_failed", events)

    def test_other_exception_is_recorded_as_capsule_error_without_crashing(self) -> None:
        capsule_service = _RecordingCapsuleService(raise_other=True)
        worker, _service, run_id = self._build_worker(capsule_service)

        result = worker.tick()

        self.assertEqual(result.tasks_succeeded, 7)
        self.assertEqual(capsule_service.calls, [run_id])
        self.assertEqual(result.capsule_builds_attempted, 1)
        self.assertEqual(result.capsule_builds_succeeded, 0)
        self.assertEqual(len(result.capsule_errors), 1)
        self.assertEqual(result.capsule_errors[0].run_id, run_id)
        self.assertEqual(result.capsule_errors[0].code, "CAPSULE.AUTO_BUILD_ERROR")
        self.assertTrue(result.capsule_errors[0].retryable)
        events = [event.event_type for event in self.store.list_events(run_id)]
        self.assertIn("capsule.auto_build_failed", events)

    def test_existing_ready_capsule_is_not_rebuilt(self) -> None:
        capsule_service = _RecordingCapsuleService()
        worker, _service, run_id = self._build_worker(capsule_service)
        # Simulate a capsule already built/READY (e.g. via the explicit API endpoint).
        from pilot107.core.states import CapsuleState as _CS

        self.store.update_capsule_state(run_id, _CS.READY, event_type="capsule.ready")

        result = worker.tick()

        self.assertEqual(result.tasks_succeeded, 7)
        self.assertEqual(self.store.get_run(run_id).collection_state, CollectionState.SUCCEEDED)
        # Guard should short-circuit; no build attempted.
        self.assertEqual(capsule_service.calls, [])
        self.assertEqual(result.capsule_builds_attempted, 0)


class WorkerAutoCapsuleConfigTests(unittest.TestCase):
    def test_auto_capsule_disabled_via_env(self) -> None:
        config = config_from_env({"PILOT107_AUTO_CAPSULE": "0"}, project_root=Path(self._tmp_root))
        self.assertFalse(config.auto_capsule_enabled)

    def test_auto_capsule_enabled_by_default(self) -> None:
        config = config_from_env({}, project_root=Path(self._tmp_root))
        self.assertTrue(config.auto_capsule_enabled)

    def test_build_worker_service_skips_capsule_when_disabled(self) -> None:
        root = Path(tempfile.mkdtemp())
        config = WorkerServiceConfig(
            db_path=root / "pilot107.db",
            evidence_root=root / "evidence",
            backend="in-memory",
            auto_capsule_enabled=False,
            capsule_root=root / "capsules",
            health_path=None,
            metrics_root=None,
        )
        worker_service = build_worker_service(config)
        self.assertIsNone(worker_service.stack.worker.capsule_service)

    def test_build_worker_service_wires_capsule_when_enabled(self) -> None:
        root = Path(tempfile.mkdtemp())
        config = WorkerServiceConfig(
            db_path=root / "pilot107.db",
            evidence_root=root / "evidence",
            backend="in-memory",
            auto_capsule_enabled=True,
            capsule_root=root / "capsules",
            health_path=None,
            metrics_root=None,
        )
        worker_service = build_worker_service(config)
        self.assertIsNotNone(worker_service.stack.worker.capsule_service)

    _tmp_root = "/tmp/pilot107-config-tests"


if __name__ == "__main__":
    unittest.main()
