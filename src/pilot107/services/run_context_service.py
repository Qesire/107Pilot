"""Evidence-first read model for one Run context.

RunContext is a read-only projection of one Run plus its persisted evidence,
diagnosis and provenance. It is not a WorkArea (research boundary) and it is not
an Agent Workspace (writable/versioned filesystem context).

This module performs no collection, diagnosis, repair, Agent, Slurm, WorkArea or
Workspace mutations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pilot107.core.contracts import ContractStore
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


@dataclass(frozen=True)
class RunContextService:
    store: RunStore
    contract_store: ContractStore | None = None

    def get(self, run_id: str, *, owner: str | None = None) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if owner is not None and run.owner != owner:
            raise PermissionError("run is owned by another user")

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
                # These fields belong to Agent Workspace execution provenance.
                "workspace_revision": run.workspace_revision,
                "workspace_digest": run.workspace_digest,
                "source_revision": run.source_revision,
                "platform_snapshot_ref": run.platform_snapshot_ref,
            },
        }

    def _contract_digest(self, run: RunRecord) -> str | None:
        if self.contract_store is None or run.contract_id is None:
            return None
        try:
            contract = self.contract_store.get_contract(run.contract_id)
        except KeyError:
            # Legacy/imported Runs may reference a Contract absent from the
            # current control database. Missing provenance remains explicit.
            return None
        if contract.owner != run.owner:
            # Never bind provenance across owners, even if a legacy fixture or
            # storage defect creates a colliding Contract id.
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
        return {
            "kind": "succeeded",
            "summary": "计算已完成；科学结果仍需根据输出与证据评价。",
        }
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
        severity = (
            "critical" if primary.severity.lower() in {"error", "critical"} else "warning"
        )
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


__all__ = ["RunContextService"]
