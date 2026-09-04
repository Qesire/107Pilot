"""Pure read projection for one Run's recovery graph.

The service does not create or advance remediation, Agent, ticket, Project, or
Run state. It reads existing control-plane facts and returns only stable,
owner-scoped fields needed by the product. Raw Agent payloads, code context,
shell output, and free-form execution error bodies are intentionally excluded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pilot107.core.remediation import RemediationState
from pilot107.core.run_store import DiagnosisRecord, RunRecord, RunStore
from pilot107.core.states import RunState

_SESSION_LIMIT = 50
_ADVICE_LIMIT = 100
_TICKET_LIMIT = 50

_REPAIR_ACTIVE_RUN_STATES = frozenset(
    {
        RunState.DRAFT,
        RunState.VALIDATED,
        RunState.SUBMITTING,
        RunState.SUBMITTED,
        RunState.PENDING,
        RunState.SUBMISSION_UNCERTAIN,
        RunState.RUNNING,
        RunState.COMPLETING,
        RunState.UNKNOWN,
    }
)
_FAILED_SOURCE_STATES = frozenset(
    {
        RunState.FAILED,
        RunState.SUBMIT_FAILED,
        RunState.AUTH_REQUIRED,
        RunState.ORPHANED,
    }
)


@dataclass(frozen=True)
class RepairWorkspaceService:
    """Aggregate existing repair authorities without becoming a new authority."""

    store: RunStore

    def get(self, run_id: str, *, owner: str | None = None) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if owner is not None and run.owner != owner:
            raise PermissionError("run is owned by another user")

        diagnoses = self.store.list_diagnoses(run.run_id)
        sessions, sessions_truncated = self._remediation_sessions(run)
        agent = self._agent_trace(run)
        tickets, tickets_truncated = self._repair_tickets(run)
        derived_runs = self.store.list_child_runs(run.run_id)

        return {
            "schema_version": "pilot107.repair-workspace/v1",
            "source_run": _source_run(run),
            "diagnoses": [_diagnosis(item) for item in diagnoses],
            "remediation_sessions": sessions,
            "agent": agent,
            "repair_tickets": tickets,
            "derived_runs": [_derived_run(item) for item in derived_runs],
            "truncation": {
                "remediation_sessions": sessions_truncated,
                "agent_advice": bool(agent["truncated"]),
                "repair_tickets": tickets_truncated,
            },
            "status": _status(sessions, agent, tickets, derived_runs),
            "next_action": _next_action(
                run,
                diagnoses=diagnoses,
                sessions=sessions,
                agent=agent,
                tickets=tickets,
                derived_runs=derived_runs,
            ),
        }

    def _remediation_sessions(self, run: RunRecord) -> tuple[list[dict[str, Any]], bool]:
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT session_id, state, version, automation_policy, provider,
                       budget_json, usage_json, created_at, updated_at
                FROM remediation_sessions
                WHERE owner = ? AND source_run_id = ?
                ORDER BY updated_at DESC, session_id DESC
                LIMIT ?
                """,
                (run.owner, run.run_id, _SESSION_LIMIT + 1),
            ).fetchall()
        selected = rows[:_SESSION_LIMIT]
        return (
            [
                {
                    "session_id": str(row["session_id"]),
                    "state": str(row["state"]),
                    "version": int(row["version"]),
                    "automation_policy": str(row["automation_policy"]),
                    "provider": str(row["provider"]),
                    "budget": _json_object(row["budget_json"]),
                    "usage": _json_object(row["usage_json"]),
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                }
                for row in selected
            ],
            len(rows) > _SESSION_LIMIT,
        )

    def _agent_trace(self, run: RunRecord) -> dict[str, Any]:
        advice, next_cursor = self.store.list_agent_advice_page(
            owner=run.owner,
            run_id=run.run_id,
            limit=_ADVICE_LIMIT,
        )
        advice_payload: list[dict[str, Any]] = []
        decisions_payload: list[dict[str, Any]] = []
        executions_payload: list[dict[str, Any]] = []
        for item in advice:
            advice_payload.append(
                {
                    "advice_id": item.advice_id,
                    "state": item.state,
                    "version": item.version,
                    "provider": item.provider,
                    "model": item.model,
                    "evidence_bundle_sha256": item.evidence_bundle_sha256,
                    "source_run_updated_at": item.source_run_updated_at,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                }
            )
            decisions_payload.extend(
                {
                    "decision_id": decision.decision_id,
                    "advice_id": decision.advice_id,
                    "decision": decision.decision,
                    "actor": decision.actor,
                    "action_ids": list(decision.action_ids),
                    "advice_version": decision.advice_version,
                    "created_at": decision.created_at,
                }
                for decision in self.store.list_agent_decisions(item.advice_id)
            )
            executions_payload.extend(
                {
                    "execution_id": execution.execution_id,
                    "advice_id": execution.advice_id,
                    "action_id": execution.action_id,
                    "state": execution.state,
                    "submit_requested": execution.submit_requested,
                    "derived_contract_id": execution.derived_contract_id,
                    "derived_run_id": execution.run_id,
                    "error_code": execution.error_code,
                    "execution_phase": execution.execution_phase,
                    "created_at": execution.created_at,
                    "updated_at": execution.updated_at,
                }
                for execution in self.store.list_agent_action_executions(item.advice_id)
                if execution.owner == run.owner
            )
        return {
            "advice": advice_payload,
            "decisions": decisions_payload,
            "executions": executions_payload,
            "truncated": next_cursor is not None,
        }

    def _repair_tickets(self, run: RunRecord) -> tuple[list[dict[str, Any]], bool]:
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT ticket_id, state, session_id, source_contract_id,
                       diagnosis_ids_json, requested_change,
                       resolution_manifest_id, resolution_run_id,
                       abandon_reason, created_at, updated_at
                FROM repair_tickets
                WHERE owner = ? AND source_run_id = ?
                ORDER BY updated_at DESC, ticket_id DESC
                LIMIT ?
                """,
                (run.owner, run.run_id, _TICKET_LIMIT + 1),
            ).fetchall()
        selected = rows[:_TICKET_LIMIT]
        return (
            [
                {
                    "ticket_id": str(row["ticket_id"]),
                    "state": str(row["state"]),
                    "session_id": _optional_text(row["session_id"]),
                    "source_contract_id": _optional_text(row["source_contract_id"]),
                    "diagnosis_ids": _json_string_list(row["diagnosis_ids_json"]),
                    "requested_change": _optional_text(row["requested_change"]),
                    "resolution_manifest_id": _optional_text(row["resolution_manifest_id"]),
                    "resolution_run_id": _optional_text(row["resolution_run_id"]),
                    "abandon_reason": _optional_text(row["abandon_reason"]),
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                }
                for row in selected
            ],
            len(rows) > _TICKET_LIMIT,
        )


def _source_run(run: RunRecord) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "owner": run.owner,
        "state": run.state.value,
        "collection_state": run.collection_state.value,
        "diagnosis_state": run.diagnosis_state.value,
        "result_status": run.result_status.value,
        "contract_id": run.contract_id,
        "updated_at": run.updated_at,
    }


def _diagnosis(item: DiagnosisRecord) -> dict[str, Any]:
    return {
        "diagnosis_id": item.diagnosis_id,
        "rule_id": item.rule_id,
        "severity": item.severity,
        "summary": item.summary,
        "evidence_refs": list(item.evidence_refs),
        "retryable": item.retryable,
        "confidence": item.confidence,
        "category": item.category,
        "stage": item.stage,
    }


def _derived_run(run: RunRecord) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "state": run.state.value,
        "collection_state": run.collection_state.value,
        "result_status": run.result_status.value,
        "lineage_reason": run.lineage_reason,
        "remediation_plan_id": run.remediation_plan_id,
        "attempt": run.attempt,
        "job_id": run.job_id,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def _status(
    sessions: list[dict[str, Any]],
    agent: dict[str, Any],
    tickets: list[dict[str, Any]],
    derived_runs: list[RunRecord],
) -> dict[str, bool]:
    return {
        "has_repair_activity": bool(
            sessions or agent["advice"] or agent["executions"] or tickets or derived_runs
        ),
        "awaiting_approval": any(
            item["state"] == RemediationState.AWAITING_APPROVAL.value for item in sessions
        ),
        "has_derived_run": bool(derived_runs),
        "has_successful_derived_run": any(
            item.state == RunState.SUCCEEDED for item in derived_runs
        ),
    }


def _next_action(
    run: RunRecord,
    *,
    diagnoses: list[DiagnosisRecord],
    sessions: list[dict[str, Any]],
    agent: dict[str, Any],
    tickets: list[dict[str, Any]],
    derived_runs: list[RunRecord],
) -> dict[str, str]:
    if any(
        item["state"] == RemediationState.AWAITING_APPROVAL.value for item in sessions
    ):
        return {
            "kind": "review_proposal",
            "label": "审核修复方案",
            "detail": "已有修复方案等待批准；核对 Evidence、修改范围与验证结果后再执行。",
        }
    active_derived = next(
        (item for item in reversed(derived_runs) if item.state in _REPAIR_ACTIVE_RUN_STATES),
        None,
    )
    if active_derived is not None:
        return {
            "kind": "watch_derived_run",
            "label": "查看修复后的运行",
            "detail": f"派生运行 {active_derived.run_id} 尚未结束。",
        }
    if any(item.state == RunState.SUCCEEDED for item in derived_runs):
        return {
            "kind": "compare_outcome",
            "label": "比较修复前后结果",
            "detail": "已有计算成功的派生运行；仍需检查 Evidence 与科学结果。",
        }
    if sessions or agent["advice"] or agent["executions"] or tickets:
        return {
            "kind": "continue_repair",
            "label": "继续受控修复",
            "detail": "已有修复活动；继续沿现有会话、审批或验证记录推进。",
        }
    if diagnoses:
        return {
            "kind": "start_repair",
            "label": "从诊断开始修复",
            "detail": "已有 Evidence 绑定的诊断；可创建受控修复而不直接修改源工作区。",
        }
    if run.state in _FAILED_SOURCE_STATES:
        return {
            "kind": "inspect_failure",
            "label": "先检查失败证据",
            "detail": "尚无持久化诊断；先确认 stderr、Slurm 状态与 Evidence。",
        }
    return {
        "kind": "no_repair_needed",
        "label": "当前没有修复任务",
        "detail": "此 Run 尚未形成需要修复的持久化事实。",
    }


def _json_object(value: object) -> dict[str, Any]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise ValueError("persisted repair workspace object is invalid")
    return parsed


def _json_string_list(value: object) -> list[str]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError("persisted repair workspace list is invalid")
    return parsed


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)
