import json
import tempfile
import unittest
from pathlib import Path

from pilot107.adapters.slurm import InMemorySlurmBackend, SlurmSubmissionRejected, SubmitIntent
from pilot107.core.contracts import ContractStore
from pilot107.core.resources import ResourcePlan
from pilot107.core.run_service import RunService, RunSubmitRequest
from pilot107.core.run_store import RunStore
from pilot107.core.states import CollectionState, ResultStatus, RunState
from pilot107.worker.evidence import EvidenceStore


def _plan(time_limit: str | None = "00:05:00") -> ResourcePlan:
    return ResourcePlan(
        partition="debug",
        qos="normal",
        nodes=1,
        ntasks=1,
        cpus_per_task=1,
        time_limit=time_limit,
    )


class RejectingBackend(InMemorySlurmBackend):
    def submit(self, intent: SubmitIntent):
        raise SlurmSubmissionRejected("no partition")


class RunServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "pilot107.db"
        self.store = RunStore(self.db_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_submit_persists_submitted_run_and_event(self) -> None:
        service = RunService(store=self.store, backend=InMemorySlurmBackend())

        run = service.submit(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_plan(),
            )
        )

        self.assertEqual(run.state, RunState.SUBMITTED)
        self.assertEqual(run.owner, "alice")
        self.assertIsNotNone(run.job_id)
        self.assertEqual(run.submit_strategy, "in_memory")
        self.assertEqual([event.event_type for event in self.store.list_events(run.run_id)], [
            "run.created",
            "run.submitting",
            "run.submitted",
        ])
        self.assertEqual(
            {task["task_type"] for task in self.store.list_collection_tasks(run.run_id)},
            {"submission_snapshot", "runtime_status"},
        )

    def test_reconcile_terminal_run_creates_terminal_tasks(self) -> None:
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

        reconciled = service.reconcile_once(run.run_id)

        self.assertEqual(reconciled.state, RunState.SUCCEEDED)
        self.assertEqual(reconciled.exit_code, "0:0")
        self.assertEqual(reconciled.result_status, ResultStatus.COMPLETE)
        task_types = {task["task_type"] for task in self.store.list_collection_tasks(run.run_id)}
        self.assertIn("submission_snapshot", task_types)
        self.assertIn("runtime_status", task_types)
        self.assertIn("terminal_accounting", task_types)
        self.assertIn("logs_finalize", task_types)
        self.assertIn("environment_finalize", task_types)
        self.assertIn("outputs_inventory", task_types)
        self.assertIn("result_summary", task_types)

    def test_nonterminal_reconcile_reactivates_runtime_status_and_records_reason(self) -> None:
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
        runtime_task = next(
            task
            for task in self.store.list_collection_tasks(run.run_id)
            if task["task_type"] == "runtime_status"
        )
        self.store.mark_collection_task_succeeded(runtime_task["task_id"])
        backend.advance_job(
            job_id=run.job_id or "",
            raw_state="PENDING",
            reason="Resources",
        )

        reconciled = service.reconcile_once(run.run_id)

        self.assertEqual(reconciled.state, RunState.PENDING)
        runtime_task = next(
            task
            for task in self.store.list_collection_tasks(run.run_id)
            if task["task_type"] == "runtime_status"
        )
        self.assertEqual(runtime_task["state"], "pending")
        self.assertEqual(reconciled.collection_state, CollectionState.PENDING)
        snapshot_event = self.store.list_events(run.run_id)[-1]
        self.assertEqual(snapshot_event.event_type, "run.snapshot")
        self.assertEqual(snapshot_event.payload["reason"], "Resources")

    def test_submit_failure_marks_run_submit_failed(self) -> None:
        service = RunService(store=self.store, backend=RejectingBackend())

        with self.assertRaises(SlurmSubmissionRejected):
            service.submit(
                RunSubmitRequest(
                    owner="alice",
                    workdir=Path("/public/home/alice"),
                    script="#!/bin/bash\nhostname\n",
                    resource_plan=_plan(),
                )
            )

        events = self.store.list_events(self._only_run_id())
        self.assertEqual(events[-1].event_type, "run.submit_failed")
        self.assertEqual(self.store.get_run(self._only_run_id()).state, RunState.SUBMIT_FAILED)

    def test_cancel_updates_state(self) -> None:
        service = RunService(store=self.store, backend=InMemorySlurmBackend())
        run = service.submit(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\nsleep 30\n",
                resource_plan=_plan(),
            )
        )

        cancelled = service.cancel(run.run_id)

        self.assertEqual(cancelled.state, RunState.CANCELLED)
        self.assertEqual(cancelled.terminal_state, "CANCELLED")

    def _only_run_id(self) -> str:
        with self.store.connect() as conn:
            row = conn.execute("SELECT run_id FROM runs").fetchone()
        assert row is not None
        return str(row["run_id"])


class BaselineCaptureTests(unittest.TestCase):
    """A1: RunService captures a pre-run baseline of declared expected outputs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "pilot107.db"
        self.evidence_root = Path(self._tmp.name) / "evidence"
        self.run_store = RunStore(self.db_path)
        self.contract_store = ContractStore(self.db_path)
        self.evidence_store = EvidenceStore(self.evidence_root)
        self.workdir = Path(self._tmp.name) / "workdir"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.contract = self.contract_store.create_contract(
            owner="alice",
            recipe_version_id="recipe_python_cpu@1.0.0",
            payload={
                "project": {"workdir": str(self.workdir)},
                "entry": {
                    "command": "echo hi",
                    "expected_outputs": ["result.txt"],
                },
                "resources": {
                    "partition": "debug",
                    "qos": "normal",
                    "nodes": 1,
                    "ntasks": 1,
                    "cpus_per_task": 1,
                    "time_limit": "00:05:00",
                },
            },
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _service(self) -> RunService:
        return RunService(
            store=self.run_store,
            backend=InMemorySlurmBackend(),
            contract_store=self.contract_store,
            evidence_store=self.evidence_store,
        )

    def test_baseline_written_when_stores_injected(self) -> None:
        service = self._service()
        run = service.submit(
            RunSubmitRequest(
                owner="alice",
                workdir=self.workdir,
                script="#!/bin/bash\necho hi > result.txt\n",
                resource_plan=ResourcePlan(
                    partition="debug",
                    qos="normal",
                    nodes=1,
                    ntasks=1,
                    cpus_per_task=1,
                    time_limit="00:05:00",
                ),
                contract_id=self.contract.contract_id,
            )
        )
        baseline_path = (
            self.evidence_store.run_root(run.run_id) / "baseline" / "baseline.json"
        )
        self.assertTrue(baseline_path.exists())
        payload = json.loads(baseline_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], "pilot107.baseline.v1")
        self.assertEqual(payload["contract_id"], self.contract.contract_id)
        self.assertIn("captured_at_epoch", payload)
        entries = payload["expected_outputs"]
        self.assertEqual([entry["path"] for entry in entries], ["result.txt"])
        # Expected output not yet produced at submit time => exists=false.
        self.assertFalse(entries[0]["exists"])
        self.assertIsNone(entries[0]["sha256"])

    def test_baseline_skipped_when_stores_not_injected(self) -> None:
        # No contract_store / evidence_store => baseline capture is silently
        # skipped; submit still succeeds and no baseline.json is written.
        service = RunService(store=self.run_store, backend=InMemorySlurmBackend())
        run = service.submit(
            RunSubmitRequest(
                owner="alice",
                workdir=self.workdir,
                script="#!/bin/bash\ntrue\n",
                resource_plan=ResourcePlan(
                    partition="debug",
                    qos="normal",
                    nodes=1,
                    ntasks=1,
                    cpus_per_task=1,
                    time_limit="00:05:00",
                ),
                contract_id=self.contract.contract_id,
            )
        )
        baseline_path = (
            self.evidence_store.run_root(run.run_id) / "baseline" / "baseline.json"
        )
        self.assertFalse(baseline_path.exists())

    def test_baseline_skipped_when_contract_id_missing(self) -> None:
        service = self._service()
        run = service.submit(
            RunSubmitRequest(
                owner="alice",
                workdir=self.workdir,
                script="#!/bin/bash\ntrue\n",
                resource_plan=ResourcePlan(
                    partition="debug",
                    qos="normal",
                    nodes=1,
                    ntasks=1,
                    cpus_per_task=1,
                    time_limit="00:05:00",
                ),
            )
        )
        baseline_path = (
            self.evidence_store.run_root(run.run_id) / "baseline" / "baseline.json"
        )
        self.assertFalse(baseline_path.exists())


if __name__ == "__main__":
    unittest.main()
