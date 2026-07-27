import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_submit_preserves_original_job_name_in_run_record(self) -> None:
        service = RunService(store=self.store, backend=InMemorySlurmBackend())

        run = service.submit(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_plan(),
                job_name="数据预处理 / July batch",
            )
        )

        self.assertEqual(run.job_name, "数据预处理 / July batch")
        self.assertEqual(self.store.get_run(run.run_id).job_name, "数据预处理 / July batch")

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
        # Round-6 P1-2: new payload metadata fields.
        self.assertEqual(payload["baseline_status"], "captured")
        self.assertEqual(payload["total_count"], 1)
        self.assertEqual(payload["baselined_count"], 1)
        self.assertFalse(payload["truncated"])
        self.assertFalse(payload["timeout"])
        self.assertIn("entries", payload)
        entries = payload["entries"]
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

    def test_baseline_truncates_over_32_outputs(self) -> None:
        # Round-6 P1-2: contract declares 33 expected outputs → baseline.json
        # has baselined_count=32, truncated=true, total_count=33.
        contract = self.contract_store.create_contract(
            owner="alice",
            recipe_version_id="recipe_python_cpu@1.0.0",
            payload={
                "project": {"workdir": str(self.workdir)},
                "entry": {
                    "command": "true",
                    "expected_outputs": [f"out_{i}.txt" for i in range(33)],
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
                contract_id=contract.contract_id,
            )
        )
        baseline_path = (
            self.evidence_store.run_root(run.run_id) / "baseline" / "baseline.json"
        )
        self.assertTrue(baseline_path.exists())
        payload = json.loads(baseline_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["total_count"], 33)
        self.assertEqual(payload["baselined_count"], 32)
        self.assertTrue(payload["truncated"])
        self.assertEqual(len(payload["entries"]), 32)

    def test_baseline_skips_invalid_paths(self) -> None:
        # Round-6 P1-2: expected output with absolute path / ``..`` is recorded
        # as path_invalid, not baselined as a file entry.
        contract = self.contract_store.create_contract(
            owner="alice",
            recipe_version_id="recipe_python_cpu@1.0.0",
            payload={
                "project": {"workdir": str(self.workdir)},
                "entry": {
                    "command": "true",
                    "expected_outputs": [
                        "/etc/passwd",  # absolute → path_invalid
                        "../escape.txt",  # parent traversal → path_invalid
                        "valid.txt",  # valid relative path
                    ],
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
                contract_id=contract.contract_id,
            )
        )
        baseline_path = (
            self.evidence_store.run_root(run.run_id) / "baseline" / "baseline.json"
        )
        self.assertTrue(baseline_path.exists())
        payload = json.loads(baseline_path.read_text(encoding="utf-8"))
        entries = payload["entries"]
        by_path = {e["path"]: e for e in entries}
        self.assertIn("/etc/passwd", by_path)
        self.assertEqual(by_path["/etc/passwd"]["status"], "path_invalid")
        self.assertIn("../escape.txt", by_path)
        self.assertEqual(by_path["../escape.txt"]["status"], "path_invalid")
        self.assertIn("valid.txt", by_path)
        # valid.txt was not produced at submit time → exists=false (baselined).
        self.assertIn("exists", by_path["valid.txt"])
        self.assertFalse(by_path["valid.txt"]["exists"])
        # baselined_count counts only successfully processed entries (valid.txt).
        self.assertEqual(payload["baselined_count"], 1)

    def test_baseline_records_status(self) -> None:
        # Round-6 P1-2 + Round-7 P2-1: payload has baseline_status field =
        # "captured" when capture completes successfully with no truncation/timeout.
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
        self.assertEqual(payload["baseline_status"], "captured")
        self.assertFalse(payload["timeout"])
        self.assertFalse(payload["truncated"])

    def test_baseline_status_partial_truncated(self) -> None:
        # Round-7 P2-1: 33 expected outputs → baseline_status=partial_truncated.
        contract = self.contract_store.create_contract(
            owner="alice",
            recipe_version_id="recipe_python_cpu@1.0.0",
            payload={
                "project": {"workdir": str(self.workdir)},
                "entry": {
                    "command": "true",
                    "expected_outputs": [f"out_{i}.txt" for i in range(33)],
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
                contract_id=contract.contract_id,
            )
        )
        payload = json.loads(
            (self.evidence_store.run_root(run.run_id) / "baseline" / "baseline.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(payload["baseline_status"], "partial_truncated")
        self.assertTrue(payload["truncated"])
        self.assertFalse(payload["timeout"])

    def test_baseline_status_not_required_when_no_expected_outputs(self) -> None:
        # Round-7 P2-1: contract with no expected_outputs → baseline_status=not_required.
        contract = self.contract_store.create_contract(
            owner="alice",
            recipe_version_id="recipe_python_cpu@1.0.0",
            payload={
                "project": {"workdir": str(self.workdir)},
                "entry": {"command": "true"},
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
                contract_id=contract.contract_id,
            )
        )
        payload = json.loads(
            (self.evidence_store.run_root(run.run_id) / "baseline" / "baseline.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(payload["baseline_status"], "not_required")
        self.assertEqual(payload["baselined_count"], 0)

    def test_baseline_status_unavailable_when_stores_not_injected(self) -> None:
        # Round-7 P2-1: no stores → baseline NOT written at all (the guard
        # returns before any payload). This is the "unavailable" case — we
        # don't write a payload because there's no evidence_store to write to.
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

    def test_baseline_deadline_stops_loop_with_timeout(self) -> None:
        # Round-7 P1-1: zero budget → falls into the "insufficient budget"
        # branch → baseline_status=unavailable + error_code.
        from unittest.mock import patch

        contract = self.contract_store.create_contract(
            owner="alice",
            recipe_version_id="recipe_python_cpu@1.0.0",
            payload={
                "project": {"workdir": str(self.workdir)},
                "entry": {
                    "command": "true",
                    "expected_outputs": ["a.txt", "b.txt", "c.txt"],
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
        service = self._service()
        with patch.object(RunService, "_baseline_budget", return_value=(0.0, False)):
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
                    contract_id=contract.contract_id,
                )
            )
        payload = json.loads(
            (self.evidence_store.run_root(run.run_id) / "baseline" / "baseline.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(payload["baseline_status"], "unavailable")
        self.assertEqual(payload["error_code"], "baseline_insufficient_budget")
        self.assertEqual(payload["baselined_count"], 0)

    def test_baseline_deadline_aborts_mid_loop(self) -> None:
        # Round-7 P1-1: deadline exceeded mid-loop → timeout=true, remaining
        # entries not started. Patch time.monotonic so the deadline passes
        # after the first entry is processed.
        from unittest.mock import patch

        import pilot107.core.run_service as rs_module

        contract = self.contract_store.create_contract(
            owner="alice",
            recipe_version_id="recipe_python_cpu@1.0.0",
            payload={
                "project": {"workdir": str(self.workdir)},
                "entry": {
                    "command": "true",
                    "expected_outputs": ["first.txt", "second.txt", "third.txt"],
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
        service = self._service()
        call_count = {"n": 0}

        def fake_monotonic() -> float:
            call_count["n"] += 1
            # First few calls (budget calc + deadline set + first entry checks)
            # return a baseline time; subsequent calls exceed the deadline.
            if call_count["n"] <= 4:
                return 1000.0
            return 2000.0

        with patch.object(rs_module.time, "monotonic", side_effect=fake_monotonic):
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
                    contract_id=contract.contract_id,
                )
            )
        payload = json.loads(
            (self.evidence_store.run_root(run.run_id) / "baseline" / "baseline.json")
            .read_text(encoding="utf-8")
        )
        self.assertTrue(payload["timeout"])
        self.assertEqual(payload["baseline_status"], "partial_timeout")
        paths = [e["path"] for e in payload["entries"]]
        # first.txt was processed (missing file → baselined with exists=false).
        self.assertIn("first.txt", paths)
        # second.txt hit the deadline → recorded with status=timeout.
        self.assertIn("second.txt", paths)
        second_entry = next(e for e in payload["entries"] if e["path"] == "second.txt")
        self.assertEqual(second_entry["status"], "timeout")
        # third.txt must NOT appear — loop aborted before it.
        self.assertNotIn("third.txt", paths)

    def test_baseline_chunked_sha_aborts_on_deadline(self) -> None:
        # Round-7 P1-1: local SHA256 streams in chunks and aborts mid-file
        # when the deadline is exceeded. Use a large temp file + patched
        # monotonic so the deadline passes during streaming.
        from unittest.mock import patch

        import pilot107.core.run_service as rs_module

        # Create a large pre-existing file (5 MiB) so streaming takes multiple chunks.
        big_file = self.workdir / "big.bin"
        big_file.write_bytes(b"x" * (5 * 1024 * 1024))
        contract = self.contract_store.create_contract(
            owner="alice",
            recipe_version_id="recipe_python_cpu@1.0.0",
            payload={
                "project": {"workdir": str(self.workdir)},
                "entry": {
                    "command": "true",
                    "expected_outputs": ["big.bin"],
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
        service = self._service()
        call_count = {"n": 0}

        def fake_monotonic() -> float:
            call_count["n"] += 1
            # Initial calls (budget + deadline + stat) return 1000; once SHA
            # streaming begins (call > 6), exceed the deadline.
            if call_count["n"] <= 6:
                return 1000.0
            return 2000.0

        with patch.object(rs_module.time, "monotonic", side_effect=fake_monotonic):
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
                    contract_id=contract.contract_id,
                )
            )
        payload = json.loads(
            (self.evidence_store.run_root(run.run_id) / "baseline" / "baseline.json")
            .read_text(encoding="utf-8")
        )
        self.assertTrue(payload["timeout"])
        self.assertEqual(payload["baseline_status"], "partial_timeout")
        # The entry for big.bin should be present with status=timeout.
        big_entry = next(e for e in payload["entries"] if e["path"] == "big.bin")
        self.assertEqual(big_entry["status"], "timeout")
        # sha256 must NOT be present (streaming aborted before completion).
        self.assertNotIn("sha256", big_entry)

    def test_baseline_budget_reservation(self) -> None:
        # Round-7 P1-1 + Round-8 P1-2: budget = min(30s cap, lease - 15s reserve)
        # when no lease_expires_at (inline path). Returns (budget, insufficient).
        # Default 60s lease → min(30, 45) = 30.
        service = self._service()
        self.assertEqual(service._baseline_budget(), (30.0, False))

        # Lease of 40s → 40-15 = 25 → min(30, 25) = 25 (reduced from cap).
        medium_service = RunService(
            store=self.run_store,
            backend=InMemorySlurmBackend(),
            contract_store=self.contract_store,
            evidence_store=self.evidence_store,
            submission_lease_seconds=40,
        )
        self.assertEqual(medium_service._baseline_budget(), (25.0, False))

        # Tiny lease: 10s → 10-15 = -5 → min(30, -5) = -5 → insufficient.
        tiny_service = RunService(
            store=self.run_store,
            backend=InMemorySlurmBackend(),
            contract_store=self.contract_store,
            evidence_store=self.evidence_store,
            submission_lease_seconds=10,
        )
        budget, insufficient = tiny_service._baseline_budget()
        self.assertLess(budget, 0.0)
        self.assertFalse(insufficient)  # configured-lease fallback, not actual-lease

    def test_baseline_budget_uses_actual_remaining_lease(self) -> None:
        # Round-8 P1-2: when lease_expires_at is provided, budget is computed
        # from the ACTUAL remaining lease, not the configured duration.
        # Simulate a lease that expires 20s from now → 20-15 = 5 → min(30, 5) = 5.
        from datetime import UTC, datetime, timedelta

        expires_soon = (
            datetime.now(UTC) + timedelta(seconds=20)
        ).isoformat()
        service = self._service()
        budget, insufficient = service._baseline_budget(expires_soon)
        self.assertFalse(insufficient)
        # Budget should be ~5s (allow slack for test execution time).
        self.assertLessEqual(budget, 5.5)
        self.assertGreater(budget, 3.0)

        # Lease far from expiry (120s) → 120-15 = 105 → min(30, 105) = 30 (capped).
        expires_far = (
            datetime.now(UTC) + timedelta(seconds=120)
        ).isoformat()
        budget_far, _ = service._baseline_budget(expires_far)
        self.assertEqual(budget_far, 30.0)

    def test_baseline_budget_insufficient_remaining_lease(self) -> None:
        # Round-8 P1-2: lease_expires_at close to now → remaining - reserve < min
        # → insufficient_lease=True.
        from datetime import UTC, datetime, timedelta

        # Expires in 10s → 10-15 = -5 < 0.5 → insufficient.
        expires_soon = (
            datetime.now(UTC) + timedelta(seconds=10)
        ).isoformat()
        service = self._service()
        budget, insufficient = service._baseline_budget(expires_soon)
        self.assertTrue(insufficient)
        self.assertLess(budget, 0.0)

    def test_baseline_skipped_with_insufficient_lease_error_code(self) -> None:
        # Round-8 P1-2: insufficient remaining lease → baseline_status=unavailable
        # + error_code=baseline_insufficient_lease. We simulate this by calling
        # _capture_baseline directly with a near-expiry lease_expires_at.
        from datetime import UTC, datetime, timedelta

        contract = self.contract_store.create_contract(
            owner="alice",
            recipe_version_id="recipe_python_cpu@1.0.0",
            payload={
                "project": {"workdir": str(self.workdir)},
                "entry": {
                    "command": "true",
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
        run = self.run_store.create_run(
            run_id="run_test_insufficient_lease",
            owner="alice",
            workdir=str(self.workdir),
            script="true",
            contract_id=contract.contract_id,
        )
        service = self._service()
        # Lease expires in 5s → 5-15 = -10 < 0.5 → insufficient_lease=True.
        expires_soon = (
            datetime.now(UTC) + timedelta(seconds=5)
        ).isoformat()
        service._capture_baseline(run, lease_expires_at=expires_soon)
        payload = json.loads(
            (self.evidence_store.run_root(run.run_id) / "baseline" / "baseline.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(payload["baseline_status"], "unavailable")
        self.assertEqual(payload["error_code"], "baseline_insufficient_lease")

    def test_baseline_budget_unparseable_lease_fails_closed(self) -> None:
        # Round-11 P1-2: unparseable lease_expires_at → FAIL-CLOSED (not the
        # round-8 fail-open configured-lease fallback). The real remaining
        # lease is unknown; returning insufficient_lease=True makes
        # _capture_baseline skip baseline with error_code=
        # baseline_lease_unparseable, so remediation gets baseline_unavailable
        # for expected outputs → UNVERIFIED, never a false green.
        service = self._service()
        budget, insufficient = service._baseline_budget("not-a-timestamp")
        self.assertTrue(insufficient)
        self.assertEqual(budget, 0.0)

    def test_baseline_skipped_with_unparseable_lease_error_code(self) -> None:
        # Round-11 P1-2: unparseable lease_expires_at at _capture_baseline →
        # baseline_status=unavailable + error_code=baseline_lease_unparseable.
        contract = self.contract_store.create_contract(
            owner="alice",
            recipe_version_id="recipe_python_cpu@1.0.0",
            payload={
                "project": {"workdir": str(self.workdir)},
                "entry": {
                    "command": "true",
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
        run = self.run_store.create_run(
            run_id="run_test_unparseable_lease",
            owner="alice",
            workdir=str(self.workdir),
            script="true",
            contract_id=contract.contract_id,
        )
        service = self._service()
        service._capture_baseline(run, lease_expires_at="not-a-timestamp")
        payload = json.loads(
            (self.evidence_store.run_root(run.run_id) / "baseline" / "baseline.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(payload["baseline_status"], "unavailable")
        self.assertEqual(payload["error_code"], "baseline_lease_unparseable")

    def test_baseline_failed_writes_payload_with_error_code(self) -> None:
        # Round-7 P2-1: an exception during capture writes baseline.json with
        # baseline_status=failed + error_code (does NOT silently return).
        from unittest.mock import patch

        contract = self.contract_store.create_contract(
            owner="alice",
            recipe_version_id="recipe_python_cpu@1.0.0",
            payload={
                "project": {"workdir": str(self.workdir)},
                "entry": {
                    "command": "true",
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
        service = self._service()
        with patch(
            "pilot107.core.run_service._resolve_expected_outputs",
            side_effect=RuntimeError("boom"),
        ):
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
                    contract_id=contract.contract_id,
                )
            )
        baseline_path = (
            self.evidence_store.run_root(run.run_id) / "baseline" / "baseline.json"
        )
        self.assertTrue(baseline_path.exists())
        payload = json.loads(baseline_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["baseline_status"], "failed")
        self.assertEqual(payload["error_code"], "baseline_exception")


class BaselineStatClassificationTests(unittest.TestCase):
    """Round-11 P1-1: a non-ENOENT stat/OSError must NOT become exists=false.

    Only confirmed FileNotFoundError / ENOENT produces a trusted
    ``_baseline_missing`` entry. Permission denied, I/O error, and other
    failures carry ``status="error"`` (or ``"timeout"``) so evidence.py's
    ``_baseline_entry_unavailable`` rejects them as ``baseline_unavailable``
    instead of letting a pre-existing file appear as ``created`` (false
    verified_success).
    """

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
                "entry": {"command": "true", "expected_outputs": ["result.txt"]},
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

    def _make_run(self, run_id: str):
        return self.run_store.create_run(
            run_id=run_id,
            owner="alice",
            workdir=str(self.workdir),
            script="true",
            contract_id=self.contract.contract_id,
        )

    def _service(self, **kwargs) -> RunService:
        return RunService(
            store=self.run_store,
            backend=InMemorySlurmBackend(),
            contract_store=self.contract_store,
            evidence_store=self.evidence_store,
            **kwargs,
        )

    def _capture(self, service: RunService, run, **kwargs) -> dict:
        service._capture_baseline(run, **kwargs)
        return json.loads(
            (self.evidence_store.run_root(run.run_id) / "baseline" / "baseline.json")
            .read_text(encoding="utf-8")
        )

    def test_local_stat_permission_error_is_status_error(self) -> None:
        # Pre-existing file: os.stat raises PermissionError (EACCES). Must NOT
        # be treated as missing (exists=false) — that would let the final file
        # appear as "created". Entry carries status=error + errno_13.
        run = self._make_run("run_stat_eacces")
        service = self._service()
        with patch("pilot107.core.run_service.os.stat", side_effect=PermissionError(13, "denied")):
            payload = self._capture(service, run)
        entry = payload["entries"][0]
        self.assertEqual(entry["status"], "error")
        self.assertEqual(entry["error_code"], "errno_13")
        self.assertNotIn("exists", entry)

    def test_local_stat_io_error_is_status_error(self) -> None:
        # os.stat raises OSError(EIO). status=error + errno_5.
        run = self._make_run("run_stat_eio")
        service = self._service()
        with patch(
            "pilot107.core.run_service.os.stat",
            side_effect=OSError(5, "I/O error"),
        ):
            payload = self._capture(service, run)
        entry = payload["entries"][0]
        self.assertEqual(entry["status"], "error")
        self.assertEqual(entry["error_code"], "errno_5")

    def test_local_stat_filenotfound_is_baseline_missing(self) -> None:
        # Genuine FileNotFoundError → _baseline_missing (no status, exists=false).
        # This is the ONLY path to a trusted exists=false entry.
        run = self._make_run("run_stat_enoent")
        service = self._service()
        with patch(
            "pilot107.core.run_service.os.stat",
            side_effect=FileNotFoundError(2, "no such file"),
        ):
            payload = self._capture(service, run)
        entry = payload["entries"][0]
        self.assertNotIn("status", entry)
        self.assertFalse(entry["exists"])
        self.assertIsNone(entry["sha256"])

    def test_simulator_stat_enoent_is_baseline_missing(self) -> None:
        from pilot107.adapters.slurm import CommandResult

        class FakeExecutor:
            def run(self, argv, *, cwd=None, user=None, stdin=None, timeout_seconds=10.0):
                return CommandResult(
                    returncode=1,
                    stdout="",
                    stderr="stat: cannot statx 'result.txt': No such file or directory",
                )

            def realpath(self, path, *, timeout_seconds=10.0):
                return path

            def write_text(self, *, path, content, owner, timeout_seconds=10.0):
                return None

        run = self._make_run("run_sim_enoent")
        service = self._service(baseline_executor=FakeExecutor())
        payload = self._capture(service, run)
        entry = payload["entries"][0]
        self.assertNotIn("status", entry)
        self.assertFalse(entry["exists"])

    def test_simulator_stat_permission_denied_is_status_error(self) -> None:
        from pilot107.adapters.slurm import CommandResult

        class FakeExecutor:
            def run(self, argv, *, cwd=None, user=None, stdin=None, timeout_seconds=10.0):
                return CommandResult(
                    returncode=1, stdout="", stderr="stat: cannot stat: Permission denied",
                )

            def realpath(self, path, *, timeout_seconds=10.0):
                return path

            def write_text(self, *, path, content, owner, timeout_seconds=10.0):
                return None

        run = self._make_run("run_sim_eacces")
        service = self._service(baseline_executor=FakeExecutor())
        payload = self._capture(service, run)
        entry = payload["entries"][0]
        self.assertEqual(entry["status"], "error")
        self.assertEqual(entry["error_code"], "stat_permission_denied")

    def test_simulator_stat_timeout_is_status_timeout(self) -> None:
        from pilot107.adapters.slurm import CommandResult

        class FakeExecutor:
            def run(self, argv, *, cwd=None, user=None, stdin=None, timeout_seconds=10.0):
                return CommandResult(
                    returncode=124, stdout="", stderr="command timed out",
                )

            def realpath(self, path, *, timeout_seconds=10.0):
                return path

            def write_text(self, *, path, content, owner, timeout_seconds=10.0):
                return None

        run = self._make_run("run_sim_timeout")
        service = self._service(baseline_executor=FakeExecutor())
        payload = self._capture(service, run)
        entry = payload["entries"][0]
        self.assertEqual(entry["status"], "timeout")
        self.assertEqual(entry["error_code"], "stat_timeout")

    def test_simulator_stat_unclassified_is_status_error(self) -> None:
        from pilot107.adapters.slurm import CommandResult

        class FakeExecutor:
            def run(self, argv, *, cwd=None, user=None, stdin=None, timeout_seconds=10.0):
                # Non-zero, no recognizable marker → fail-closed as error.
                return CommandResult(returncode=2, stdout="", stderr="weird gateway hiccup")

            def realpath(self, path, *, timeout_seconds=10.0):
                return path

            def write_text(self, *, path, content, owner, timeout_seconds=10.0):
                return None

        run = self._make_run("run_sim_unclassified")
        service = self._service(baseline_executor=FakeExecutor())
        payload = self._capture(service, run)
        entry = payload["entries"][0]
        self.assertEqual(entry["status"], "error")
        self.assertEqual(entry["error_code"], "stat_unclassified")


class SubmissionLeaseEnforcementTests(unittest.TestCase):
    """Round-11 P1-2: renew-before-submit + receipt lease check (fake clock).

    Uses the real ControlRepository (sqlite) with an outbox message and a
    stub backend to prove the lease is renewed before submit and the receipt
    is refused when the lease expired during submit.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "pilot107.db"
        self.run_store = RunStore(self.db_path)
        self.contract_store = ContractStore(self.db_path)
        self.evidence_store = EvidenceStore(Path(self._tmp.name) / "evidence")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_renew_before_submit_proceeds_with_fresh_lease(self) -> None:
        from pilot107.core.control_repository import SQLiteControlRepository
        from pilot107.core.run_service import _SUBMIT_LEASE_RESERVE_SECONDS

        repo = SQLiteControlRepository(self.db_path)
        # Enqueue + claim a submission outbox message.
        message, _ = repo.enqueue(
            message_id="msg_renew_ok",
            topic="run.submit",
            aggregate_id="run_renew_ok",
            payload={"run_id": "run_renew_ok"},
        )
        claimed = repo.claim_outbox_message(
            message_id=message.message_id,
            owner="dispatcher-a",
            lease_seconds=60,
        )
        assert claimed is not None
        renew_calls: list[int] = []

        class RecordingRepo(SQLiteControlRepository):
            def renew_outbox(self, *, message_id, owner, fencing_token, lease_seconds):
                renew_calls.append(fencing_token)
                return super().renew_outbox(
                    message_id=message_id,
                    owner=owner,
                    fencing_token=fencing_token,
                    lease_seconds=lease_seconds,
                )

        recording_repo = RecordingRepo(self.db_path)
        service = RunService(
            store=self.run_store,
            backend=InMemorySlurmBackend(),
            control_repository=recording_repo,
            submission_lease_seconds=60,
        )
        renewed = service._renew_lease_for_submit(claimed)
        self.assertEqual(renew_calls, [claimed.fencing_token])
        # Renewed lease must have at least the reserve remaining.
        remaining = service._lease_remaining_seconds(renewed.lease_expires_at)
        self.assertIsNotNone(remaining)
        self.assertGreaterEqual(remaining or 0.0, _SUBMIT_LEASE_RESERVE_SECONDS)
        # Sanity: the not-expired path does NOT raise.
        service._assert_lease_valid_for_receipt(renewed)

    def test_renew_returns_expired_lease_aborts_submit(self) -> None:
        # When the renewed lease's remaining time is below the submit reserve,
        # _renew_lease_for_submit raises SubmissionLeaseExpiredError (no submit).
        from datetime import UTC, datetime, timedelta

        from pilot107.core.control_repository import OutboxMessage
        from pilot107.core.run_service import (
            RunService,
            SubmissionLeaseExpiredError,
        )

        # A message whose renewed expiry is already in the past.
        expired = OutboxMessage(
            message_id="m1",
            topic="run.submit",
            aggregate_id="run1",
            payload={"run_id": "run1"},
            state="running",
            available_at=datetime.now(UTC).isoformat(),
            lease_owner="dispatcher-a",
            lease_expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            fencing_token=1,
            attempts=0,
            last_error=None,
            created_at=datetime.now(UTC).isoformat(),
            updated_at=datetime.now(UTC).isoformat(),
        )

        class _StubRepo:
            def renew_outbox(self, *, message_id, owner, fencing_token, lease_seconds):
                return expired

        service = RunService(
            store=self.run_store,
            backend=InMemorySlurmBackend(),
            control_repository=_StubRepo(),  # type: ignore[arg-type]
            submission_lease_seconds=60,
        )
        with self.assertRaises(SubmissionLeaseExpiredError):
            service._renew_lease_for_submit(expired)

    def test_receipt_refused_when_lease_expired_during_submit(self) -> None:
        # After submit returns, the lease check refuses to persist the receipt
        # if the lease expired mid-submit.
        from datetime import UTC, datetime, timedelta

        from pilot107.core.control_repository import OutboxMessage
        from pilot107.core.run_service import (
            RunService,
            SubmissionLeaseExpiredError,
        )

        service = RunService(
            store=self.run_store,
            backend=InMemorySlurmBackend(),
            submission_lease_seconds=60,
        )
        expired = OutboxMessage(
            message_id="m2",
            topic="run.submit",
            aggregate_id="run2",
            payload={"run_id": "run2"},
            state="running",
            available_at=datetime.now(UTC).isoformat(),
            lease_owner="dispatcher-a",
            lease_expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            fencing_token=1,
            attempts=0,
            last_error=None,
            created_at=datetime.now(UTC).isoformat(),
            updated_at=datetime.now(UTC).isoformat(),
        )
        with self.assertRaises(SubmissionLeaseExpiredError):
            service._assert_lease_valid_for_receipt(expired)

    def test_multi_dispatcher_renew_raises_fence_for_lower_token(self) -> None:
        # Dispatcher A holds the fence; dispatcher B cannot renew A's message
        # (renew checks lease_owner + fencing_token). A's renew succeeds and
        # keeps its token; B's renew on A's message raises ControlRepositoryConflict.
        from pilot107.core.control_repository import (
            ControlRepositoryConflict,
            SQLiteControlRepository,
        )

        repo = SQLiteControlRepository(self.db_path)
        message, _ = repo.enqueue(
            message_id="msg_fence",
            topic="run.submit",
            aggregate_id="run_fence",
            payload={"run_id": "run_fence"},
        )
        claimed = repo.claim_outbox_message(
            message_id=message.message_id,
            owner="dispatcher-a",
            lease_seconds=60,
        )
        assert claimed is not None
        # A renews — succeeds, same token.
        renewed = repo.renew_outbox(
            message_id=message.message_id,
            owner="dispatcher-a",
            fencing_token=claimed.fencing_token,
            lease_seconds=60,
        )
        self.assertEqual(renewed.fencing_token, claimed.fencing_token)
        # B tries to renew A's message with a DIFFERENT token → conflict.
        with self.assertRaises(ControlRepositoryConflict):
            repo.renew_outbox(
                message_id=message.message_id,
                owner="dispatcher-b",
                fencing_token=claimed.fencing_token + 1,
                lease_seconds=60,
            )


if __name__ == "__main__":
    unittest.main()
