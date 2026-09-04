"""Evidence-first read models for one Run and its repair workspace.

These projections perform no collection, diagnosis, repair, Agent, or Slurm
actions. They only organize already-persisted authority into decision-oriented
responses for the web workspace. The repair projection deliberately omits raw
LLM payloads and code-context bodies; callers receive IDs, state, evidence
bindings, approval facts, validation outcomes, and derived Run lineage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pilot107.core.contracts import ContractStore
from pilot107.core.remediation import RemediationState
from pilot107.core.remediation_store import RemediationStore
from pilot107.core.repair_ticket_store import RepairTicketStore
from pilot107.core.run_store import DiagnosisRecord, EvidenceObjectRecord, RunRecord, RunStore
from pilot107.core.states import CollectionState, RunState

_FAILED_RUN_STATES = frozenset(
    {
        RunState.FAILED,
        RunState.SUBMIT_FAILED,
        RunState.AUTH_REQUIRED,
        RunState.ORPHANED,
    }
)
_QUEUE_RUN_STATES = frozenset(
    {
        RunState.DRAFT,
        RunState.VALIDATED,
        RunState.SUBMITTING,
        RunState.SUBMITTED,
        RunState.PENDING,
        RunState.SUBMISSION_UNCERTAIN,
    }
)
_ACTIVE_RUN_STATES = frozenset({RunState.RUNNING, RunState.COMPLETING, RunState.UNKNOWN})
_REPAIR_ACTIVE_RUN_STATES = _QUEUE_RUN_STATES | _ACTIVE_RUN_STATES
_REPAIR_TERMINAL_FAILURE_STATES = frozenset(
    {
        RemediationState.EXHAUSTED,
        RemediationState.BLOCKED,
        RemediationState.FAILED,
        RemediationState.CANCELLED,
    }
)


@dataclass(frozen=True)
class RunWorkspaceService:
    store: RunStore
    contract_store: ContractStore | None = None
    remediation_store: RemediationStore | None = None
    repair_ticket_store: RepairTicketStore | None = None

    def __post_init__(self) -> None:
        # These are adapters over the same control database, not new authority.
        # Pilot107HttpApi already initializes the corresponding stores; this
        # fallback keeps the projection usable in focused service tests and
        # older composition roots without coupling the route to Agent services.
        if self.remediation_store is None:
            object.__setattr__(self, "remediation_store", RemediationStore(self.store.db_path))
        if self.repair_ticket_store is None:
            object.__setattr__(self, "repair_ticket_store", RepairTicketStore(self.store.db_path))

    def get(self, run_id: str, *, owner: str | None = None) -> dict[str, Any]:
        run = self._authorized_run(run_id, owner=owner)
        evidence = self.store.list_evidence_objects(run_id)
        diagnoses = self.store.list_diagnoses(run_id)
        contract_digest = self._contract_digest(run)
        outcome = _outcome(run)
        attention = _attention(run, diagnoses)
        next_action = _next_action(run, diagnoses)

        return {
            "run": _run_identity(run),
            "states": {
                "execution": run.state.value,
                "collection": run.collection_state.value,
                "diagnosis": run.diagnosis_state.value,
                "capsule": run.capsule_state.value,
                "result": run.result_status.value,
            },
            "outcome": outcome,
            "attention": attention,
            "next_action": next_action,
            "evidence_summary": _evidence_summary(run, evidence, diagnoses),
            "provenance": {
                "contract_id": run.contract_id,
                "contract_digest": contract_digest,
                "workdir": run.workdir,
                "job_id": run.job_id,
                "parent_run_id": run.parent_run_id,
                "lineage_reason": run.lineage_reason,
                "remediation_plan_id": run.remediation_plan_id,
                "workspace_revision": run.workspace_revision,
                "workspace_digest": run.workspace_digest,
                "source_revision": run.source_revision,
                "platform_snapshot_ref": run.platform_snapshot_ref,
            },
        }

    def get_repair(self, run_id: str, *, owner: str | None = None) -> dict[str, Any]:
        """Project the persisted failure-recovery graph for one source Run.

        This is intentionally a read model: Diagnosis, Remediation, Agent
        advice/action records, RepairTicket, and child Run records remain their
        existing authorities. No repair state is written here.
        """

        run = self._authorized_run(run_id, owner=owner)
        diagnoses = self.store.list_diagnoses(run_id)
        sessions = self._remediation_sessions(run)
        agent = self._agent_trace(run)
        tickets = self._repair_tickets(run)
        derived_runs = self.store.list_child_runs(run.run_id)
        status = _repair_status(sessions, agent, tickets, derived_runs)

        return {
            "schema_version": "pilot107.repair-workspace/v1",
            "source_run": {
                "run_id": run.run_id,
                "owner": run.owner,
                "state": run.state.value,
                "collection_state": run.collection_state.value,
                "diagnosis_state": run.diagnosis_state.value,
                "result_status": run.result_status.value,
                "contract_id": run.contract_id,
                "updated_at": run.updated_at,
            },
            "diagnoses": [_repair_diagnosis(item) for item in diagnoses],
            "remediation_sessions": sessions,
            "agent": agent,
            "repair_tickets": tickets,
            "derived_runs": [_derived_run(item) for item in derived_runs],
            "status": status,
            "next_action": _repair_next_action(
                run,
                diagnoses=diagnoses,
                sessions=sessions,
                agent=agent,
                tickets=tickets,
                derived_runs=derived_runs,
            ),
        }

    def _authorized_run(self, run_id: str, *, owner: str | None) -> RunRecord:
        run = self.store.get_run(run_id)
        if owner is not None and run.owner != owner:
            raise PermissionError("run is owned by another user")
        return run

    def _remediation_sessions(self, run: RunRecord) -> list[dict[str, Any]]:
        assert self.remediation_store is not None
        with self.remediation_store.connect() as conn:
            rows = conn.execute(
                "SELECT session_id FROM remediation_sessions "
                "WHERE owner = ? AND source_run_id = ? "
                "ORDER BY updated_at DESC, session_id DESC LIMIT 50",
                (run.owner, run.run_id),
            ).fetchall()
        projected: list[dict[str, Any]] = []
        for row in rows:
            session = self.remediation_store.get_session(str(row["session_id"]))
            turns = self.remediation_store.list_turns(session.session_id)
            proposals = self.remediation_store.list_proposals(session.session_id)
            decisions = self.remediation_store.list_decisions(session.session_id)
            executions = self.remediation_store.list_executions(session.session_id)
            evaluations = self.remediation_store.list_evaluations(session.session_id)
            projected.append(
                {
                    "session_id": session.session_id,
                    "state": session.state.value,
                    "version": session.version,
                    "automation_policy": session.automation_policy,
                    "provider": session.provider,
                    "stop_reason": session.stop_reason,
                    "takeover_reason": session.takeover_reason,
                    "budget": session.budget.to_payload(),
                    "usage": session.usage.to_payload(),
                    "created_at": session.created_at,
                    "updated_at": session.updated_at,
                    "turns": [
                        {
                            "turn_id": item.turn_id,
                            "turn_index": item.turn_index,
                            "state": item.state,
                            "advice_id": item.advice_id,
                            "created_at": item.created_at,
                            "updated_at": item.updated_at,
                        }
                        for item in turns
                    ],
                    "proposals": [
                        {
                            "proposal_id": item.proposal_id,
                            "turn_id": item.turn_id,
                            "action_id": item.action_id,
                            "action_type": item.action_type,
                            "source": item.source,
                            "risk": item.risk,
                            "approval_required": item.approval_required,
                            "policy_status": item.policy_status,
                            "created_at": item.created_at,
                        }
                        for item in proposals
                    ],
                    "decisions": [
                        {
                            "decision_id": item.decision_id,
                            "proposal_id": item.proposal_id,
                            "actor": item.actor,
                            "decision": item.decision,
                            "note": item.note,
                            "created_at": item.created_at,
                        }
                        for item in decisions
                    ],
                    "executions": [
                        {
                            "execution_id": item.execution_id,
                            "proposal_id": item.proposal_id,
                            "state": item.state,
                            "derived_contract_id": item.derived_contract_id,
                            "derived_run_id": item.derived_run_id,
                            "error_code": item.error_code,
                            "error_message": item.error_message,
                            "created_at": item.created_at,
                            "updated_at": item.updated_at,
                        }
                        for item in executions
                    ],
                    "evaluations": [
                        {
                            "evaluation_id": item.evaluation_id,
                            "execution_id": item.execution_id,
                            "derived_run_id": item.derived_run_id,
                            "outcome": item.outcome.value,
                            "checks": list(item.checks),
                            "comparison": item.comparison,
                            "evidence_refs": list(item.evidence_refs),
                            "created_at": item.created_at,
                        }
                        for item in evaluations
                    ],
                }
            )
        return projected

    def _agent_trace(self, run: RunRecord) -> dict[str, Any]:
        advice, _ = self.store.list_agent_advice_page(
            owner=run.owner,
            run_id=run.run_id,
            limit=100,
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
                    "note": decision.note,
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
            "truncated": len(advice) == 100,
        }

    def _repair_tickets(self, run: RunRecord) -> list[dict[str, Any]]:
        assert self.repair_ticket_store is not None
        with self.repair_ticket_store.connect() as conn:
            rows = conn.execute(
                "SELECT ticket_id FROM repair_tickets "
                "WHERE owner = ? AND source_run_id = ? "
                "ORDER BY updated_at DESC, ticket_id DESC LIMIT 50",
                (run.owner, run.run_id),
            ).fetchall()
        tickets = [self.repair_ticket_store.get_ticket(str(row["ticket_id"])) for row in rows]
        return [
            {
                "ticket_id": item.ticket_id,
                "state": item.state.value,
                "session_id": item.session_id,
                "source_contract_id": item.source_contract_id,
                "diagnosis_ids": list(item.diagnosis_ids),
                "requested_change": item.requested_change,
                "resolution_manifest_id": item.resolution_manifest_id,
                "resolution_run_id": item.resolution_run_id,
                "resolution_comparison": item.resolution_comparison,
                "abandon_reason": item.abandon_reason,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
            for item in tickets
        ]

    def _contract_digest(self, run: RunRecord) -> str | None:
        if self.contract_store is None or run.contract_id is None:
            return None
        try:
            contract = self.contract_store.get_contract(run.contract_id)
        except KeyError:
            # Legacy/imported Runs may reference a Contract that is not present
            # in the current control database. Missing provenance is reported
            # as null rather than turning a readable Run into a 500 response.
            return None
        if contract.owner != run.owner:
            # Never bind provenance across owners, even if storage corruption or
            # a legacy fixture creates a colliding Contract id.
            return None
        return contract.digest or None


def _run_identity(run: RunRecord) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "owner": run.owner,
        "job_name": run.job_name,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "exit_code": run.exit_code,
        "terminal_state": run.terminal_state,
        "attempt": run.attempt,
    }


def _repair_diagnosis(item: DiagnosisRecord) -> dict[str, Any]:
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


def _derived_run(item: RunRecord) -> dict[str, Any]:
    return {
        "run_id": item.run_id,
        "state": item.state.value,
        "collection_state": item.collection_state.value,
        "result_status": item.result_status.value,
        "lineage_reason": item.lineage_reason,
        "remediation_plan_id": item.remediation_plan_id,
        "attempt": item.attempt,
        "job_id": item.job_id,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _repair_status(
    sessions: list[dict[str, Any]],
    agent: dict[str, Any],
    tickets: list[dict[str, Any]],
    derived_runs: list[RunRecord],
) -> dict[str, bool]:
    awaiting_approval = any(
        item["state"] == RemediationState.AWAITING_APPROVAL.value for item in sessions
    )
    verified_success = any(
        evaluation["outcome"] == "verified_success"
        for session in sessions
        for evaluation in session["evaluations"]
    )
    return {
        "has_repair_activity": bool(
            sessions or agent["advice"] or agent["executions"] or tickets or derived_runs
        ),
        "awaiting_approval": awaiting_approval,
        "has_derived_run": bool(derived_runs),
        "verified_success": verified_success,
    }


def _repair_next_action(
    run: RunRecord,
    *,
    diagnoses: list[DiagnosisRecord],
    sessions: list[dict[str, Any]],
    agent: dict[str, Any],
    tickets: list[dict[str, Any]],
    derived_runs: list[RunRecord],
) -> dict[str, str]:
    if any(item["state"] == RemediationState.AWAITING_APPROVAL.value for item in sessions):
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
    if any(
        evaluation["outcome"] == "verified_success"
        for session in sessions
        for evaluation in session["evaluations"]
    ):
        return {
            "kind": "compare_outcome",
            "label": "比较修复前后结果",
            "detail": "已有验证成功的修复运行；比较配置、修改与 Evidence 后再形成结论。",
        }
    if derived_runs and any(item.state == RunState.SUCCEEDED for item in derived_runs):
        return {
            "kind": "compare_outcome",
            "label": "比较修复前后结果",
            "detail": "已有计算成功的派生运行；仍需检查 Evidence 与科学结果。",
        }
    if sessions and all(
        RemediationState(item["state"]) in _REPAIR_TERMINAL_FAILURE_STATES for item in sessions
    ):
        return {
            "kind": "review_repair_failure",
            "label": "检查修复为何停止",
            "detail": "已有修复会话停止或失败；先查看 stop reason、Evidence 与执行错误。",
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
    if run.state in _FAILED_RUN_STATES:
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


def _outcome(run: RunRecord) -> dict[str, str]:
    if run.state in _FAILED_RUN_STATES:
        return {"kind": "failed", "summary": _failure_summary(run)}
    if run.state == RunState.COLLECTION_FAILED or run.collection_state == CollectionState.FAILED:
        return {
            "kind": "collection_failed",
            "summary": "计算阶段已经结束，但运行证据或结果整理失败。",
        }
    if run.state == RunState.EVIDENCE_PARTIAL or run.collection_state == CollectionState.DEGRADED:
        return {
            "kind": "collection_failed",
            "summary": "运行证据仅部分收集完成，需要检查缺失对象。",
        }
    if run.state == RunState.SUCCEEDED:
        if run.collection_state in {CollectionState.PENDING, CollectionState.RUNNING}:
            return {"kind": "collecting", "summary": "计算已完成，正在整理运行证据与结果。"}
        return {"kind": "succeeded", "summary": "计算已完成；科学结果仍需根据输出与证据评价。"}
    if run.state in _ACTIVE_RUN_STATES:
        return {"kind": "running", "summary": "作业正在 Slurm 中执行或完成收尾。"}
    if run.state in _QUEUE_RUN_STATES:
        return {"kind": "queued", "summary": "运行尚未完成，正在准备提交或等待调度。"}
    if run.state == RunState.CANCELLED:
        return {"kind": "failed", "summary": "运行已取消。"}
    return {"kind": "queued", "summary": "运行状态尚未形成终态。"}


def _failure_summary(run: RunRecord) -> str:
    if run.state == RunState.SUBMIT_FAILED:
        return "运行在 Slurm 提交阶段失败。"
    if run.state == RunState.AUTH_REQUIRED:
        return "运行需要重新确认集群身份或授权。"
    if run.state == RunState.ORPHANED:
        return "运行记录与集群作业的绑定已经失去权威对应。"
    if run.exit_code:
        return f"作业以非成功状态结束，退出码 {run.exit_code}。"
    return "作业以失败状态结束。"


def _attention(run: RunRecord, diagnoses: list[DiagnosisRecord]) -> dict[str, str | None]:
    if diagnoses:
        primary = diagnoses[0]
        severity = "critical" if primary.severity.lower() in {"error", "critical"} else "warning"
        return {
            "severity": severity,
            "title": primary.summary,
            "detail": f"依据已有诊断 {primary.rule_id}；可继续查看其 Evidence 引用。",
        }
    if run.state in _FAILED_RUN_STATES:
        return {
            "severity": "critical",
            "title": _failure_summary(run),
            "detail": "尚无持久化诊断；下一步应先检查运行证据。",
        }
    if run.state == RunState.COLLECTION_FAILED or run.collection_state in {
        CollectionState.FAILED,
        CollectionState.DEGRADED,
    }:
        return {
            "severity": "warning",
            "title": "运行证据整理不完整",
            "detail": "计算状态与证据收集状态是独立事实；请检查收集任务与缺失对象。",
        }
    if run.state in _ACTIVE_RUN_STATES or run.state in _QUEUE_RUN_STATES:
        return {
            "severity": "info",
            "title": None,
            "detail": None,
        }
    return {"severity": "none", "title": None, "detail": None}


def _next_action(run: RunRecord, diagnoses: list[DiagnosisRecord]) -> dict[str, str]:
    if run.state in _FAILED_RUN_STATES:
        if diagnoses:
            return {
                "kind": "prepare_repair",
                "label": "查看诊断并准备修复",
                "detail": "已有持久化诊断；先核对 Evidence，再决定是否进入受控修复。",
            }
        return {
            "kind": "inspect_failure",
            "label": "查看失败证据",
            "detail": "尚无持久化诊断；先查看 stderr、运行状态与其他 Evidence。",
        }
    if run.state == RunState.COLLECTION_FAILED or run.collection_state in {
        CollectionState.FAILED,
        CollectionState.DEGRADED,
    }:
        return {
            "kind": "inspect_collection",
            "label": "检查证据收集",
            "detail": "计算与证据收集是独立状态；先确认缺失或失败的收集任务。",
        }
    if run.state == RunState.SUCCEEDED:
        if run.collection_state in {CollectionState.PENDING, CollectionState.RUNNING}:
            return {
                "kind": "wait_collection",
                "label": "查看结果整理",
                "detail": "计算已经成功结束，但 Evidence 尚未完成整理。",
            }
        return {
            "kind": "view_results",
            "label": "查看结果与证据",
            "detail": "计算与证据整理已完成；继续评价实际输出，不自动推断科学结论。",
        }
    if run.state in _ACTIVE_RUN_STATES:
        return {
            "kind": "watch_run",
            "label": "查看实时状态",
            "detail": "查看当前 Slurm 状态和增量日志。",
        }
    return {
        "kind": "watch_queue",
        "label": "查看提交与排队状态",
        "detail": "运行尚未进入终态；查看提交、排队或调度状态。",
    }


def _evidence_summary(
    run: RunRecord,
    evidence: list[EvidenceObjectRecord],
    diagnoses: list[DiagnosisRecord],
) -> dict[str, Any]:
    logical_paths = {obj.logical_path.lower() for obj in evidence}
    result_count = sum(
        1
        for obj in evidence
        if obj.category.lower() in {"outputs", "results"}
        or obj.logical_path.lower().startswith(("outputs/", "results/"))
    )
    return {
        "object_count": len(evidence),
        "result_count": result_count,
        "diagnosis_count": len(diagnoses),
        "stdout_available": any("stdout" in path for path in logical_paths),
        "stderr_available": any("stderr" in path for path in logical_paths),
        "capsule_available": run.capsule_state.value == "ready",
    }
