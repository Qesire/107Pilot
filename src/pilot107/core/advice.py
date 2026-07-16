"""Deterministic, evidence-bound agent advice and approval policy."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from pilot107.core.agent import AgentExplainService, AgentExplanation
from pilot107.core.contracts import ContractError, ContractService
from pilot107.core.run_service import RunService
from pilot107.core.run_store import (
    AgentActionExecutionRecord,
    AgentAdviceConflict,
    AgentAdviceRecord,
    AgentDecisionRecord,
    DiagnosisRecord,
    RunRecord,
    RunStore,
)

_SCHEMA_VERSION = "AgentAdviceV1"
_REQUEST_KEY = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_PLACEHOLDER = re.compile(r"<[^<>]+>")
_PATCHABLE_FIELDS = frozenset(
    {
        "project.workdir",
        "entry.command",
        "resources.partition",
        "resources.qos",
        "resources.nodes",
        "resources.ntasks",
        "resources.cpus_per_task",
        "resources.time_limit",
        "resources.memory",
        "resources.gpus_per_node",
        "resources.array",
    }
)
_HIGH_RISK_FIELDS = frozenset({"project.workdir", "entry.command"})


class AgentAdviceError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AdviceResult:
    record: AgentAdviceRecord
    created: bool


class AgentPolicyEngine:
    """Evaluate rule-authored contract patches without consulting an LLM."""

    def __init__(self, *, contract_service: ContractService | None) -> None:
        self.contract_service = contract_service

    def actions_for(
        self,
        *,
        run: RunRecord,
        diagnoses: tuple[DiagnosisRecord, ...],
    ) -> tuple[dict[str, Any], ...]:
        return tuple(self._action_for(run=run, diagnosis=item) for item in diagnoses)

    def _action_for(
        self,
        *,
        run: RunRecord,
        diagnosis: DiagnosisRecord,
    ) -> dict[str, Any]:
        patch = diagnosis.suggested_patch
        base = {
            "action_id": f"action_{diagnosis.diagnosis_id}",
            "type": "contract_patch_preview" if patch else "manual_remediation",
            "source": "diagnosis_rule",
            "rule_id": diagnosis.rule_id,
            "approval_required": True,
            "risk": _risk_for_patch(patch),
            "proposed_patch": patch,
        }
        if not patch:
            return {**base, "policy_status": "manual_only", "reasons": ["no_rule_patch"]}
        unsupported = sorted(set(patch) - _PATCHABLE_FIELDS)
        if unsupported:
            return {
                **base,
                "policy_status": "blocked",
                "reasons": [f"field_not_patchable:{field}" for field in unsupported],
            }
        unresolved = sorted(
            field
            for field, value in patch.items()
            if value is None or (isinstance(value, str) and _PLACEHOLDER.search(value))
        )
        if unresolved:
            return {
                **base,
                "policy_status": "requires_input",
                "reasons": [f"value_required:{field}" for field in unresolved],
            }
        if run.contract_id is None or self.contract_service is None:
            return {**base, "policy_status": "blocked", "reasons": ["source_contract_missing"]}
        try:
            contract = self.contract_service.get(run.contract_id)
            if contract.owner != run.owner:
                raise AgentAdviceError(
                    "run and contract owners do not match",
                    code="AGENT.CONTRACT_OWNER_MISMATCH",
                )
            candidate = copy.deepcopy(contract.payload)
            for field, value in patch.items():
                _set_dotted(candidate, field, value)
            validation = self.contract_service.validate(candidate)
        except (KeyError, TypeError, ValueError, ContractError) as exc:
            return {
                **base,
                "policy_status": "blocked",
                "reasons": [f"contract_validation_error:{type(exc).__name__}"],
            }
        blockers = [
            finding.code
            for finding in validation.findings
            if finding.severity.value == "BLOCK"
        ]
        if blockers:
            return {
                **base,
                "policy_status": "blocked",
                "reasons": [f"preflight_blocked:{code}" for code in blockers],
            }
        return {
            **base,
            "policy_status": "allowed_preview",
            "reasons": [],
            "candidate": validation.effective_request,
            "risk_lint": validation.risk_lint,
        }


class AgentAdviceService:
    def __init__(
        self,
        *,
        store: RunStore,
        explain_service: AgentExplainService,
        policy_engine: AgentPolicyEngine,
        contract_service: ContractService | None = None,
        run_service: RunService | None = None,
    ) -> None:
        self.store = store
        self.explain_service = explain_service
        self.policy_engine = policy_engine
        self.contract_service = contract_service or policy_engine.contract_service
        self.run_service = run_service

    def advise(
        self,
        run_id: str,
        *,
        provider: str = "none",
        idempotency_key: str | None = None,
    ) -> AdviceResult:
        run = self.store.get_run(run_id)
        request_key = self._request_key(run, provider, idempotency_key)
        explanation = self.explain_service.explain(run_id, provider=provider)
        actions = self.policy_engine.actions_for(
            run=run,
            diagnoses=explanation.diagnoses,
        )
        state = _initial_state(explanation, actions)
        bundle_hash = explanation.evidence_bundle_sha256 or ""
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "summary": explanation.summary,
            "narrative": explanation.narrative,
            "facts": [fact.to_payload() for fact in explanation.facts],
            "citations": [citation.to_payload() for citation in explanation.citations],
            "recommendations": list(explanation.recommendations),
            "warnings": list(explanation.warnings),
            "actions": list(actions),
        }
        advice_id = "advice_" + hashlib.sha256(
            f"{run_id}\0{request_key}".encode()
        ).hexdigest()[:32]
        record, created = self.store.create_agent_advice(
            advice_id=advice_id,
            run_id=run_id,
            owner=run.owner,
            request_key=request_key,
            state=state,
            source_run_updated_at=run.updated_at,
            evidence_bundle_sha256=bundle_hash,
            provider=explanation.provider,
            model=explanation.model,
            payload=payload,
        )
        return AdviceResult(record=record, created=created)

    def get(self, advice_id: str) -> AgentAdviceRecord:
        return self.store.get_agent_advice(advice_id)

    def decisions(self, advice_id: str) -> list[AgentDecisionRecord]:
        return self.store.list_agent_decisions(advice_id)

    def executions(self, advice_id: str) -> list[AgentActionExecutionRecord]:
        return self.store.list_agent_action_executions(advice_id)

    def approve(
        self,
        advice_id: str,
        *,
        expected_version: int,
        action_ids: list[str],
        actor: str,
        note: str | None = None,
    ) -> AgentAdviceRecord:
        record = self.store.get_agent_advice(advice_id)
        if record.state != "ready":
            raise AgentAdviceError("advice is not approvable", code="AGENT.NOT_APPROVABLE")
        if not action_ids or len(action_ids) != len(set(action_ids)):
            raise AgentAdviceError(
                "action_ids must be a non-empty unique list",
                code="AGENT.INVALID_ACTION_SELECTION",
            )
        allowed = {
            str(action["action_id"])
            for action in record.payload.get("actions", [])
            if action.get("policy_status") == "allowed_preview"
        }
        if not set(action_ids).issubset(allowed):
            raise AgentAdviceError(
                "selected action is not allowed by policy",
                code="AGENT.POLICY_DENIED",
            )
        current_run = self.store.get_run(record.run_id)
        current = self.explain_service.explain(record.run_id, provider="none")
        if (
            current_run.updated_at != record.source_run_updated_at
            or (current.evidence_bundle_sha256 or "") != record.evidence_bundle_sha256
        ):
            self._invalidate_stale(record, actor=actor)
            raise AgentAdviceError(
                "run or evidence changed after advice generation",
                code="AGENT.ADVICE_STALE",
            )
        try:
            return self.store.decide_agent_advice(
                advice_id=advice_id,
                expected_version=expected_version,
                expected_state="ready",
                new_state="approved",
                decision="approve",
                actor=actor,
                action_ids=action_ids,
                note=_bounded_note(note),
            )
        except AgentAdviceConflict as exc:
            raise AgentAdviceError(
                "advice version or state changed",
                code="AGENT.ADVICE_CONFLICT",
            ) from exc

    def reject(
        self,
        advice_id: str,
        *,
        expected_version: int,
        actor: str,
        note: str | None = None,
    ) -> AgentAdviceRecord:
        record = self.store.get_agent_advice(advice_id)
        if record.state not in {"ready", "needs_input", "no_safe_action"}:
            raise AgentAdviceError("advice is already decided", code="AGENT.ADVICE_CONFLICT")
        try:
            return self.store.decide_agent_advice(
                advice_id=advice_id,
                expected_version=expected_version,
                expected_state=record.state,
                new_state="rejected",
                decision="reject",
                actor=actor,
                action_ids=[],
                note=_bounded_note(note),
            )
        except AgentAdviceConflict as exc:
            raise AgentAdviceError(
                "advice version or state changed",
                code="AGENT.ADVICE_CONFLICT",
            ) from exc

    def execute_action(
        self,
        advice_id: str,
        *,
        action_id: str,
        actor: str,
        submit: bool = True,
    ) -> AgentActionExecutionRecord:
        if self.contract_service is None or self.run_service is None:
            raise AgentAdviceError(
                "agent action execution service is unavailable",
                code="AGENT.EXECUTION_UNAVAILABLE",
            )
        advice = self.store.get_agent_advice(advice_id)
        if advice.owner != actor:
            raise AgentAdviceError("action actor is not the owner", code="AUTH.FORBIDDEN")
        if advice.state != "approved":
            raise AgentAdviceError(
                "advice must be approved before execution",
                code="AGENT.NOT_APPROVED",
            )
        selected = {
            action
            for decision in self.store.list_agent_decisions(advice_id)
            if decision.decision == "approve"
            for action in decision.action_ids
        }
        if action_id not in selected:
            raise AgentAdviceError(
                "action was not selected by the approval decision",
                code="AGENT.ACTION_NOT_APPROVED",
            )
        action = next(
            (
                item
                for item in advice.payload.get("actions", [])
                if isinstance(item, dict) and item.get("action_id") == action_id
            ),
            None,
        )
        if action is None or action.get("policy_status") != "allowed_preview":
            raise AgentAdviceError("action is not executable", code="AGENT.POLICY_DENIED")
        execution_id = "agentexec_" + hashlib.sha256(
            f"{advice_id}\0{action_id}".encode()
        ).hexdigest()[:32]
        try:
            existing_execution = self.store.get_agent_action_execution(execution_id)
        except KeyError:
            existing_execution = None
        if existing_execution is not None and existing_execution.state in {"submitted", "failed"}:
            return existing_execution
        current_run = self.store.get_run(advice.run_id)
        current = self.explain_service.explain(advice.run_id, provider="none")
        if (
            current_run.updated_at != advice.source_run_updated_at
            or (current.evidence_bundle_sha256 or "") != advice.evidence_bundle_sha256
        ):
            raise AgentAdviceError(
                "run or evidence changed after action approval",
                code="AGENT.APPROVED_ACTION_STALE",
            )

        execution, owns_execution = self.store.claim_agent_action_execution(
            execution_id=execution_id,
            advice_id=advice_id,
            action_id=action_id,
            owner=advice.owner,
            submit_requested=submit,
        )
        if execution.state in {"submitted", "failed"}:
            return execution
        if not owns_execution:
            if execution.state != "prepared" or not submit:
                return execution
            execution, owns_execution = self.store.begin_agent_action_execution(
                execution.execution_id,
                expected_state="prepared",
                submit_requested=True,
            )
            if not owns_execution:
                return execution
        try:
            source_run = self.store.get_run(advice.run_id)
            if source_run.contract_id is None:
                raise AgentAdviceError(
                    "source run has no contract",
                    code="AGENT.SOURCE_CONTRACT_MISSING",
                )
            source_contract = self.contract_service.get(source_run.contract_id)
            candidate = action.get("candidate")
            if not isinstance(candidate, dict) or not isinstance(candidate.get("contract"), dict):
                raise AgentAdviceError(
                    "approved action has no canonical contract candidate",
                    code="AGENT.CANDIDATE_MISSING",
                )
            patch = action.get("proposed_patch")
            if not isinstance(patch, dict):
                raise AgentAdviceError(
                    "approved action patch is invalid",
                    code="AGENT.CANDIDATE_MISSING",
                )
            derived_contract_id = "contract_agent_" + hashlib.sha256(
                execution_id.encode()
            ).hexdigest()[:32]
            derived = self.contract_service.create_derived(
                source=source_contract,
                payload=candidate["contract"],
                contract_id=derived_contract_id,
                advice_id=advice_id,
                action_id=action_id,
                patched_fields=list(patch),
            )
            request = self.contract_service.to_submit_request(
                derived,
                parent_run_id=source_run.run_id,
                lineage_reason="agent_remediation",
                remediation_plan_id=f"{advice_id}:{action_id}",
            )
            derived_run_id = "run_agent_" + hashlib.sha256(
                execution_id.encode()
            ).hexdigest()[:32]
            derived_run = self.run_service.prepare(
                request,
                run_id=derived_run_id,
                idempotent=True,
            )
            execution = self.store.update_agent_action_execution(
                execution_id,
                state="prepared",
                submit_requested=submit,
                derived_contract_id=derived.contract_id,
                run_id=derived_run.run_id,
            )
            if submit:
                submitted = self.run_service.submit_prepared(derived_run.run_id)
                execution = self.store.update_agent_action_execution(
                    execution_id,
                    state="submitted",
                    submit_requested=True,
                    derived_contract_id=derived.contract_id,
                    run_id=submitted.run_id,
                )
            self.store.append_event(
                run_id=source_run.run_id,
                event_type="agent.action_executed",
                payload={
                    "execution_id": execution.execution_id,
                    "action_id": action_id,
                    "derived_contract_id": execution.derived_contract_id,
                    "derived_run_id": execution.run_id,
                    "state": execution.state,
                },
            )
            return execution
        except AgentAdviceError as exc:
            self.store.update_agent_action_execution(
                execution_id,
                state="failed",
                error_code=exc.code,
                error_message=str(exc)[:1000],
            )
            raise
        except Exception as exc:
            self.store.update_agent_action_execution(
                execution_id,
                state="failed",
                error_code="AGENT.EXECUTION_FAILED",
                error_message=str(exc)[:1000],
            )
            raise AgentAdviceError(
                "approved action execution failed",
                code="AGENT.EXECUTION_FAILED",
            ) from exc

    def _invalidate_stale(self, record: AgentAdviceRecord, *, actor: str) -> None:
        with suppress(AgentAdviceConflict):
            self.store.decide_agent_advice(
                advice_id=record.advice_id,
                expected_version=record.version,
                expected_state=record.state,
                new_state="stale",
                decision="invalidate",
                actor=actor,
                action_ids=[],
                note="run_or_evidence_changed",
            )

    @staticmethod
    def _request_key(run: RunRecord, provider: str, key: str | None) -> str:
        if key is not None:
            normalized = key.strip()
            if not _REQUEST_KEY.fullmatch(normalized):
                raise AgentAdviceError(
                    "invalid idempotency_key",
                    code="AGENT.INVALID_IDEMPOTENCY_KEY",
                )
            return normalized
        material = json.dumps(
            {"run_id": run.run_id, "updated_at": run.updated_at, "provider": provider},
            sort_keys=True,
        )
        return "auto:" + hashlib.sha256(material.encode()).hexdigest()


def advice_payload(
    record: AgentAdviceRecord,
    decisions: list[AgentDecisionRecord] | None = None,
    executions: list[AgentActionExecutionRecord] | None = None,
) -> dict[str, Any]:
    return {
        "advice_id": record.advice_id,
        "run_id": record.run_id,
        "owner": record.owner,
        "state": record.state,
        "version": record.version,
        "source_run_updated_at": record.source_run_updated_at,
        "evidence_bundle_sha256": record.evidence_bundle_sha256,
        "provider": record.provider,
        "model": record.model,
        **record.payload,
        "decisions": [
            {
                "decision_id": item.decision_id,
                "decision": item.decision,
                "actor": item.actor,
                "action_ids": item.action_ids,
                "note": item.note,
                "advice_version": item.advice_version,
                "created_at": item.created_at,
            }
            for item in (decisions or [])
        ],
        "executions": [execution_payload(item) for item in (executions or [])],
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def execution_payload(record: AgentActionExecutionRecord) -> dict[str, Any]:
    return {
        "execution_id": record.execution_id,
        "advice_id": record.advice_id,
        "action_id": record.action_id,
        "owner": record.owner,
        "state": record.state,
        "submit_requested": record.submit_requested,
        "derived_contract_id": record.derived_contract_id,
        "run_id": record.run_id,
        "error_code": record.error_code,
        "error_message": record.error_message,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _initial_state(
    explanation: AgentExplanation,
    actions: tuple[dict[str, Any], ...],
) -> str:
    if explanation.status != "explained" or not explanation.evidence_bundle_sha256:
        return "no_safe_action"
    statuses = {str(action["policy_status"]) for action in actions}
    if "allowed_preview" in statuses:
        return "ready"
    if "requires_input" in statuses:
        return "needs_input"
    return "no_safe_action"


def _set_dotted(payload: dict[str, Any], field: str, value: Any) -> None:
    parts = field.split(".")
    cursor: dict[str, Any] = payload
    for part in parts[:-1]:
        child = cursor.get(part)
        if not isinstance(child, dict):
            raise TypeError(f"contract field is not an object: {part}")
        cursor = child
    cursor[parts[-1]] = value


def _risk_for_patch(patch: dict[str, Any]) -> str:
    if set(patch) & _HIGH_RISK_FIELDS:
        return "high"
    return "medium" if patch else "manual"


def _bounded_note(note: str | None) -> str | None:
    if note is None:
        return None
    normalized = note.strip()
    if len(normalized) > 1000:
        raise AgentAdviceError("note exceeds 1000 characters", code="AGENT.INVALID_NOTE")
    return normalized or None
