from __future__ import annotations

import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pilot107.adapters.slurm import (
    InMemorySlurmBackend,
    JobSnapshot,
    SubmissionStrategy,
    SubmitReceipt,
)
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.core.run_service import RunService
from pilot107.core.run_store import RunRecord, RunStore
from pilot107.core.states import RunState
from pilot107.worker.evidence import EvidenceCollectionResult
from pilot107.worker.runtime_worker import RuntimeReconcileWorker


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class SimulatedProcessCrash(BaseException):
    pass


class RecordingCollectionHandler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def collect(self, *, run: RunRecord, task_type: str) -> EvidenceCollectionResult:
        with self.lock:
            self.calls.append((run.run_id, task_type))
        return EvidenceCollectionResult(run_id=run.run_id, task_type=task_type, artifacts=[])


class BlockingCollectionHandler(RecordingCollectionHandler):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def collect(self, *, run: RunRecord, task_type: str) -> EvidenceCollectionResult:
        with self.lock:
            first = not self.calls
            self.calls.append((run.run_id, task_type))
        if first:
            self.started.set()
            if not self.release.wait(timeout=5):
                raise TimeoutError("test did not release blocking collector")
        return EvidenceCollectionResult(run_id=run.run_id, task_type=task_type, artifacts=[])


class ProcessCollectionHandler:
    def __init__(self, side_effect_path: Path) -> None:
        self.side_effect_path = side_effect_path

    def collect(self, *, run: RunRecord, task_type: str) -> EvidenceCollectionResult:
        descriptor = os.open(
            self.side_effect_path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(descriptor, f"{run.run_id}:{task_type}\n".encode())
        finally:
            os.close(descriptor)
        return EvidenceCollectionResult(run_id=run.run_id, task_type=task_type, artifacts=[])


class CrashBeforeAcknowledgeRepository(SQLiteControlRepository):
    def __init__(self, db_path: Path, *, clock: MutableClock) -> None:
        super().__init__(db_path, clock=clock)
        self.crash_once = True

    def acknowledge(self, *, message_id: str, owner: str, fencing_token: int) -> None:
        if message_id.startswith("collection:") and self.crash_once:
            self.crash_once = False
            raise SimulatedProcessCrash("process died before collection outbox ack")
        super().acknowledge(
            message_id=message_id,
            owner=owner,
            fencing_token=fencing_token,
        )


def _dispatch_collections_in_process(
    db_path: str,
    side_effect_path: str,
    worker_id: str,
) -> None:
    path = Path(db_path)
    service = RunService(
        store=RunStore(path),
        backend=InMemorySlurmBackend(),
        control_repository=SQLiteControlRepository(path),
        dispatcher_id=worker_id,
    )
    result = RuntimeReconcileWorker(
        service=service,
        task_handler=ProcessCollectionHandler(Path(side_effect_path)),
        worker_id=worker_id,
    ).tick()
    if result.task_errors:
        raise RuntimeError(result.task_errors[0].message)


class CollectionOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "pilot107.db"
        self.clock = MutableClock()
        self.store = RunStore(self.db_path)
        self.control = SQLiteControlRepository(self.db_path, clock=self.clock)
        self.run = self._terminal_run()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_collection_tasks_are_dispatched_and_acknowledged_from_outbox(self) -> None:
        handler = RecordingCollectionHandler()
        result = self._worker(handler=handler, worker_id="worker-a").tick()

        self.assertEqual(result.tasks_checked, 7)
        self.assertEqual(result.tasks_succeeded, 7)
        self.assertEqual(result.task_errors, [])
        self.assertEqual(len(handler.calls), 7)
        tasks = self.store.list_collection_tasks(self.run.run_id)
        self.assertEqual({task["state"] for task in tasks}, {"succeeded"})
        self.assertTrue(all(task["fencing_token"] == 1 for task in tasks))

    def test_two_threads_collect_each_task_exactly_once(self) -> None:
        handler = RecordingCollectionHandler()
        workers = (
            self._worker(handler=handler, worker_id="worker-a"),
            self._worker(handler=handler, worker_id="worker-b"),
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda worker: worker.tick(), workers))

        self.assertEqual(sum(result.tasks_checked for result in results), 7)
        self.assertEqual(sum(result.tasks_succeeded for result in results), 7)
        self.assertEqual(len(handler.calls), 7)
        self.assertEqual(len(set(handler.calls)), 7)

    def test_reactivated_runtime_task_uses_a_new_outbox_generation(self) -> None:
        handler = RecordingCollectionHandler()
        worker = self._worker(handler=handler, worker_id="worker-generation")
        worker.tick()
        runtime_task = next(
            task
            for task in self.store.list_collection_tasks(self.run.run_id)
            if task["task_type"] == "runtime_status"
        )

        self.store.apply_snapshot(
            self.run.run_id,
            JobSnapshot(
                job_id="collection-job-1",
                owner="alice",
                run_state=RunState.RUNNING,
                raw_state_flags=["RUNNING"],
            ),
        )
        result = worker.tick()

        current = self.store.get_collection_task(runtime_task["task_id"])
        self.assertEqual(result.tasks_checked, 1)
        self.assertEqual(result.tasks_succeeded, 1)
        self.assertEqual(current.generation, 2)
        self.assertEqual(
            self.control.get_outbox(f"collection:{current.task_id}:1").state,
            "succeeded",
        )
        self.assertEqual(
            self.control.get_outbox(f"collection:{current.task_id}:2").state,
            "succeeded",
        )

    def test_two_spawned_processes_emit_one_side_effect_per_task(self) -> None:
        side_effect_path = Path(self.tempdir.name) / "collection-side-effects.log"
        context = multiprocessing.get_context("spawn")
        processes = [
            context.Process(
                target=_dispatch_collections_in_process,
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
        effects = side_effect_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(effects), 7)
        self.assertEqual(len(set(effects)), 7)

    def test_heartbeat_prevents_reclaim_during_a_slow_collector(self) -> None:
        handler = BlockingCollectionHandler()
        real_control = SQLiteControlRepository(self.db_path)
        first = self._worker(
            handler=handler,
            worker_id="worker-slow",
            control=real_control,
            lease_seconds=1,
        )
        second = self._worker(
            handler=handler,
            worker_id="worker-fast",
            control=real_control,
            lease_seconds=1,
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            future = executor.submit(first.tick)
            self.assertTrue(handler.started.wait(timeout=2))
            time.sleep(1.2)
            fast_result = second.tick()
            handler.release.set()
            slow_result = future.result(timeout=5)

        self.assertEqual(fast_result.task_errors, [])
        self.assertEqual(slow_result.task_errors, [])
        self.assertEqual(len(handler.calls), 7)
        self.assertEqual(len(set(handler.calls)), 7)

    def test_task_write_then_ack_crash_recovers_without_recollecting(self) -> None:
        handler = RecordingCollectionHandler()
        crashing = CrashBeforeAcknowledgeRepository(self.db_path, clock=self.clock)
        worker = self._worker(
            handler=handler,
            worker_id="worker-crashing",
            control=crashing,
        )

        with self.assertRaises(SimulatedProcessCrash):
            worker.tick()

        self.assertEqual(len(handler.calls), 1)
        self.clock.advance(301)
        recovered = self._worker(handler=handler, worker_id="worker-recovery").tick()

        self.assertEqual(recovered.task_errors, [])
        self.assertEqual(len(handler.calls), 7)
        self.assertEqual(len(set(handler.calls)), 7)

    def _worker(
        self,
        *,
        handler: RecordingCollectionHandler,
        worker_id: str,
        control: SQLiteControlRepository | None = None,
        lease_seconds: int = 300,
    ) -> RuntimeReconcileWorker:
        service = RunService(
            store=self.store,
            backend=InMemorySlurmBackend(),
            control_repository=control or self.control,
            dispatcher_id=worker_id,
        )
        return RuntimeReconcileWorker(
            service=service,
            task_handler=handler,
            worker_id=worker_id,
            task_lease_seconds=lease_seconds,
        )

    def _terminal_run(self) -> RunRecord:
        run = self.store.create_run(
            run_id="run_collection_outbox",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\nhostname\n",
        )
        self.store.apply_submit_receipt(
            run.run_id,
            SubmitReceipt(
                job_id="collection-job-1",
                run_state=RunState.SUBMITTED,
                strategy=SubmissionStrategy.IN_MEMORY,
            ),
        )
        return self.store.apply_snapshot(
            run.run_id,
            JobSnapshot(
                job_id="collection-job-1",
                owner="alice",
                run_state=RunState.SUCCEEDED,
                raw_state_flags=["COMPLETED"],
                exit_code="0:0",
            ),
        )


if __name__ == "__main__":
    unittest.main()
