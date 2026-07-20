"""Lease-safe remediation orchestration built on existing evidence-bound advice."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, Protocol

from pilot107.core.advice import AdviceResult
from pilot107.core.contracts import ContractStore
from pilot107.core.remediation import (
    TERMINAL_REMEDIATION_STATES,
    ActionDecision,
    ActionExecution,
    ActionProposal,
    EvaluationOutcome,
    EvaluationResult,
    RemediationBudget,
    RemediationConflict,
    RemediationSession,
    RemediationState,
    RemediationUsage,
)
from pilot107.core.remediation_store import RemediationStore
from pilot107.core.run_store import (
    AgentActionExecutionRecord,
    AgentAdviceRecord,
    RunRecord,
    RunStore,
)
from pilot107.core.states import (
    TERMINAL_RUN_STATES,
    CollectionState,
    DiagnosisState,
    RunState,
)
from pilot107.worker.evidence import EvidenceStore

_REQUEST_KEY = re.compile(r"[A-Za-z0-9._:-]{1,128}")


class RemediationServiceError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class AdviceService(Protocol):
    def advise(
        self,
        run_id: str,
        *,
        provider: str = "none",
        idempotency_key: str | None = None,
    ) -> AdviceResult: ...

    def get(self, advice_id: str) -> AgentAdviceRecord: ...

    def approve(
        self,
        advice_id: str,
        *,
        expected_version: int,
        action_ids: list[str],
        actor: str,
        note: str | None = None,
    ) -> AgentAdviceRecord: ...

    def reject(
        self,
        advice_id: str,
        *,
        expected_version: int,
        actor: str,
        note: str | None = None,
    ) -> AgentAdviceRecord: ...

    def execute_action(
        self,
        advice_id: str,
        *,
        action_id: str,
        actor: str,
        submit: bool = True,
    ) -> AgentActionExecutionRecord: ...


class RemediationService:
    def __init__(
        self,
        *,
        run_store: RunStore,
        remediation_store: RemediationStore,
        advice_service: AdviceService,
        contract_store: ContractStore | None = None,
        evidence_store: EvidenceStore | None = None,
    ) -> None:
        self.run_store = run_store
        self.remediation_store = remediation_store
        self.advice_service = advice_service
        self.contract_store = contract_store
        self.evidence_store = evidence_store

    def create(
        self,
        *,
        owner: str,
        source_run_id: str,
        request_key: str,
        automation_policy: str = "manual_approval",
        budget: RemediationBudget | None = None,
        provider: str = "none",
    ) -> tuple[RemediationSession, bool]:
        normalized_key = request_key.strip()
        if not _REQUEST_KEY.fullmatch(normalized_key):
            raise RemediationServiceError(
                "invalid remediation request key",
                code="REMEDIATION.INVALID_REQUEST_KEY",
            )
        if automation_policy not in {"manual_approval", "read_only_auto"}:
            raise RemediationServiceError(
                "unsupported automation policy",
                code="REMEDIATION.INVALID_AUTOMATION_POLICY",
            )
        try:
            run = self.run_store.get_run(source_run_id)
        except KeyError as exc:
            raise RemediationServiceError(
                "source run does not exist",
                code="REMEDIATION.RUN_NOT_FOUND",
            ) from exc
        if run.owner != owner:
            raise RemediationServiceError(
                "source run belongs to another owner",
                code="AUTH.FORBIDDEN",
            )
        diagnosis_digest, evidence_digest = self._source_digests(run)
        session_id = "remsession_" + hashlib.sha256(
            f"{owner}\0{normalized_key}".encode()
        ).hexdigest()[:32]
        return self.remediation_store.create_session(
            session_id=session_id,
            owner=owner,
            request_key=normalized_key,
            state=RemediationState.WAITING_EVIDENCE,
            source_run_id=run.run_id,
            source_contract_id=run.contract_id,
            source_diagnosis_digest=diagnosis_digest,
            source_evidence_digest=evidence_digest,
            automation_policy=automation_policy,
            budget=budget or RemediationBudget(),
            provider=provider,
        )

    def advance(
        self,
        session_id: str,
        *,
        worker_id: str,
        provider: str | None = None,
    ) -> RemediationSession:
        session, claimed = self.remediation_store.claim_lease(
            session_id,
            worker_id=worker_id,
        )
        if not claimed:
            return session
        # Resolve the effective provider: an explicit non-None provider from
        # the caller (e.g. the API's manual-advance) overrides the session's
        # persisted choice; ``None`` (the Worker's auto-advance) means "use
        # what the user picked at creation time". The resolved value is
        # persisted so later transitions / replanning cycles stay consistent.
        if provider is not None:
            session = self.remediation_store.update_provider(
                session_id,
                provider=provider,
            )
        effective_provider = session.provider
        try:
            for _ in range(8):
                session = self.remediation_store.get_session(session_id)
                if session.state == RemediationState.WAITING_EVIDENCE:
                    run = self.run_store.get_run(session.source_run_id)
                    if not _evidence_ready(run):
                        return session
                    session = self.remediation_store.transition(
                        session_id,
                        expected_version=session.version,
                        expected_state=session.state,
                        target_state=RemediationState.DIAGNOSING,
                    )
                    continue
                if session.state == RemediationState.DIAGNOSING:
                    session = self.remediation_store.transition(
                        session_id,
                        expected_version=session.version,
                        expected_state=session.state,
                        target_state=RemediationState.PLANNING,
                    )
                    continue
                if session.state == RemediationState.PLANNING:
                    return self._plan_turn(session, provider=effective_provider)
                if session.state == RemediationState.EXECUTING:
                    return self._evaluate_execution(session)
                if session.state == RemediationState.EVALUATING:
                    return self._finish_evaluation(session)
                return session
            raise RemediationServiceError(
                "remediation advance exceeded internal transition bound",
                code="REMEDIATION.TRANSITION_BOUND",
            )
        finally:
            self.remediation_store.release_lease(session_id, worker_id=worker_id)

    def approve(
        self,
        session_id: str,
        *,
        proposal_id: str,
        actor: str,
        expected_version: int,
        note: str | None = None,
    ) -> RemediationSession:
        session = self.remediation_store.get_session(session_id)
        if session.owner != actor:
            raise RemediationServiceError("session belongs to another owner", code="AUTH.FORBIDDEN")
        if session.state != RemediationState.AWAITING_APPROVAL:
            raise RemediationServiceError(
                "session is not awaiting approval",
                code="REMEDIATION.NOT_AWAITING_APPROVAL",
            )
        if session.version != expected_version:
            raise RemediationConflict("remediation session version changed")
        proposal = self.remediation_store.get_proposal(proposal_id)
        if proposal.session_id != session_id or proposal.policy_status != "allowed_preview":
            raise RemediationServiceError(
                "proposal is not executable in this session",
                code="REMEDIATION.POLICY_DENIED",
            )
        turn = self.remediation_store.get_turn(proposal.turn_id)
        if turn.advice_id is None:
            raise RemediationServiceError(
                "proposal has no advice binding",
                code="REMEDIATION.ADVICE_MISSING",
            )
        advice = self.advice_service.get(turn.advice_id)
        if advice.state != "approved":
            self.advice_service.approve(
                advice.advice_id,
                expected_version=advice.version,
                action_ids=[proposal.action_id],
                actor=actor,
                note=note,
            )
        decision = ActionDecision(
            decision_id="remdecision_"
            + hashlib.sha256(
                f"{session_id}\0{proposal_id}\0{expected_version}\0approve".encode()
            ).hexdigest()[:32],
            session_id=session_id,
            proposal_id=proposal_id,
            actor=actor,
            decision="approve",
            expected_session_version=expected_version,
            note=_bounded_note(note),
            created_at=datetime.now(UTC).isoformat(),
        )
        self.remediation_store.append_decision(decision)
        return self.remediation_store.transition(
            session_id,
            expected_version=expected_version,
            expected_state=RemediationState.AWAITING_APPROVAL,
            target_state=RemediationState.READY,
        )

    def reject(
        self,
        session_id: str,
        *,
        proposal_id: str,
        actor: str,
        expected_version: int,
        note: str | None = None,
    ) -> RemediationSession:
        session = self.remediation_store.get_session(session_id)
        if session.owner != actor:
            raise RemediationServiceError("session belongs to another owner", code="AUTH.FORBIDDEN")
        if session.state != RemediationState.AWAITING_APPROVAL:
            raise RemediationServiceError(
                "session is not awaiting approval",
                code="REMEDIATION.NOT_AWAITING_APPROVAL",
            )
        if session.version != expected_version:
            raise RemediationConflict("remediation session version changed")
        proposal = self.remediation_store.get_proposal(proposal_id)
        if proposal.session_id != session_id:
            raise RemediationServiceError(
                "proposal belongs to another session",
                code="AUTH.FORBIDDEN",
            )
        turn = self.remediation_store.get_turn(proposal.turn_id)
        if turn.advice_id is None:
            raise RemediationServiceError(
                "proposal has no advice binding",
                code="REMEDIATION.ADVICE_MISSING",
            )
        advice = self.advice_service.get(turn.advice_id)
        if advice.state != "rejected":
            self.advice_service.reject(
                advice.advice_id,
                expected_version=advice.version,
                actor=actor,
                note=note,
            )
        decision = ActionDecision(
            decision_id="remdecision_"
            + hashlib.sha256(
                f"{session_id}\0{proposal_id}\0{expected_version}\0reject".encode()
            ).hexdigest()[:32],
            session_id=session_id,
            proposal_id=proposal_id,
            actor=actor,
            decision="reject",
            expected_session_version=expected_version,
            note=_bounded_note(note),
            created_at=datetime.now(UTC).isoformat(),
        )
        self.remediation_store.append_decision(decision)
        return self.remediation_store.transition(
            session_id,
            expected_version=expected_version,
            expected_state=RemediationState.AWAITING_APPROVAL,
            target_state=RemediationState.BLOCKED,
            stop_reason="action_rejected",
        )

    def cancel(
        self,
        session_id: str,
        *,
        actor: str,
        expected_version: int,
        note: str | None = None,
    ) -> RemediationSession:
        session = self.remediation_store.get_session(session_id)
        if session.owner != actor:
            raise RemediationServiceError("session belongs to another owner", code="AUTH.FORBIDDEN")
        if session.state == RemediationState.CANCELLED:
            return session
        if session.state in TERMINAL_REMEDIATION_STATES:
            raise RemediationConflict("terminal remediation session cannot be cancelled")
        if session.version != expected_version:
            raise RemediationConflict("remediation session version changed")
        return self.remediation_store.transition(
            session_id,
            expected_version=expected_version,
            expected_state=session.state,
            target_state=RemediationState.CANCELLED,
            stop_reason="cancelled_by_owner",
            takeover_reason=_bounded_note(note),
        )

    def takeover(
        self,
        session_id: str,
        *,
        actor: str,
        expected_version: int,
        note: str,
    ) -> RemediationSession:
        session = self.remediation_store.get_session(session_id)
        if session.owner != actor:
            raise RemediationServiceError("session belongs to another owner", code="AUTH.FORBIDDEN")
        if session.state == RemediationState.BLOCKED and session.stop_reason == "manual_takeover":
            return session
        if session.state in TERMINAL_REMEDIATION_STATES:
            raise RemediationConflict("terminal remediation session cannot be taken over")
        if session.version != expected_version:
            raise RemediationConflict("remediation session version changed")
        reason = _bounded_note(note)
        if reason is None:
            raise RemediationServiceError(
                "takeover note is required",
                code="REMEDIATION.TAKEOVER_NOTE_REQUIRED",
            )
        return self.remediation_store.transition(
            session_id,
            expected_version=expected_version,
            expected_state=session.state,
            target_state=RemediationState.BLOCKED,
            stop_reason="manual_takeover",
            takeover_reason=reason,
        )

    def execute(
        self,
        session_id: str,
        *,
        proposal_id: str,
        actor: str,
        expected_version: int,
        submit: bool = True,
    ) -> tuple[RemediationSession, ActionExecution]:
        session = self.remediation_store.get_session(session_id)
        if session.owner != actor:
            raise RemediationServiceError("session belongs to another owner", code="AUTH.FORBIDDEN")
        if session.state not in {RemediationState.READY, RemediationState.PREPARING}:
            raise RemediationConflict("remediation session is not executable")
        if session.version != expected_version:
            raise RemediationConflict("remediation session version changed")
        if session.state == RemediationState.READY:
            exhausted = session.usage.exhausted_reason(session.budget)
            if exhausted is not None:
                terminal = self.remediation_store.transition(
                    session_id,
                    expected_version=expected_version,
                    expected_state=RemediationState.READY,
                    target_state=RemediationState.EXHAUSTED,
                    stop_reason=exhausted,
                )
                raise RemediationServiceError(
                    f"remediation budget is exhausted: {terminal.stop_reason}",
                    code="REMEDIATION.BUDGET_EXHAUSTED",
                )
        proposal = self.remediation_store.get_proposal(proposal_id)
        if proposal.session_id != session_id:
            raise RemediationServiceError(
                "proposal belongs to another session",
                code="AUTH.FORBIDDEN",
            )
        approved = any(
            item.proposal_id == proposal_id and item.decision == "approve"
            for item in self.remediation_store.list_decisions(session_id)
        )
        if not approved:
            raise RemediationServiceError(
                "proposal has not been approved",
                code="REMEDIATION.NOT_APPROVED",
            )
        turn = self.remediation_store.get_turn(proposal.turn_id)
        if turn.advice_id is None:
            raise RemediationServiceError(
                "proposal has no advice binding",
                code="REMEDIATION.ADVICE_MISSING",
            )
        preparing = session
        if session.state == RemediationState.READY:
            preparing = self.remediation_store.transition(
                session_id,
                expected_version=expected_version,
                expected_state=RemediationState.READY,
                target_state=RemediationState.PREPARING,
            )
        try:
            agent_execution = self.advice_service.execute_action(
                turn.advice_id,
                action_id=proposal.action_id,
                actor=actor,
                submit=submit,
            )
        except Exception as exc:
            self.remediation_store.transition(
                session_id,
                expected_version=preparing.version,
                expected_state=RemediationState.PREPARING,
                target_state=RemediationState.FAILED,
                stop_reason=f"action_execution_failed:{type(exc).__name__}",
            )
            raise
        now = datetime.now(UTC).isoformat()
        execution = ActionExecution(
            execution_id="remexec_"
            + hashlib.sha256(f"{session_id}\0{proposal_id}".encode()).hexdigest()[:32],
            session_id=session_id,
            proposal_id=proposal_id,
            state=agent_execution.state,
            derived_contract_id=agent_execution.derived_contract_id,
            derived_run_id=agent_execution.run_id,
            error_code=agent_execution.error_code,
            error_message=agent_execution.error_message,
            created_at=now,
            updated_at=now,
        )
        self.remediation_store.append_execution(execution)
        usage = RemediationUsage(
            attempts=session.usage.attempts + 1,
            submissions=session.usage.submissions + (1 if submit else 0),
            wall_time_seconds=session.usage.wall_time_seconds,
            llm_calls=session.usage.llm_calls + (1 if provider_uses_llm(turn.payload) else 0),
            llm_tokens=session.usage.llm_tokens,
        )
        target = RemediationState.EXECUTING if submit else RemediationState.READY
        updated = self.remediation_store.transition(
            session_id,
            expected_version=preparing.version,
            expected_state=RemediationState.PREPARING,
            target_state=target,
            usage=usage,
        )
        return updated, execution

    def detail(self, session_id: str, *, owner: str) -> dict[str, Any]:
        session = self.remediation_store.get_session(session_id)
        if session.owner != owner:
            raise RemediationServiceError("session belongs to another owner", code="AUTH.FORBIDDEN")
        return remediation_session_payload(
            session,
            turns=self.remediation_store.list_turns(session_id),
            proposals=self.remediation_store.list_proposals(session_id),
            decisions=self.remediation_store.list_decisions(session_id),
            executions=self.remediation_store.list_executions(session_id),
            evaluations=self.remediation_store.list_evaluations(session_id),
        )

    def _evaluate_execution(self, session: RemediationSession) -> RemediationSession:
        executions = self.remediation_store.list_executions(session.session_id)
        if not executions:
            raise RemediationServiceError(
                "executing remediation has no action execution",
                code="REMEDIATION.EXECUTION_MISSING",
            )
        execution = executions[-1]
        if execution.derived_run_id is None:
            return self.remediation_store.transition(
                session.session_id,
                expected_version=session.version,
                expected_state=RemediationState.EXECUTING,
                target_state=RemediationState.BLOCKED,
                stop_reason="derived_run_missing",
            )
        run = self.run_store.get_run(execution.derived_run_id)
        if not _evaluation_ready(run):
            return session
        evaluating = self.remediation_store.transition(
            session.session_id,
            expected_version=session.version,
            expected_state=RemediationState.EXECUTING,
            target_state=RemediationState.EVALUATING,
        )
        evaluation = _evaluate_run(
            session=evaluating,
            execution=execution,
            run=run,
            evidence=self.run_store.list_evidence_objects(run.run_id),
            contract_store=self.contract_store,
            evidence_store=self.evidence_store,
        )
        self.remediation_store.append_evaluation(evaluation)
        return self._finish_evaluation(evaluating)

    def _finish_evaluation(self, session: RemediationSession) -> RemediationSession:
        evaluations = self.remediation_store.list_evaluations(session.session_id)
        if not evaluations:
            executions = self.remediation_store.list_executions(session.session_id)
            if not executions or executions[-1].derived_run_id is None:
                raise RemediationServiceError(
                    "evaluation has no recoverable execution",
                    code="REMEDIATION.EVALUATION_INPUT_MISSING",
                )
            execution = executions[-1]
            derived_run_id = execution.derived_run_id
            if derived_run_id is None:
                raise AssertionError("derived run id was checked above")
            run = self.run_store.get_run(derived_run_id)
            if not _evaluation_ready(run):
                return session
            self.remediation_store.append_evaluation(
                _evaluate_run(
                    session=session,
                    execution=execution,
                    run=run,
                    evidence=self.run_store.list_evidence_objects(run.run_id),
                    contract_store=self.contract_store,
                    evidence_store=self.evidence_store,
                )
            )
            evaluations = self.remediation_store.list_evaluations(session.session_id)
        outcome = evaluations[-1].outcome
        usage = _usage_with_elapsed_time(session)
        stop_reason: str | None
        if outcome == EvaluationOutcome.VERIFIED_SUCCESS:
            target = RemediationState.SUCCEEDED
            stop_reason = "verified_success"
        elif outcome == EvaluationOutcome.EXECUTION_SUCCESS_UNVERIFIED:
            target = RemediationState.BLOCKED
            stop_reason = "execution_success_unverified"
        elif outcome == EvaluationOutcome.FAILED:
            exhausted = usage.exhausted_reason(session.budget)
            target = RemediationState.EXHAUSTED if exhausted else RemediationState.PLANNING
            stop_reason = exhausted
        else:
            target = RemediationState.BLOCKED
            stop_reason = "evaluation_inconclusive"
        return self.remediation_store.transition(
            session.session_id,
            expected_version=session.version,
            expected_state=RemediationState.EVALUATING,
            target_state=target,
            usage=usage,
            stop_reason=stop_reason,
        )

    def _plan_turn(self, session: RemediationSession, *, provider: str) -> RemediationSession:
        turn_index = len(self.remediation_store.list_evaluations(session.session_id))
        source_run_id = self._current_run_id(session)
        advice_result = self.advice_service.advise(
            source_run_id,
            provider=provider,
            idempotency_key=f"{session.session_id}:turn:{turn_index}",
        )
        advice = advice_result.record
        turn_id = "remturn_" + hashlib.sha256(
            f"{session.session_id}\0{turn_index}".encode()
        ).hexdigest()[:32]
        turn = self.remediation_store.append_turn(
            turn_id=turn_id,
            session_id=session.session_id,
            turn_index=turn_index,
            state=advice.state,
            source_run_id=source_run_id,
            advice_id=advice.advice_id,
            payload={
                "schema_version": advice.payload.get("schema_version"),
                "summary": advice.payload.get("summary"),
                "provider": advice.provider,
                "model": advice.model,
                "evidence_bundle_sha256": advice.evidence_bundle_sha256,
            },
        )
        actions = [item for item in advice.payload.get("actions", []) if isinstance(item, dict)]
        statuses: set[str] = set()
        for action in actions:
            action_id = str(action.get("action_id") or "")
            if not action_id:
                continue
            proposal_id = "remproposal_" + hashlib.sha256(
                f"{turn.turn_id}\0{action_id}".encode()
            ).hexdigest()[:32]
            proposal = self.remediation_store.append_proposal(
                proposal_id=proposal_id,
                session_id=session.session_id,
                turn_id=turn.turn_id,
                action_id=action_id,
                action_type=str(action.get("type") or "unknown"),
                source=str(action.get("source") or "unknown"),
                risk=str(action.get("risk") or "unknown"),
                approval_required=bool(action.get("approval_required", True)),
                policy_status=str(action.get("policy_status") or "blocked"),
                payload=action,
            )
            statuses.add(proposal.policy_status)
        if "allowed_preview" in statuses:
            target = RemediationState.AWAITING_APPROVAL
            stop_reason = None
        elif "requires_input" in statuses:
            target = RemediationState.AWAITING_INPUT
            stop_reason = None
        else:
            target = RemediationState.BLOCKED
            stop_reason = "no_safe_action"
        return self.remediation_store.transition(
            session.session_id,
            expected_version=session.version,
            expected_state=RemediationState.PLANNING,
            target_state=target,
            stop_reason=stop_reason,
        )

    def _current_run_id(self, session: RemediationSession) -> str:
        evaluations = self.remediation_store.list_evaluations(session.session_id)
        return evaluations[-1].derived_run_id if evaluations else session.source_run_id

    def _source_digests(self, run: RunRecord) -> tuple[str, str]:
        diagnoses = [
            {
                "rule_id": item.rule_id,
                "severity": item.severity,
                "summary": item.summary,
                "evidence_refs": item.evidence_refs,
                "suggested_patch": item.suggested_patch,
            }
            for item in self.run_store.list_diagnoses(run.run_id)
        ]
        evidence = [
            {
                "logical_path": item.logical_path,
                "sha256": item.sha256,
                "collection_status": item.collection_status,
                "finalized_at": item.finalized_at,
            }
            for item in self.run_store.list_evidence_objects(run.run_id)
        ]
        return _digest(diagnoses), _digest(evidence)


def remediation_session_payload(
    session: RemediationSession,
    *,
    turns: list[Any] | None = None,
    proposals: list[ActionProposal] | None = None,
    decisions: list[ActionDecision] | None = None,
    executions: list[ActionExecution] | None = None,
    evaluations: list[Any] | None = None,
) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "owner": session.owner,
        "state": session.state.value,
        "version": session.version,
        "source_run_id": session.source_run_id,
        "source_contract_id": session.source_contract_id,
        "source_diagnosis_digest": session.source_diagnosis_digest,
        "source_evidence_digest": session.source_evidence_digest,
        "automation_policy": session.automation_policy,
        "provider": session.provider,
        "budget": session.budget.to_payload(),
        "usage": session.usage.to_payload(),
        "stop_reason": session.stop_reason,
        "takeover_reason": session.takeover_reason,
        "lease": {
            "owner": session.lease_owner,
            "expires_at": session.lease_expires_at,
        },
        "turns": [_dataclass_payload(item) for item in (turns or [])],
        "proposals": [_dataclass_payload(item) for item in (proposals or [])],
        "decisions": [_dataclass_payload(item) for item in (decisions or [])],
        "executions": [_dataclass_payload(item) for item in (executions or [])],
        "evaluations": [_dataclass_payload(item) for item in (evaluations or [])],
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def _dataclass_payload(value: Any) -> dict[str, Any]:
    payload = dict(vars(value))
    for key, item in tuple(payload.items()):
        if hasattr(item, "value"):
            payload[key] = item.value
        elif isinstance(item, tuple):
            payload[key] = list(item)
    return payload


def _evidence_ready(run: RunRecord) -> bool:
    return run.collection_state in {CollectionState.SUCCEEDED, CollectionState.DEGRADED} and (
        run.diagnosis_state in {DiagnosisState.SUCCEEDED, DiagnosisState.SKIPPED}
    )


def _evaluation_ready(run: RunRecord) -> bool:
    return (
        run.state in TERMINAL_RUN_STATES
        and run.collection_state not in {CollectionState.PENDING, CollectionState.RUNNING}
        and run.diagnosis_state not in {DiagnosisState.PENDING, DiagnosisState.RUNNING}
    )


def _evaluate_run(
    *,
    session: RemediationSession,
    execution: ActionExecution,
    run: RunRecord,
    evidence: list[Any],
    contract_store: ContractStore | None = None,
    evidence_store: EvidenceStore | None = None,
) -> EvaluationResult:
    exit_zero = run.exit_code in {"0", "0:0"}
    execution_succeeded = run.state == RunState.SUCCEEDED and exit_zero
    evidence_complete = (
        run.collection_state == CollectionState.SUCCEEDED
        and run.diagnosis_state in {DiagnosisState.SUCCEEDED, DiagnosisState.SKIPPED}
    )
    expected_verification = _verify_expected_outputs(
        run=run,
        contract_store=contract_store,
        evidence_store=evidence_store,
    )
    expected_outputs_ok = expected_verification["ok"]
    has_expected_outputs = bool(expected_verification["expected_outputs"])
    # Strict run attribution: when the derived contract declares expected
    # outputs AND we can read the baseline+inventory, verified_success requires
    # the outputs to be newly produced (created/modified). If we cannot resolve
    # expected outputs (no stores, no contract, no inventory), preserve the
    # legacy condition so runs without declared outputs stay backward-compatible.
    enforce_expected = has_expected_outputs and expected_verification["resolved"]
    if execution_succeeded and evidence_complete:
        if enforce_expected and not expected_outputs_ok:
            outcome = EvaluationOutcome.EXECUTION_SUCCESS_UNVERIFIED
        else:
            outcome = EvaluationOutcome.VERIFIED_SUCCESS
    elif execution_succeeded:
        outcome = EvaluationOutcome.EXECUTION_SUCCESS_UNVERIFIED
    elif run.state in {RunState.FAILED, RunState.CANCELLED, RunState.SUBMIT_FAILED}:
        outcome = EvaluationOutcome.FAILED
    else:
        outcome = EvaluationOutcome.INCONCLUSIVE
    checks: tuple[dict[str, Any], ...] = (
        {
            "name": "terminal_execution",
            "status": "passed" if execution_succeeded else "failed",
            "state": run.state.value,
            "terminal_state": run.terminal_state,
            "exit_code": run.exit_code,
        },
        {
            "name": "evidence_collection",
            "status": "passed" if evidence_complete else "incomplete",
            "collection_state": run.collection_state.value,
            "diagnosis_state": run.diagnosis_state.value,
        },
    )
    if enforce_expected:
        checks = checks + (
            {
                "name": "expected_outputs_verified",
                "status": "passed" if expected_outputs_ok else "failed",
                "expected_outputs": expected_verification["expected_outputs"],
            },
        )
    evidence_refs = tuple(
        f"evidence://runs/{run.run_id}/{item.logical_path}"
        for item in evidence
        if item.collection_status == "collected" and item.finalized_at is not None
    )
    evaluation_id = "remeval_" + hashlib.sha256(
        f"{session.session_id}\0{execution.execution_id}".encode()
    ).hexdigest()[:32]
    comparison: dict[str, Any] = {
        "source_run_id": session.source_run_id,
        "derived_run_id": run.run_id,
        "source_diagnosis_digest": session.source_diagnosis_digest,
        "source_evidence_digest": session.source_evidence_digest,
    }
    if has_expected_outputs:
        comparison["expected_outputs"] = expected_verification["expected_outputs"]
        comparison["expected_outputs_ok"] = expected_outputs_ok
    return EvaluationResult(
        evaluation_id=evaluation_id,
        session_id=session.session_id,
        execution_id=execution.execution_id,
        source_run_id=session.source_run_id,
        derived_run_id=run.run_id,
        outcome=outcome,
        checks=checks,
        comparison=comparison,
        evidence_refs=evidence_refs,
        created_at=datetime.now(UTC).isoformat(),
    )


def _verify_expected_outputs(
    *,
    run: RunRecord,
    contract_store: ContractStore | None,
    evidence_store: EvidenceStore | None,
) -> dict[str, Any]:
    """Strictly verify declared expected outputs were newly produced.

    Returns a dict with:
      ``resolved`` (bool) — whether we could resolve the contract AND read the
        derived run's inventory (False => fall back to legacy verification);
      ``expected_outputs`` (list[dict]) — per-output {path, baseline_sha256,
        final_sha256, status};
      ``ok`` (bool) — True iff every declared output is ``created`` or
        ``modified`` (newly produced or changed by this run).

    Fail-closed semantics (round-6 audit P1-1): when the derived run HAS a
    ``contract_id`` but verification dependencies (stores) are unavailable or
    contract resolution fails, we return ``ok=False`` with ``resolved=True`` so
    ``enforce_expected=True`` → ``EXECUTION_SUCCESS_UNVERIFIED``. The ONLY
    ``ok=True`` paths are: (a) no ``contract_id`` (genuinely nothing to verify),
    and (b) contract read succeeds AND declares no expected outputs.
    """
    # No contract_id → genuinely nothing to verify. Legacy path.
    if run.contract_id is None:
        return {"resolved": False, "expected_outputs": [], "ok": True}
    # Contract_id present but verification dependencies unavailable → fail closed.
    # We cannot know whether the contract declares expected outputs, so we must
    # NOT silently upgrade to VERIFIED_SUCCESS. Return a placeholder entry so
    # has_expected_outputs=True → enforce_expected=True → ok=False → UNVERIFIED.
    if contract_store is None or evidence_store is None:
        return {
            "resolved": True,
            "expected_outputs": [
                {
                    "path": "<unknown>",
                    "baseline_sha256": None,
                    "final_sha256": None,
                    "status": "verification_dependencies_unavailable",
                }
            ],
            "ok": False,
        }
    try:
        contract = contract_store.get_contract(run.contract_id)
    except Exception:  # noqa: BLE001 - verification must never crash evaluation
        # Contract resolution failed → fail closed (same rationale as above).
        return {
            "resolved": True,
            "expected_outputs": [
                {
                    "path": "<unknown>",
                    "baseline_sha256": None,
                    "final_sha256": None,
                    "status": "contract_resolution_failed",
                }
            ],
            "ok": False,
        }
    outputs = contract.payload.get("outputs") or {}
    expected = outputs.get("expected") or []
    if not isinstance(expected, list) or not expected:
        # Contract read succeeded and declares no expected outputs → genuinely
        # nothing to verify. ok=True, resolved=True (we DID resolve the contract).
        return {"resolved": True, "expected_outputs": [], "ok": True}
    expected_paths = [str(item) for item in expected]
    inventory_path = evidence_store.run_root(run.run_id) / "outputs" / "inventory.json"
    try:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        # Fail CLOSED: stores are present and expected outputs are declared, but
        # the inventory is missing/unparseable. The audit's requirement #3
        # ("missing expected output → degraded/not verified") is violated if we
        # fall back to legacy VERIFIED_SUCCESS. Mark every expected output as
        # inventory_unreadable with ok=False so enforce_expected=True →
        # EXECUTION_SUCCESS_UNVERIFIED.
        return {
            "resolved": True,
            "expected_outputs": [
                {
                    "path": p,
                    "baseline_sha256": None,
                    "final_sha256": None,
                    "status": "inventory_unreadable",
                }
                for p in expected_paths
            ],
            "ok": False,
        }
    files = inventory.get("files") if isinstance(inventory, dict) else None
    if not isinstance(files, list):
        # Same fail-closed semantics for a structurally-invalid inventory.
        return {
            "resolved": True,
            "expected_outputs": [
                {
                    "path": p,
                    "baseline_sha256": None,
                    "final_sha256": None,
                    "status": "inventory_unreadable",
                }
                for p in expected_paths
            ],
            "ok": False,
        }
    by_path: dict[str, dict[str, Any]] = {
        str(item.get("relative_path")): item
        for item in files
        if isinstance(item, dict) and item.get("relative_path") is not None
    }
    entries: list[dict[str, Any]] = []
    ok = True
    for path in expected_paths:
        item = by_path.get(path)
        status = "missing" if item is None else str(item.get("attribution") or "unknown")
        entries.append(
            {
                "path": path,
                "baseline_sha256": (
                    None if item is None else item.get("baseline_sha256")
                ),
                "final_sha256": None if item is None else item.get("final_sha256"),
                "status": status,
            }
        )
        if status not in {"created", "modified"}:
            ok = False
    return {"resolved": True, "expected_outputs": entries, "ok": ok}


def _usage_with_elapsed_time(session: RemediationSession) -> RemediationUsage:
    try:
        created_at = datetime.fromisoformat(session.created_at)
        elapsed = max(0, int((datetime.now(UTC) - created_at).total_seconds()))
    except ValueError:
        elapsed = session.usage.wall_time_seconds
    return RemediationUsage(
        attempts=session.usage.attempts,
        submissions=session.usage.submissions,
        wall_time_seconds=max(session.usage.wall_time_seconds, elapsed),
        llm_calls=session.usage.llm_calls,
        llm_tokens=session.usage.llm_tokens,
    )


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _bounded_note(note: str | None) -> str | None:
    if note is None:
        return None
    normalized = note.strip()
    if len(normalized) > 1000:
        raise RemediationServiceError("note exceeds 1000 characters", code="INVALID_NOTE")
    return normalized or None


def provider_uses_llm(payload: dict[str, Any]) -> bool:
    return payload.get("provider") not in {None, "", "none", "rule"}
