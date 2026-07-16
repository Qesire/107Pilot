from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from pilot107.core.remediation import (
    ActionDecision,
    ActionExecution,
    EvaluationOutcome,
    EvaluationResult,
    RemediationBudget,
    RemediationConflict,
    RemediationState,
    RemediationUsage,
)
from pilot107.core.remediation_store import RemediationStore
from pilot107.core.run_store import RunStore


class RemediationStoreTests(unittest.TestCase):
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
        self.store = RemediationStore(self.db_path)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_migration_and_create_are_idempotent(self) -> None:
        session, created = self._create()
        duplicate, duplicate_created = self._create()

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate, session)
        reopened = RemediationStore(self.db_path)
        self.assertEqual(reopened.list_actionable_sessions(), [session])

    def test_request_key_conflict_is_rejected(self) -> None:
        self._create()
        with self.assertRaises(RemediationConflict):
            self.store.create_session(
                session_id="session_other",
                owner="alice",
                request_key="request-1",
                state=RemediationState.WAITING_EVIDENCE,
                source_run_id="run_derived",
                source_contract_id=None,
                source_diagnosis_digest="d" * 64,
                source_evidence_digest="e" * 64,
                automation_policy="manual_approval",
                budget=RemediationBudget(),
            )

    def test_transition_uses_version_and_state_cas(self) -> None:
        session, _ = self._create()
        diagnosing = self.store.transition(
            session.session_id,
            expected_version=1,
            expected_state=RemediationState.WAITING_EVIDENCE,
            target_state=RemediationState.DIAGNOSING,
            usage=RemediationUsage(attempts=1),
        )
        self.assertEqual(diagnosing.version, 2)
        self.assertEqual(diagnosing.usage.attempts, 1)

        with self.assertRaises(RemediationConflict):
            self.store.transition(
                session.session_id,
                expected_version=1,
                expected_state=RemediationState.WAITING_EVIDENCE,
                target_state=RemediationState.DIAGNOSING,
            )

    def test_session_list_uses_stable_owner_scoped_keyset_pagination(self) -> None:
        first, _ = self._create()
        second, _ = self.store.create_session(
            session_id="session_second",
            owner="alice",
            request_key="request-2",
            state=RemediationState.WAITING_EVIDENCE,
            source_run_id="run_source",
            source_contract_id="contract_source",
            source_diagnosis_digest="d" * 64,
            source_evidence_digest="e" * 64,
            automation_policy="manual_approval",
            budget=RemediationBudget(),
        )

        page_one, cursor = self.store.list_sessions_page(owner="alice", limit=1)
        page_two, final_cursor = self.store.list_sessions_page(
            owner="alice",
            before=cursor,
            limit=1,
        )

        self.assertIsNotNone(cursor)
        self.assertEqual(
            {page_one[0].session_id, page_two[0].session_id},
            {first.session_id, second.session_id},
        )
        self.assertIsNone(final_cursor)

    def test_terminal_transition_requires_stop_reason(self) -> None:
        session, _ = self._create()
        with self.assertRaises(ValueError):
            self.store.transition(
                session.session_id,
                expected_version=1,
                expected_state=RemediationState.WAITING_EVIDENCE,
                target_state=RemediationState.BLOCKED,
            )

    def test_lease_is_exclusive_and_owner_can_release(self) -> None:
        session, _ = self._create()
        leased, claimed = self.store.claim_lease(session.session_id, worker_id="worker-a")
        _, other_claimed = self.store.claim_lease(session.session_id, worker_id="worker-b")

        self.assertTrue(claimed)
        self.assertEqual(leased.lease_owner, "worker-a")
        self.assertFalse(other_claimed)
        self.assertFalse(self.store.release_lease(session.session_id, worker_id="worker-b"))
        self.assertTrue(self.store.release_lease(session.session_id, worker_id="worker-a"))

    def test_audit_children_are_persisted_and_ordered(self) -> None:
        session, _ = self._create()
        turn = self.store.append_turn(
            turn_id="turn_1",
            session_id=session.session_id,
            turn_index=0,
            state="planned",
            source_run_id="run_source",
            advice_id="advice_1",
            payload={"summary": "fix"},
        )
        proposal = self.store.append_proposal(
            proposal_id="proposal_1",
            session_id=session.session_id,
            turn_id=turn.turn_id,
            action_id="action_1",
            action_type="contract_patch",
            source="rule",
            risk="medium",
            approval_required=True,
            policy_status="allowed_preview",
            payload={"patch": {"resources.time_limit": "00:10:00"}},
        )
        now = datetime.now(UTC).isoformat()
        self.store.append_decision(
            ActionDecision(
                decision_id="decision_1",
                session_id=session.session_id,
                proposal_id=proposal.proposal_id,
                actor="alice",
                decision="approve",
                expected_session_version=1,
                note=None,
                created_at=now,
            )
        )
        execution = self.store.append_execution(
            ActionExecution(
                execution_id="execution_1",
                session_id=session.session_id,
                proposal_id=proposal.proposal_id,
                state="succeeded",
                derived_contract_id="contract_derived",
                derived_run_id="run_derived",
                error_code=None,
                error_message=None,
                created_at=now,
                updated_at=now,
            )
        )
        self.store.append_evaluation(
            EvaluationResult(
                evaluation_id="evaluation_1",
                session_id=session.session_id,
                execution_id=execution.execution_id,
                source_run_id="run_source",
                derived_run_id="run_derived",
                outcome=EvaluationOutcome.VERIFIED_SUCCESS,
                checks=({"name": "terminal_state", "status": "passed"},),
                evidence_refs=("evidence://runs/run_derived/slurm/accounting.json",),
                created_at=now,
            )
        )

        self.assertEqual(self.store.list_turns(session.session_id), [turn])
        self.assertEqual(self.store.list_proposals(session.session_id), [proposal])
        events, next_event_id = self.store.list_events_page(session.session_id)
        self.assertIsNone(next_event_id)
        self.assertEqual(
            [event.event_type for event in events],
            [
                "session.created",
                "turn.created",
                "proposal.created",
                "decision.approve",
                "execution.created",
                "evaluation.created",
            ],
        )

    def test_deterministic_children_are_idempotent(self) -> None:
        session, _ = self._create()
        turn = self.store.append_turn(
            turn_id="turn_replay",
            session_id=session.session_id,
            turn_index=0,
            state="ready",
            source_run_id="run_source",
            advice_id="advice_replay",
            payload={"summary": "retry"},
        )
        replayed_turn = self.store.append_turn(
            turn_id="turn_replay",
            session_id=session.session_id,
            turn_index=0,
            state="ready",
            source_run_id="run_source",
            advice_id="advice_replay",
            payload={"summary": "retry"},
        )
        proposal = self.store.append_proposal(
            proposal_id="proposal_replay",
            session_id=session.session_id,
            turn_id=turn.turn_id,
            action_id="action_replay",
            action_type="contract_patch_preview",
            source="diagnosis_rule",
            risk="low",
            approval_required=True,
            policy_status="allowed_preview",
            payload={"action_id": "action_replay"},
        )
        replayed_proposal = self.store.append_proposal(
            proposal_id="proposal_replay",
            session_id=session.session_id,
            turn_id=turn.turn_id,
            action_id="action_replay",
            action_type="contract_patch_preview",
            source="diagnosis_rule",
            risk="low",
            approval_required=True,
            policy_status="allowed_preview",
            payload={"action_id": "action_replay"},
        )

        self.assertEqual(replayed_turn, turn)
        self.assertEqual(replayed_proposal, proposal)
        self.assertEqual(len(self.store.list_turns(session.session_id)), 1)
        self.assertEqual(len(self.store.list_proposals(session.session_id)), 1)

    def _create(self):
        return self.store.create_session(
            session_id="session_1",
            owner="alice",
            request_key="request-1",
            state=RemediationState.WAITING_EVIDENCE,
            source_run_id="run_source",
            source_contract_id="contract_source",
            source_diagnosis_digest="d" * 64,
            source_evidence_digest="e" * 64,
            automation_policy="manual_approval",
            budget=RemediationBudget(),
        )


if __name__ == "__main__":
    unittest.main()
