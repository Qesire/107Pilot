from __future__ import annotations

import hashlib
import multiprocessing
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pilot107.adapters.slurm import (
    InMemorySlurmBackend,
    JobSnapshot,
    SubmissionStrategy,
    SubmitIntent,
    SubmitReceipt,
)
from pilot107.core.advice import AgentAdviceError, AgentAdviceService, AgentPolicyEngine
from pilot107.core.agent import AgentExplainService
from pilot107.core.contracts import ContractService, ContractStore, RecipeCatalog
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.core.evidence_binding import EvidenceBinder
from pilot107.core.run_service import RunService
from pilot107.core.run_store import AgentExecutionFenceConflict, RunStore
from pilot107.core.states import RunState
from pilot107.worker.evidence import EvidenceStore
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


class RecordingBackend(InMemorySlurmBackend):
    def __init__(self) -> None:
        super().__init__()
        self.submit_calls = 0

    def submit(self, intent: SubmitIntent) -> SubmitReceipt:
        self.submit_calls += 1
        return super().submit(intent)


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
            job_id="agent-process-job-1",
            run_state=RunState.SUBMITTED,
            strategy=SubmissionStrategy.DEMO,
        )

    def get_job(self, *, user: str, job_id: str) -> JobSnapshot:
        raise AssertionError("get_job is not used in Agent dispatch process test")

    def cancel(self, *, user: str, job_id: str) -> JobSnapshot:
        raise AssertionError("cancel is not used in Agent dispatch process test")


class EnqueueOnlyRepository(SQLiteControlRepository):
    def claim_outbox_message(
        self,
        *,
        message_id: str,
        owner: str,
        lease_seconds: int,
    ) -> None:
        return None


class CrashBeforeAgentAcknowledgeRepository(SQLiteControlRepository):
    def __init__(self, db_path: Path, *, clock: MutableClock) -> None:
        super().__init__(db_path, clock=clock)
        self.crash_once = True

    def acknowledge(self, *, message_id: str, owner: str, fencing_token: int) -> None:
        if message_id.startswith("agent:") and self.crash_once:
            self.crash_once = False
            raise SimulatedProcessCrash("process died before agent outbox ack")
        super().acknowledge(
            message_id=message_id,
            owner=owner,
            fencing_token=fencing_token,
        )


def _dispatch_agent_in_process(
    db_path: str,
    evidence_root: str,
    side_effect_path: str,
    dispatcher_id: str,
) -> None:
    path = Path(db_path)
    store = RunStore(path)
    contract_service = ContractService(
        catalog=RecipeCatalog(),
        store=ContractStore(path),
    )
    run_service = RunService(
        store=store,
        backend=ProcessRecordingBackend(Path(side_effect_path)),
        control_repository=SQLiteControlRepository(path),
        dispatcher_id=dispatcher_id,
    )
    service = AgentAdviceService(
        store=store,
        explain_service=AgentExplainService(
            store=store,
            evidence_binder=EvidenceBinder(
                store=store,
                evidence_root=Path(evidence_root),
            ),
        ),
        policy_engine=AgentPolicyEngine(contract_service=contract_service),
        contract_service=contract_service,
        run_service=run_service,
        dispatcher_id=dispatcher_id,
    )
    batch = service.dispatch_due_executions(limit=1)
    if batch.errors:
        raise RuntimeError(batch.errors[0].message)


class AgentExecutionOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db_path = self.root / "pilot107.db"
        self.clock = MutableClock()
        self.store = RunStore(self.db_path)
        self.evidence_store = EvidenceStore(self.root / "evidence")
        self.contract_service = ContractService(
            catalog=RecipeCatalog(),
            store=ContractStore(self.db_path),
        )
        self.backend = RecordingBackend()
        self.control = SQLiteControlRepository(self.db_path, clock=self.clock)
        self.advice_id, self.action_id = self._approved_action()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_prepare_and_submit_use_distinct_succeeded_outbox_phases(self) -> None:
        service = self._service(dispatcher_id="api-a")

        prepared = service.execute_action(
            self.advice_id,
            action_id=self.action_id,
            actor="alice",
            submit=False,
        )
        submitted = service.execute_action(
            self.advice_id,
            action_id=self.action_id,
            actor="alice",
            submit=True,
        )

        self.assertEqual(prepared.state, "prepared")
        self.assertEqual(submitted.state, "submitted")
        self.assertEqual(self.backend.submit_calls, 1)
        self.assertEqual(
            self.control.get_outbox(f"agent:{submitted.execution_id}:prepare").state,
            "succeeded",
        )
        self.assertEqual(
            self.control.get_outbox(f"agent:{submitted.execution_id}:submit").state,
            "succeeded",
        )

    def test_worker_recovers_message_left_pending_after_enqueue(self) -> None:
        enqueue_only = EnqueueOnlyRepository(self.db_path, clock=self.clock)
        api = self._service(dispatcher_id="api-crashed", control=enqueue_only)
        with self.assertRaises(AgentAdviceError) as raised:
            api.execute_action(
                self.advice_id,
                action_id=self.action_id,
                actor="alice",
                submit=True,
            )
        self.assertEqual(raised.exception.code, "AGENT.EXECUTION_IN_PROGRESS")

        worker = self._service(dispatcher_id="worker-a")
        batch = worker.dispatch_due_executions()

        self.assertEqual(batch.checked, 1)
        self.assertEqual(batch.errors, [])
        self.assertEqual(batch.succeeded[0].state, "submitted")
        self.assertEqual(self.backend.submit_calls, 1)

    def test_two_dispatchers_execute_one_agent_side_effect(self) -> None:
        enqueue_only = EnqueueOnlyRepository(self.db_path, clock=self.clock)
        with self.assertRaises(AgentAdviceError):
            self._service(
                dispatcher_id="api-crashed",
                control=enqueue_only,
            ).execute_action(
                self.advice_id,
                action_id=self.action_id,
                actor="alice",
                submit=True,
            )
        workers = (
            self._service(dispatcher_id="worker-a"),
            self._service(dispatcher_id="worker-b"),
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            batches = list(executor.map(lambda service: service.dispatch_due_executions(), workers))

        self.assertEqual(sum(batch.checked for batch in batches), 1)
        self.assertEqual(sum(len(batch.succeeded) for batch in batches), 1)
        self.assertEqual(self.backend.submit_calls, 1)

    def test_runtime_worker_dispatches_pending_agent_execution(self) -> None:
        enqueue_only = EnqueueOnlyRepository(self.db_path, clock=self.clock)
        with self.assertRaises(AgentAdviceError):
            self._service(
                dispatcher_id="api-crashed",
                control=enqueue_only,
            ).execute_action(
                self.advice_id,
                action_id=self.action_id,
                actor="alice",
                submit=True,
            )
        advice_service = self._service(dispatcher_id="runtime-worker")

        result = RuntimeReconcileWorker(
            service=advice_service.run_service,
            agent_advice_service=advice_service,
            worker_id="runtime-worker",
        ).tick()

        self.assertEqual(result.agent_executions_checked, 1)
        self.assertEqual(result.agent_executions_succeeded, 1)
        self.assertEqual(result.agent_execution_errors, [])
        self.assertEqual(self.backend.submit_calls, 1)

    def test_two_spawned_processes_emit_one_agent_submit_side_effect(self) -> None:
        enqueue_only = EnqueueOnlyRepository(self.db_path, clock=self.clock)
        with self.assertRaises(AgentAdviceError):
            self._service(
                dispatcher_id="api-crashed",
                control=enqueue_only,
            ).execute_action(
                self.advice_id,
                action_id=self.action_id,
                actor="alice",
                submit=True,
            )
        side_effect_path = self.root / "agent-submits.log"
        context = multiprocessing.get_context("spawn")
        processes = [
            context.Process(
                target=_dispatch_agent_in_process,
                args=(
                    str(self.db_path),
                    str(self.evidence_store.root),
                    str(side_effect_path),
                    f"worker-{index}",
                ),
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
        self.assertEqual(len(effects), 1)
        self.assertRegex(effects[0], r"^pilot107-run-[0-9a-f]{20}$")

    def test_execution_write_then_ack_crash_recovers_without_resubmit(self) -> None:
        crashing = CrashBeforeAgentAcknowledgeRepository(self.db_path, clock=self.clock)
        service = self._service(dispatcher_id="api-crashing", control=crashing)

        with self.assertRaises(SimulatedProcessCrash):
            service.execute_action(
                self.advice_id,
                action_id=self.action_id,
                actor="alice",
                submit=True,
            )

        execution = self.store.list_agent_action_executions(self.advice_id)[0]
        self.assertEqual(execution.state, "submitted")
        self.assertEqual(self.backend.submit_calls, 1)
        self.clock.advance(61)
        batch = self._service(dispatcher_id="worker-recovery").dispatch_due_executions()

        self.assertEqual(batch.errors, [])
        self.assertEqual(batch.succeeded[0].state, "submitted")
        self.assertEqual(self.backend.submit_calls, 1)

    def test_terminal_replay_survives_later_source_run_changes(self) -> None:
        service = self._service(dispatcher_id="api-terminal-replay")
        submitted = service.execute_action(
            self.advice_id,
            action_id=self.action_id,
            actor="alice",
            submit=True,
        )
        self.store.update_state(
            "run_agent_outbox_source",
            RunState.FAILED,
            event_type="test.source_changed_after_execution",
        )

        replay = service.execute_action(
            self.advice_id,
            action_id=self.action_id,
            actor="alice",
            submit=True,
        )

        self.assertEqual(replay.execution_id, submitted.execution_id)
        self.assertEqual(replay.state, "submitted")
        self.assertEqual(self.backend.submit_calls, 1)

    def test_phase_boundary_fences_stale_prepare_writer(self) -> None:
        execution_id = _execution_id(self.advice_id, self.action_id)
        prepared, claimed = self.store.claim_agent_action_execution_fenced(
            execution_id=execution_id,
            advice_id=self.advice_id,
            action_id=self.action_id,
            owner="alice",
            submit_requested=False,
            execution_phase="prepare",
            execution_owner="worker-reused",
            fencing_token=1,
        )
        self.assertTrue(claimed)
        self.store.update_agent_action_execution(
            execution_id,
            state="prepared",
            execution_phase="prepare",
            execution_owner="worker-reused",
            fencing_token=1,
        )
        submitting, claimed = self.store.claim_agent_action_execution_fenced(
            execution_id=execution_id,
            advice_id=self.advice_id,
            action_id=self.action_id,
            owner="alice",
            submit_requested=True,
            execution_phase="submit",
            execution_owner="worker-reused",
            fencing_token=1,
        )
        self.assertTrue(claimed)
        self.assertEqual(submitting.execution_phase, "submit")

        with self.assertRaises(AgentExecutionFenceConflict):
            self.store.update_agent_action_execution(
                execution_id,
                state="failed",
                execution_phase="prepare",
                execution_owner="worker-reused",
                fencing_token=1,
            )

    def test_same_phase_reclaim_fences_old_token_with_reused_owner(self) -> None:
        execution_id = _execution_id(self.advice_id, self.action_id)
        _, first_claimed = self.store.claim_agent_action_execution_fenced(
            execution_id=execution_id,
            advice_id=self.advice_id,
            action_id=self.action_id,
            owner="alice",
            submit_requested=True,
            execution_phase="submit",
            execution_owner="worker-reused",
            fencing_token=1,
        )
        _, second_claimed = self.store.claim_agent_action_execution_fenced(
            execution_id=execution_id,
            advice_id=self.advice_id,
            action_id=self.action_id,
            owner="alice",
            submit_requested=True,
            execution_phase="submit",
            execution_owner="worker-reused",
            fencing_token=2,
        )
        self.assertTrue(first_claimed)
        self.assertTrue(second_claimed)

        with self.assertRaises(AgentExecutionFenceConflict):
            self.store.update_agent_action_execution(
                execution_id,
                state="submitted",
                execution_phase="submit",
                execution_owner="worker-reused",
                fencing_token=1,
            )

    def _service(
        self,
        *,
        dispatcher_id: str,
        control: SQLiteControlRepository | None = None,
    ) -> AgentAdviceService:
        repository = control or self.control
        run_service = RunService(
            store=self.store,
            backend=self.backend,
            control_repository=repository,
            dispatcher_id=dispatcher_id,
        )
        explain_service = AgentExplainService(
            store=self.store,
            evidence_binder=EvidenceBinder(
                store=self.store,
                evidence_root=self.evidence_store.root,
            ),
        )
        return AgentAdviceService(
            store=self.store,
            explain_service=explain_service,
            policy_engine=AgentPolicyEngine(contract_service=self.contract_service),
            contract_service=self.contract_service,
            run_service=run_service,
            dispatcher_id=dispatcher_id,
            execution_lease_seconds=60,
            execution_retry_delay_seconds=0,
        )

    def _approved_action(self) -> tuple[str, str]:
        contract = self.contract_service.create(owner="alice", payload=_contract_payload())
        run_id = "run_agent_outbox_source"
        self.store.create_run(
            run_id=run_id,
            contract_id=contract.contract_id,
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\necho contract-ok\n",
        )
        artifact = self.evidence_store.write_text(
            run_id=run_id,
            logical_path="logs/stderr.tail.txt",
            content="TIME LIMIT\n",
            content_type="text/plain",
        )
        evidence_ref = f"evidence://runs/{run_id}/{artifact.logical_path}"
        self.store.upsert_evidence_objects(
            run_id,
            [
                {
                    "object_id": "ev_agent_outbox",
                    "category": "logs",
                    "logical_path": artifact.logical_path,
                    "store_path": str(artifact.path),
                    "source_uri": evidence_ref,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "mime_type": artifact.content_type,
                    "collection_status": "collected",
                    "mutable_during_run": False,
                }
            ],
        )
        self.store.replace_diagnoses(
            run_id,
            [
                {
                    "diagnosis_id": "diag_agent_outbox",
                    "rule_id": "RUNTIME.TIMEOUT",
                    "severity": "error",
                    "summary": "time limit exceeded",
                    "evidence_refs": [evidence_ref],
                    "suggested_patch": {"resources.time_limit": "00:10:00"},
                    "retryable": True,
                    "confidence": "high",
                }
            ],
        )
        service = self._service(dispatcher_id="setup")
        advice = service.advise(run_id, idempotency_key="agent-outbox").record
        action_id = str(advice.payload["actions"][0]["action_id"])
        service.approve(
            advice.advice_id,
            expected_version=1,
            action_ids=[action_id],
            actor="alice",
        )
        return advice.advice_id, action_id


def _execution_id(advice_id: str, action_id: str) -> str:
    digest = hashlib.sha256(f"{advice_id}\0{action_id}".encode()).hexdigest()[:32]
    return f"agentexec_{digest}"


def _contract_payload() -> dict[str, Any]:
    return {
        "recipe_version_id": "recipe_python_cpu@1.0.0",
        "project": {"workdir": "/public/home/alice"},
        "entry": {"command": "echo contract-ok", "expected_outputs": ["result.txt"]},
        "resources": {
            "partition": "debug",
            "qos": "normal",
            "nodes": 1,
            "ntasks": 1,
            "cpus_per_task": 1,
            "time_limit": "00:05:00",
        },
    }


if __name__ == "__main__":
    unittest.main()
