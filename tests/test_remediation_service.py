from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from pilot107.core.advice import AdviceResult
from pilot107.core.remediation import EvaluationOutcome, RemediationBudget, RemediationState
from pilot107.core.remediation_store import RemediationStore
from pilot107.core.run_store import (
    AgentActionExecutionRecord,
    AgentAdviceRecord,
    RunStore,
    utc_now_iso,
)
from pilot107.services.remediation_service import (
    RemediationService,
    RemediationServiceError,
)


class FakeAdviceService:
    def __init__(self) -> None:
        now = utc_now_iso()
        self.record = AgentAdviceRecord(
            advice_id="advice_fake",
            run_id="run_source",
            owner="alice",
            request_key="fake",
            state="ready",
            version=1,
            source_run_updated_at=now,
            evidence_bundle_sha256="e" * 64,
            provider="none",
            model=None,
            payload={
                "schema_version": "AgentAdviceV1",
                "summary": "increase time",
                "actions": [
                    {
                        "action_id": "action_time",
                        "type": "contract_patch_preview",
                        "source": "diagnosis_rule",
                        "risk": "medium",
                        "approval_required": True,
                        "policy_status": "allowed_preview",
                        "proposed_patch": {"resources.time_limit": "00:10:00"},
                    }
                ],
            },
            created_at=now,
            updated_at=now,
        )

    def advise(
        self,
        run_id: str,
        *,
        provider: str = "none",
        idempotency_key: str | None = None,
    ) -> AdviceResult:
        del provider, idempotency_key
        if run_id != self.record.run_id:
            raise KeyError(run_id)
        return AdviceResult(record=self.record, created=True)

    def get(self, advice_id: str) -> AgentAdviceRecord:
        if advice_id != self.record.advice_id:
            raise KeyError(advice_id)
        return self.record

    def approve(
        self,
        advice_id: str,
        *,
        expected_version: int,
        action_ids: list[str],
        actor: str,
        note: str | None = None,
    ) -> AgentAdviceRecord:
        del note
        if (
            advice_id != self.record.advice_id
            or expected_version != self.record.version
            or action_ids != ["action_time"]
            or actor != "alice"
        ):
            raise ValueError("invalid approval")
        self.record = replace(self.record, state="approved", version=2)
        return self.record

    def execute_action(
        self,
        advice_id: str,
        *,
        action_id: str,
        actor: str,
        submit: bool = True,
    ) -> AgentActionExecutionRecord:
        if advice_id != self.record.advice_id or action_id != "action_time" or actor != "alice":
            raise ValueError("invalid execution")
        now = utc_now_iso()
        return AgentActionExecutionRecord(
            execution_id="agentexec_fake",
            advice_id=advice_id,
            action_id=action_id,
            owner=actor,
            state="submitted" if submit else "prepared",
            submit_requested=submit,
            derived_contract_id="contract_derived",
            run_id="run_derived",
            error_code=None,
            error_message=None,
            created_at=now,
            updated_at=now,
        )

    def reject(
        self,
        advice_id: str,
        *,
        expected_version: int,
        actor: str,
        note: str | None = None,
    ) -> AgentAdviceRecord:
        del note
        if (
            advice_id != self.record.advice_id
            or expected_version != self.record.version
            or actor != "alice"
        ):
            raise ValueError("invalid rejection")
        self.record = replace(self.record, state="rejected", version=2)
        return self.record


class RemediationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "pilot107.db"
        self.runs = RunStore(self.db_path)
        self.runs.create_run(
            run_id="run_source",
            owner="alice",
            workdir="/public/home/alice",
            script="exit 42",
            contract_id="contract_source",
        )
        self.runs.create_run(
            run_id="run_derived",
            owner="alice",
            workdir="/public/home/alice",
            script="true",
            parent_run_id="run_source",
            lineage_reason="retry",
        )
        with self.runs.connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET state = 'FAILED', collection_state = 'succeeded',
                    diagnosis_state = 'succeeded'
                WHERE run_id = 'run_source'
                """
            )
        self.remediations = RemediationStore(self.db_path)
        self.advice = FakeAdviceService()
        self.service = RemediationService(
            run_store=self.runs,
            remediation_store=self.remediations,
            advice_service=self.advice,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_create_plan_approve_and_execute(self) -> None:
        created, was_created = self.service.create(
            owner="alice",
            source_run_id="run_source",
            request_key="request-1",
        )
        planned = self.service.advance(created.session_id, worker_id="worker-1")

        self.assertTrue(was_created)
        self.assertEqual(planned.state, RemediationState.AWAITING_APPROVAL)
        proposals = self.remediations.list_proposals(created.session_id)
        self.assertEqual([item.action_id for item in proposals], ["action_time"])

        approved = self.service.approve(
            created.session_id,
            proposal_id=proposals[0].proposal_id,
            actor="alice",
            expected_version=planned.version,
        )
        executing, execution = self.service.execute(
            created.session_id,
            proposal_id=proposals[0].proposal_id,
            actor="alice",
            expected_version=approved.version,
        )

        self.assertEqual(executing.state, RemediationState.EXECUTING)
        self.assertEqual(executing.usage.attempts, 1)
        self.assertEqual(executing.usage.submissions, 1)
        self.assertEqual(execution.derived_run_id, "run_derived")
        detail = self.service.detail(created.session_id, owner="alice")
        self.assertEqual(len(detail["turns"]), 1)
        self.assertEqual(len(detail["proposals"]), 1)
        self.assertEqual(len(detail["decisions"]), 1)
        self.assertEqual(len(detail["executions"]), 1)

        with self.runs.connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET state = 'SUCCEEDED', terminal_state = 'COMPLETED', exit_code = '0:0',
                    collection_state = 'succeeded', diagnosis_state = 'skipped'
                WHERE run_id = 'run_derived'
                """
            )
        succeeded = self.service.advance(created.session_id, worker_id="worker-1")
        self.assertEqual(succeeded.state, RemediationState.SUCCEEDED)
        evaluation = self.remediations.list_evaluations(created.session_id)[0]
        self.assertEqual(evaluation.outcome, EvaluationOutcome.VERIFIED_SUCCESS)

    def test_failed_derived_run_returns_to_planning_until_budget_is_exhausted(self) -> None:
        session, _ = self.service.create(
            owner="alice",
            source_run_id="run_source",
            request_key="request-retry",
            budget=RemediationBudget(max_attempts=2),
        )
        planned = self.service.advance(session.session_id, worker_id="worker-1")
        proposal = self.remediations.list_proposals(session.session_id)[0]
        approved = self.service.approve(
            session.session_id,
            proposal_id=proposal.proposal_id,
            actor="alice",
            expected_version=planned.version,
        )
        self.service.execute(
            session.session_id,
            proposal_id=proposal.proposal_id,
            actor="alice",
            expected_version=approved.version,
        )
        with self.runs.connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET state = 'FAILED', terminal_state = 'TIMEOUT', exit_code = '1:0',
                    collection_state = 'succeeded', diagnosis_state = 'succeeded'
                WHERE run_id = 'run_derived'
                """
            )

        retrying = self.service.advance(session.session_id, worker_id="worker-1")

        self.assertEqual(retrying.state, RemediationState.PLANNING)
        self.assertEqual(
            self.remediations.list_evaluations(session.session_id)[0].outcome,
            EvaluationOutcome.FAILED,
        )

    def test_success_with_degraded_evidence_requires_takeover(self) -> None:
        session, _ = self.service.create(
            owner="alice",
            source_run_id="run_source",
            request_key="request-unverified",
        )
        planned = self.service.advance(session.session_id, worker_id="worker-1")
        proposal = self.remediations.list_proposals(session.session_id)[0]
        approved = self.service.approve(
            session.session_id,
            proposal_id=proposal.proposal_id,
            actor="alice",
            expected_version=planned.version,
        )
        self.service.execute(
            session.session_id,
            proposal_id=proposal.proposal_id,
            actor="alice",
            expected_version=approved.version,
        )
        with self.runs.connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET state = 'SUCCEEDED', terminal_state = 'COMPLETED', exit_code = '0:0',
                    collection_state = 'degraded', diagnosis_state = 'succeeded'
                WHERE run_id = 'run_derived'
                """
            )

        blocked = self.service.advance(session.session_id, worker_id="worker-1")

        self.assertEqual(blocked.state, RemediationState.BLOCKED)
        self.assertEqual(blocked.stop_reason, "execution_success_unverified")

    def test_create_is_owner_scoped_and_idempotent(self) -> None:
        first, first_created = self.service.create(
            owner="alice",
            source_run_id="run_source",
            request_key="request-1",
        )
        duplicate, duplicate_created = self.service.create(
            owner="alice",
            source_run_id="run_source",
            request_key="request-1",
        )
        self.assertTrue(first_created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first, duplicate)

        with self.assertRaises(RemediationServiceError) as captured:
            self.service.create(
                owner="bob",
                source_run_id="run_source",
                request_key="request-bob",
            )
        self.assertEqual(captured.exception.code, "AUTH.FORBIDDEN")

    def test_waits_for_evidence_without_creating_advice(self) -> None:
        with self.runs.connect() as conn:
            conn.execute(
                """
                UPDATE runs SET collection_state = 'pending', diagnosis_state = 'pending'
                WHERE run_id = 'run_source'
                """
            )
        session, _ = self.service.create(
            owner="alice",
            source_run_id="run_source",
            request_key="request-wait",
        )
        waiting = self.service.advance(session.session_id, worker_id="worker-1")
        self.assertEqual(waiting.state, RemediationState.WAITING_EVIDENCE)
        self.assertEqual(self.remediations.list_turns(session.session_id), [])

    def test_zero_submission_budget_stops_before_action(self) -> None:
        session, _ = self.service.create(
            owner="alice",
            source_run_id="run_source",
            request_key="request-budget",
            budget=RemediationBudget(max_submissions=0),
        )
        planned = self.service.advance(session.session_id, worker_id="worker-1")
        proposal = self.remediations.list_proposals(session.session_id)[0]
        approved = self.service.approve(
            session.session_id,
            proposal_id=proposal.proposal_id,
            actor="alice",
            expected_version=planned.version,
        )
        with self.assertRaises(RemediationServiceError) as captured:
            self.service.execute(
                session.session_id,
                proposal_id=proposal.proposal_id,
                actor="alice",
                expected_version=approved.version,
            )
        self.assertEqual(captured.exception.code, "REMEDIATION.BUDGET_EXHAUSTED")
        self.assertEqual(
            self.remediations.get_session(session.session_id).state,
            RemediationState.EXHAUSTED,
        )

    def test_execute_recovers_from_preparing_state(self) -> None:
        session, _ = self.service.create(
            owner="alice",
            source_run_id="run_source",
            request_key="request-preparing-recovery",
        )
        planned = self.service.advance(session.session_id, worker_id="worker-1")
        proposal = self.remediations.list_proposals(session.session_id)[0]
        approved = self.service.approve(
            session.session_id,
            proposal_id=proposal.proposal_id,
            actor="alice",
            expected_version=planned.version,
        )
        preparing = self.remediations.transition(
            session.session_id,
            expected_version=approved.version,
            expected_state=RemediationState.READY,
            target_state=RemediationState.PREPARING,
        )

        executing, execution = self.service.execute(
            session.session_id,
            proposal_id=proposal.proposal_id,
            actor="alice",
            expected_version=preparing.version,
        )

        self.assertEqual(executing.state, RemediationState.EXECUTING)
        self.assertEqual(executing.usage.attempts, 1)
        self.assertEqual(execution.derived_run_id, "run_derived")

    def test_approval_recovers_when_underlying_advice_was_already_approved(self) -> None:
        session, _ = self.service.create(
            owner="alice",
            source_run_id="run_source",
            request_key="request-approval-recovery",
        )
        planned = self.service.advance(session.session_id, worker_id="worker-1")
        proposal = self.remediations.list_proposals(session.session_id)[0]
        self.advice.record = replace(self.advice.record, state="approved", version=2)

        approved = self.service.approve(
            session.session_id,
            proposal_id=proposal.proposal_id,
            actor="alice",
            expected_version=planned.version,
        )

        self.assertEqual(approved.state, RemediationState.READY)
        self.assertEqual(len(self.remediations.list_decisions(session.session_id)), 1)

    def test_owner_can_reject_a_proposal(self) -> None:
        session, _ = self.service.create(
            owner="alice",
            source_run_id="run_source",
            request_key="request-reject",
        )
        planned = self.service.advance(session.session_id, worker_id="worker-1")
        proposal = self.remediations.list_proposals(session.session_id)[0]

        rejected = self.service.reject(
            session.session_id,
            proposal_id=proposal.proposal_id,
            actor="alice",
            expected_version=planned.version,
            note="not safe for this run",
        )

        self.assertEqual(rejected.state, RemediationState.BLOCKED)
        self.assertEqual(rejected.stop_reason, "action_rejected")
        self.assertEqual(self.remediations.list_decisions(session.session_id)[0].decision, "reject")

    def test_owner_can_cancel_and_replay_is_idempotent(self) -> None:
        session, _ = self.service.create(
            owner="alice",
            source_run_id="run_source",
            request_key="request-cancel",
        )

        cancelled = self.service.cancel(
            session.session_id,
            actor="alice",
            expected_version=session.version,
            note="take over manually",
        )
        replayed = self.service.cancel(
            session.session_id,
            actor="alice",
            expected_version=session.version,
        )

        self.assertEqual(cancelled.state, RemediationState.CANCELLED)
        self.assertEqual(cancelled.takeover_reason, "take over manually")
        self.assertEqual(replayed, cancelled)

    def test_owner_can_record_explicit_manual_takeover(self) -> None:
        session, _ = self.service.create(
            owner="alice",
            source_run_id="run_source",
            request_key="request-takeover",
        )

        blocked = self.service.takeover(
            session.session_id,
            actor="alice",
            expected_version=session.version,
            note="derive a Contract in Studio and continue outside this session",
        )
        replayed = self.service.takeover(
            session.session_id,
            actor="alice",
            expected_version=session.version,
            note="ignored on replay",
        )

        self.assertEqual(blocked.state, RemediationState.BLOCKED)
        self.assertEqual(blocked.stop_reason, "manual_takeover")
        self.assertEqual(replayed, blocked)

    def test_worker_advance_uses_persisted_session_provider(self) -> None:
        """The Worker passes ``provider=None`` to ``advance``; the service must
        read the persisted per-session provider and thread it into ``advise``
        so the user's LLM choice is honored on the first planning cycle.
        """
        capturing = CapturingAdviceService(self.advice)
        self.service.advice_service = capturing
        session, _ = self.service.create(
            owner="alice",
            source_run_id="run_source",
            request_key="request-persisted-provider",
            provider="local",
        )

        # Simulate the Worker, which does not know the provider up-front and
        # passes ``provider=None`` so the persisted value is used.
        self.service.advance(session.session_id, worker_id="worker-1", provider=None)

        self.assertEqual(capturing.providers, ["local"])
        stored = self.remediations.get_session(session.session_id)
        self.assertEqual(stored.provider, "local")


class CapturingAdviceService(FakeAdviceService):
    """Record the ``provider`` passed to ``advise`` so tests can assert the
    Worker's auto-advance honors the persisted per-session provider."""

    def __init__(self, inner: FakeAdviceService) -> None:
        super().__init__()
        self.providers: list[str] = []
        # Mirror the inner service's advice record so approve/execute work.
        self.record = inner.record

    def advise(  # type: ignore[override]
        self,
        run_id: str,
        *,
        provider: str = "none",
        idempotency_key: str | None = None,
    ) -> AdviceResult:
        self.providers.append(provider)
        return super().advise(run_id, provider=provider, idempotency_key=idempotency_key)


class VerifyExpectedOutputsTests(unittest.TestCase):
    """P1 (round 4): _verify_expected_outputs strict baseline-vs-final classification.

    Covers the enforce_expected=True branch that the legacy RemediationServiceTests
    (no stores injected) cannot reach. Tests the 4 required scenarios:
    created→ok, missing→not ok, unchanged→not ok, inventory-missing→fail-closed.
    """

    def setUp(self) -> None:
        from pilot107.core.contracts import ContractStore
        from pilot107.services.remediation_service import _verify_expected_outputs
        from pilot107.worker.evidence import EvidenceStore

        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "pilot107.db"
        self.evidence_root = Path(self._tmp.name) / "evidence"
        self.contract_store = ContractStore(self.db_path)
        self.evidence_store = EvidenceStore(self.evidence_root)
        self._verify = _verify_expected_outputs
        self.run_id = "run_derived"
        # Contract declares one expected output.
        self.contract = self.contract_store.create_contract(
            owner="alice",
            recipe_version_id="recipe_python_cpu@1.0.0",
            payload={
                "project": {"workdir": "/public/home/alice"},
                "entry": {
                    "command": "echo ok > out/result.txt",
                    "expected_outputs": ["out/result.txt"],
                },
                "resources": {
                    "partition": "CPU-RC",
                    "qos": "qos_cpu_rc",
                    "nodes": 1,
                    "ntasks": 1,
                    "cpus_per_task": 1,
                    "time_limit": "00:05:00",
                },
            },
        )
        # Minimal derived run record carrying the contract_id.
        self.derived_run = RunStore(self.db_path).create_run(
            run_id=self.run_id,
            owner="alice",
            workdir="/public/home/alice",
            script="true",
            contract_id=self.contract.contract_id,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_inventory(self, files: list[dict]) -> None:
        self.evidence_store.write_json(
            run_id=self.run_id,
            logical_path="outputs/inventory.json",
            payload={"files": files},
        )

    def test_created_expected_output_verifies(self) -> None:
        # Expected output newly produced by the run (baseline absent, now present).
        self._write_inventory([
            {
                "relative_path": "out/result.txt",
                "attribution": "created",
                "baseline_sha256": None,
                "final_sha256": "abc123",
            }
        ])
        result = self._verify(
            run=self.derived_run,
            contract_store=self.contract_store,
            evidence_store=self.evidence_store,
        )
        self.assertTrue(result["resolved"])
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["expected_outputs"]), 1)
        self.assertEqual(result["expected_outputs"][0]["status"], "created")

    def test_missing_expected_output_does_not_verify(self) -> None:
        # Expected output absent from inventory → status "missing" → ok=False.
        self._write_inventory([])
        result = self._verify(
            run=self.derived_run,
            contract_store=self.contract_store,
            evidence_store=self.evidence_store,
        )
        self.assertTrue(result["resolved"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["expected_outputs"][0]["status"], "missing")

    def test_unchanged_expected_output_does_not_verify(self) -> None:
        # Expected output present but attribution "unchanged" (baseline==final) → ok=False.
        self._write_inventory([
            {
                "relative_path": "out/result.txt",
                "attribution": "unchanged",
                "baseline_sha256": "same",
                "final_sha256": "same",
            }
        ])
        result = self._verify(
            run=self.derived_run,
            contract_store=self.contract_store,
            evidence_store=self.evidence_store,
        )
        self.assertTrue(result["resolved"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["expected_outputs"][0]["status"], "unchanged")

    def test_inventory_missing_fails_closed(self) -> None:
        # Stores present + expected outputs declared, but inventory.json absent.
        # Must NOT fall back to legacy VERIFIED_SUCCESS — fail closed.
        result = self._verify(
            run=self.derived_run,
            contract_store=self.contract_store,
            evidence_store=self.evidence_store,
        )
        self.assertTrue(result["resolved"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["expected_outputs"][0]["status"], "inventory_unreadable")

    def test_no_stores_falls_back_to_legacy(self) -> None:
        # Backward compat: no stores injected → resolved=False, ok=True (legacy path).
        result = self._verify(
            run=self.derived_run,
            contract_store=None,
            evidence_store=None,
        )
        self.assertFalse(result["resolved"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["expected_outputs"], [])


if __name__ == "__main__":
    unittest.main()
