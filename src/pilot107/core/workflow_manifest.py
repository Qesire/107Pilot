"""Versioned artifact-aware workflow truth and recovery orchestration."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from pilot107.core.materializer import compress_array_tasks
from pilot107.core.resources import ArraySpec, ResourcePlan
from pilot107.core.run_service import RunService, RunSubmitRequest, WorkflowPolicy
from pilot107.core.run_store import (
    RunStore,
    WorkflowManifestFenceConflict,
)
from pilot107.core.states import RunState

WorkflowStageKind = Literal["preflight", "array", "merge"]
_TERMINAL = {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}
_ARRAY_TOKEN = re.compile(r"^(\d+)(?:-(\d+))?(?::(\d+))?$")


class WorkflowManifestConflict(RuntimeError):
    pass


class WorkflowArtifactGateError(RuntimeError):
    pass


class WorkflowResourceLimitExceeded(RuntimeError):
    def __init__(
        self,
        *,
        layer: tuple[str, ...],
        requested_cpus: int,
        requested_memory_mib: int,
        requested_gpus: int,
        ceiling: WorkflowResourceCeiling,
    ) -> None:
        self.layer = layer
        self.requested_cpus = requested_cpus
        self.requested_memory_mib = requested_memory_mib
        self.requested_gpus = requested_gpus
        self.ceiling = ceiling
        super().__init__(
            "workflow dependency-layer resource peak exceeds the approved ceiling: "
            f"layer={','.join(layer)} cpus={requested_cpus} "
            f"memory_mib={requested_memory_mib} gpus={requested_gpus}"
        )


@dataclass(frozen=True)
class WorkflowResourceCeiling:
    cpus: int | None = None
    memory_mib: int | None = None
    gpus: int | None = None

    @classmethod
    def unbounded(cls) -> WorkflowResourceCeiling:
        return cls()


@dataclass(frozen=True)
class ArtifactTruth:
    task_index: int
    artifact_path: str
    artifact_sha256: str | None
    metadata_path: str
    metadata_sha256: str | None
    complete_marker_path: str
    complete: bool

    @property
    def verified(self) -> bool:
        return bool(
            self.task_index >= 0
            and self.artifact_path
            and self.artifact_sha256
            and self.metadata_path
            and self.metadata_sha256
            and self.complete_marker_path
            and self.complete
        )


@dataclass(frozen=True)
class WorkflowStageDecision:
    run_id: str
    job_id: str | None
    request_digest: str
    run_state: str
    submitted_tasks: tuple[int, ...] = ()
    reused_verified_tasks: tuple[int, ...] = ()
    recovery_attempt: int = 0


@dataclass(frozen=True)
class WorkflowStage:
    stage_id: str
    kind: WorkflowStageKind
    request: RunSubmitRequest
    dependencies: tuple[str, ...] = ()
    array_tasks: tuple[int, ...] = ()
    artifact_truth: tuple[ArtifactTruth, ...] = ()
    decisions: tuple[WorkflowStageDecision, ...] = ()

    def __post_init__(self) -> None:
        if not self.stage_id:
            raise ValueError("workflow stage_id is required")
        if self.kind not in {"preflight", "array", "merge"}:
            raise ValueError("workflow stage kind is invalid")
        if self.kind == "array" and not self.array_tasks:
            raise ValueError("array workflow stage requires task indexes")
        if self.kind != "array" and self.array_tasks:
            raise ValueError("only array workflow stages may define task indexes")
        if len(set(self.array_tasks)) != len(self.array_tasks) or any(
            item < 0 for item in self.array_tasks
        ):
            raise ValueError("workflow array tasks must be unique non-negative indexes")


@dataclass(frozen=True)
class WorkflowManifest:
    workflow_id: str
    owner: str
    stages: tuple[WorkflowStage, ...]
    state: str = "pending"
    version: int = 0
    cancelled_by: str | None = None

    def stage(self, stage_id: str) -> WorkflowStage:
        for stage in self.stages:
            if stage.stage_id == stage_id:
                return stage
        raise KeyError(stage_id)


@dataclass(frozen=True)
class WorkflowRecoveryPlan:
    workflow_id: str
    stage_id: str
    array_expression: str
    missing_tasks: tuple[int, ...]
    reuses_verified_tasks: tuple[int, ...]
    recovery_attempt: int


class WorkflowService:
    """Owns status, cancel, resume and recovery through one persisted manifest."""

    def __init__(self, *, store: RunStore, run_service: RunService | None) -> None:
        self.store = store
        self.run_service = run_service

    def create(self, manifest: WorkflowManifest) -> WorkflowManifest:
        self.validate(manifest, WorkflowResourceCeiling.unbounded())
        if manifest.version not in {0, 1}:
            raise ValueError("new workflow manifest has an invalid version")
        version, payload = self.store.create_workflow_manifest(
            workflow_id=manifest.workflow_id,
            owner=manifest.owner,
            manifest=_manifest_payload(replace(manifest, version=0)),
        )
        return _manifest_from_payload(payload, version=version)

    def status(self, workflow_id: str, *, actor: str) -> WorkflowManifest:
        version, payload = self.store.get_workflow_manifest(workflow_id, owner=actor)
        return _manifest_from_payload(payload, version=version)

    def save(
        self,
        manifest: WorkflowManifest,
        *,
        expected_version: int,
    ) -> WorkflowManifest:
        try:
            version, payload = self.store.update_workflow_manifest(
                workflow_id=manifest.workflow_id,
                owner=manifest.owner,
                expected_version=expected_version,
                manifest=_manifest_payload(replace(manifest, version=0)),
            )
        except WorkflowManifestFenceConflict as exc:
            raise WorkflowManifestConflict(str(exc)) from exc
        return _manifest_from_payload(payload, version=version)

    def validate(
        self,
        manifest: WorkflowManifest,
        ceiling: WorkflowResourceCeiling,
    ) -> None:
        if not manifest.workflow_id or not manifest.owner or not manifest.stages:
            raise ValueError("workflow identity, owner and stages are required")
        stage_ids = [stage.stage_id for stage in manifest.stages]
        if len(stage_ids) != len(set(stage_ids)):
            raise ValueError("workflow stage ids must be unique")
        known = set(stage_ids)
        for stage in manifest.stages:
            if stage.request.owner != manifest.owner:
                raise ValueError("workflow stage owner does not match manifest owner")
            unknown = set(stage.dependencies) - known
            if unknown:
                raise ValueError(f"workflow stage dependency is unknown: {sorted(unknown)[0]}")
            if stage.stage_id in stage.dependencies:
                raise ValueError("workflow dependency graph contains a cycle")
        for layer in _dependency_layers(manifest.stages):
            cpus = memory_mib = gpus = 0
            for stage in layer:
                parallelism = _stage_parallelism(stage)
                plan = stage.request.resource_plan
                cpus += plan.derived_cpu_upper_bound * parallelism
                memory_mib += _memory_mib(plan) * parallelism
                gpus += plan.derived_gpu_total * parallelism
            if (
                (ceiling.cpus is not None and cpus > ceiling.cpus)
                or (ceiling.memory_mib is not None and memory_mib > ceiling.memory_mib)
                or (ceiling.gpus is not None and gpus > ceiling.gpus)
            ):
                raise WorkflowResourceLimitExceeded(
                    layer=tuple(stage.stage_id for stage in layer),
                    requested_cpus=cpus,
                    requested_memory_mib=memory_mib,
                    requested_gpus=gpus,
                    ceiling=ceiling,
                )

    def resume(self, workflow_id: str, *, actor: str) -> WorkflowManifest:
        manifest = self.status(workflow_id, actor=actor)
        if manifest.state in {"cancelled", "completed"}:
            return manifest
        if self.run_service is None:
            raise RuntimeError("workflow run service is unavailable")
        manifest = self.reconcile(workflow_id, actor=actor)
        original = manifest
        stages = list(manifest.stages)
        for index, stage in enumerate(stages):
            if stage.decisions:
                continue
            dependencies = [
                next(item for item in stages if item.stage_id == dep) for dep in stage.dependencies
            ]
            if any(_stage_failed(item) for item in dependencies):
                continue
            if any(not item.decisions for item in dependencies):
                continue
            if stage.kind == "merge":
                if any(not _stage_succeeded(item) for item in dependencies):
                    continue
                self._require_merge_truth(dependencies)
            current = replace(manifest, stages=tuple(stages))
            stages[index] = self._submit_stage(current, stage, recovery_attempt=0)
        state = "running" if any(stage.decisions for stage in stages) else manifest.state
        if stages and all(_stage_complete(stage) for stage in stages):
            state = "completed"
        updated = replace(manifest, stages=tuple(stages), state=state)
        if updated == original:
            return manifest
        return self.save(updated, expected_version=manifest.version)

    def reconcile(self, workflow_id: str, *, actor: str) -> WorkflowManifest:
        manifest = self.status(workflow_id, actor=actor)
        if self.run_service is None or manifest.state == "cancelled":
            return manifest
        stages: list[WorkflowStage] = []
        changed = False
        for stage in manifest.stages:
            decisions = list(stage.decisions)
            for index, decision in enumerate(decisions):
                decision_state = RunState(decision.run_state)
                run = self.store.get_run(decision.run_id)
                if decision_state not in _TERMINAL and run.job_id is not None:
                    run = self.run_service.reconcile_once(run.run_id)
                refreshed = replace(
                    decision,
                    job_id=run.job_id,
                    run_state=run.state.value,
                )
                if refreshed != decision:
                    decisions[index] = refreshed
                    changed = True
            stages.append(replace(stage, decisions=tuple(decisions)))
        state = manifest.state
        if stages and all(_stage_complete(stage) for stage in stages):
            state = "completed"
        updated = replace(manifest, stages=tuple(stages), state=state)
        if not changed and state == manifest.state:
            return manifest
        return self.save(updated, expected_version=manifest.version)

    def record_artifact_truth(
        self,
        workflow_id: str,
        *,
        stage_id: str,
        truth: tuple[ArtifactTruth, ...],
        actor: str,
    ) -> WorkflowManifest:
        manifest = self.status(workflow_id, actor=actor)
        stage = manifest.stage(stage_id)
        if stage.kind != "array":
            raise ValueError("artifact task truth belongs to an array stage")
        by_index = {item.task_index: item for item in truth}
        if len(by_index) != len(truth) or not set(by_index).issubset(stage.array_tasks):
            raise ValueError("artifact truth contains duplicate or unknown task indexes")
        merged = {item.task_index: item for item in stage.artifact_truth}
        merged.update(by_index)
        updated_stage = replace(
            stage,
            artifact_truth=tuple(merged[key] for key in sorted(merged)),
        )
        updated = _replace_stage(manifest, updated_stage)
        return self.save(updated, expected_version=manifest.version)

    def plan_recovery(self, workflow_id: str, *, actor: str) -> WorkflowRecoveryPlan:
        manifest = self.status(workflow_id, actor=actor)
        for stage in manifest.stages:
            if stage.kind != "array" or not stage.decisions or not _stage_succeeded(stage):
                continue
            verified = _verified_tasks(stage)
            missing = tuple(task for task in stage.array_tasks if task not in verified)
            if not missing:
                continue
            attempt = max(item.recovery_attempt for item in stage.decisions) + 1
            if attempt > 3:
                raise WorkflowArtifactGateError("array recovery attempt limit reached")
            return WorkflowRecoveryPlan(
                workflow_id=manifest.workflow_id,
                stage_id=stage.stage_id,
                array_expression=compress_array_tasks(missing),
                missing_tasks=missing,
                reuses_verified_tasks=tuple(task for task in stage.array_tasks if task in verified),
                recovery_attempt=attempt,
            )
        raise WorkflowArtifactGateError("workflow has no recoverable missing array tasks")

    def recover(self, workflow_id: str, *, actor: str) -> WorkflowManifest:
        if self.run_service is None:
            raise RuntimeError("workflow run service is unavailable")
        manifest = self.reconcile(workflow_id, actor=actor)
        plan = self.plan_recovery(workflow_id, actor=actor)
        stage = manifest.stage(plan.stage_id)
        if any(item.recovery_attempt == plan.recovery_attempt for item in stage.decisions):
            return manifest
        original_array = stage.request.resource_plan.array
        resource_plan = replace(
            stage.request.resource_plan,
            array=ArraySpec(
                plan.array_expression,
                max_concurrency=None if original_array is None else original_array.max_concurrency,
            ),
        )
        parent_run_id = stage.decisions[-1].run_id
        request = replace(
            stage.request,
            resource_plan=resource_plan,
            parent_run_id=parent_run_id,
            lineage_reason="workflow_array_recovery",
        )
        recovered_stage = self._submit_stage(
            manifest,
            replace(stage, request=request),
            recovery_attempt=plan.recovery_attempt,
            submitted_tasks=plan.missing_tasks,
            reused_verified_tasks=plan.reuses_verified_tasks,
        )
        recovered_stage = replace(recovered_stage, request=stage.request)
        updated = _replace_stage(manifest, recovered_stage)
        return self.save(updated, expected_version=manifest.version)

    def cancel(self, workflow_id: str, *, actor: str) -> WorkflowManifest:
        manifest = self.status(workflow_id, actor=actor)
        if manifest.state == "cancelled":
            return manifest
        if self.run_service is None:
            raise RuntimeError("workflow run service is unavailable")
        stages: list[WorkflowStage] = []
        for stage in manifest.stages:
            decisions: list[WorkflowStageDecision] = []
            for decision in stage.decisions:
                run = self.store.get_run(decision.run_id)
                if run.state not in _TERMINAL:
                    run = self.run_service.cancel(run.run_id)
                decisions.append(replace(decision, job_id=run.job_id, run_state=run.state.value))
            stages.append(replace(stage, decisions=tuple(decisions)))
        updated = replace(
            manifest,
            stages=tuple(stages),
            state="cancelled",
            cancelled_by=actor,
        )
        return self.save(updated, expected_version=manifest.version)

    def _submit_stage(
        self,
        manifest: WorkflowManifest,
        stage: WorkflowStage,
        *,
        recovery_attempt: int,
        submitted_tasks: tuple[int, ...] | None = None,
        reused_verified_tasks: tuple[int, ...] = (),
    ) -> WorkflowStage:
        assert self.run_service is not None
        dependency_runs = tuple(
            manifest.stage(dependency).decisions[-1].run_id for dependency in stage.dependencies
        )
        tasks = submitted_tasks
        if tasks is None:
            tasks = stage.array_tasks if stage.kind == "array" else ()
        workflow = replace(
            stage.request.workflow,
            dependencies=dependency_runs,
            manifest_workflow_id=manifest.workflow_id,
            manifest_stage_id=stage.stage_id,
            manifest_stage_kind=stage.kind,
            recovery_attempt=recovery_attempt,
            submitted_tasks=tasks,
            reused_verified_tasks=reused_verified_tasks,
        )
        request = replace(stage.request, workflow=workflow)
        digest = _request_digest(request)
        run = self.run_service.submit_workflow_stage(
            request,
            workflow_id=manifest.workflow_id,
            stage_id=stage.stage_id,
            recovery_attempt=recovery_attempt,
        )
        decision = WorkflowStageDecision(
            run_id=run.run_id,
            job_id=run.job_id,
            request_digest=digest,
            run_state=run.state.value,
            submitted_tasks=tasks,
            reused_verified_tasks=reused_verified_tasks,
            recovery_attempt=recovery_attempt,
        )
        return replace(stage, decisions=(*stage.decisions, decision))

    @staticmethod
    def _require_merge_truth(dependencies: list[WorkflowStage]) -> None:
        for dependency in dependencies:
            if dependency.kind != "array":
                continue
            verified = _verified_tasks(dependency)
            for task in dependency.array_tasks:
                if task not in verified:
                    raise WorkflowArtifactGateError(
                        f"merge blocked: array stage {dependency.stage_id} task {task} "
                        "has invalid artifact/metadata/COMPLETE truth"
                    )


def _dependency_layers(stages: tuple[WorkflowStage, ...]) -> tuple[tuple[WorkflowStage, ...], ...]:
    remaining = {stage.stage_id: stage for stage in stages}
    resolved: set[str] = set()
    layers: list[tuple[WorkflowStage, ...]] = []
    while remaining:
        layer = tuple(
            stage
            for stage in stages
            if stage.stage_id in remaining and set(stage.dependencies).issubset(resolved)
        )
        if not layer:
            raise ValueError("workflow dependency graph contains a cycle")
        layers.append(layer)
        for stage in layer:
            resolved.add(stage.stage_id)
            del remaining[stage.stage_id]
    return tuple(layers)


def _stage_parallelism(stage: WorkflowStage) -> int:
    if stage.kind != "array":
        return 1
    array = stage.request.resource_plan.array
    if array is not None and array.max_concurrency is not None:
        return min(array.max_concurrency, len(stage.array_tasks))
    return len(stage.array_tasks)


def _memory_mib(plan: ResourcePlan) -> int:
    if plan.memory_value is None:
        return 0
    unit = (plan.memory_unit or "M").upper()
    factors = {
        "K": 1 / 1024,
        "KB": 1 / 1024,
        "M": 1,
        "MB": 1,
        "G": 1024,
        "GB": 1024,
        "T": 1024 * 1024,
        "TB": 1024 * 1024,
    }
    if unit not in factors:
        raise ValueError(f"workflow memory unit is unknown: {unit}")
    return int(plan.memory_value * factors[unit])


def _verified_tasks(stage: WorkflowStage) -> set[int]:
    return {item.task_index for item in stage.artifact_truth if item.verified}


def _stage_succeeded(stage: WorkflowStage) -> bool:
    return bool(stage.decisions) and all(
        item.run_state == RunState.SUCCEEDED.value for item in stage.decisions
    )


def _stage_failed(stage: WorkflowStage) -> bool:
    return any(
        item.run_state in {RunState.FAILED.value, RunState.CANCELLED.value}
        for item in stage.decisions
    )


def _stage_complete(stage: WorkflowStage) -> bool:
    if not _stage_succeeded(stage):
        return False
    return stage.kind != "array" or _verified_tasks(stage) == set(stage.array_tasks)


def _replace_stage(manifest: WorkflowManifest, stage: WorkflowStage) -> WorkflowManifest:
    return replace(
        manifest,
        stages=tuple(
            stage if item.stage_id == stage.stage_id else item for item in manifest.stages
        ),
    )


def _request_digest(request: RunSubmitRequest) -> str:
    payload = _request_payload(request)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _manifest_payload(manifest: WorkflowManifest) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workflow_id": manifest.workflow_id,
        "owner": manifest.owner,
        "state": manifest.state,
        "cancelled_by": manifest.cancelled_by,
        "stages": [
            {
                "stage_id": stage.stage_id,
                "kind": stage.kind,
                "request": _request_payload(stage.request),
                "dependencies": list(stage.dependencies),
                "array_tasks": list(stage.array_tasks),
                "artifact_truth": [item.__dict__ for item in stage.artifact_truth],
                "decisions": [
                    {
                        **item.__dict__,
                        "submitted_tasks": list(item.submitted_tasks),
                        "reused_verified_tasks": list(item.reused_verified_tasks),
                    }
                    for item in stage.decisions
                ],
            }
            for stage in manifest.stages
        ],
    }


def _manifest_from_payload(payload: dict[str, Any], *, version: int) -> WorkflowManifest:
    if payload.get("schema_version") != 1:
        raise ValueError("workflow manifest schema version is unsupported")
    stages = tuple(
        WorkflowStage(
            stage_id=str(item["stage_id"]),
            kind=str(item["kind"]),  # type: ignore[arg-type]
            request=_request_from_payload(item["request"]),
            dependencies=tuple(str(value) for value in item.get("dependencies", [])),
            array_tasks=tuple(int(value) for value in item.get("array_tasks", [])),
            artifact_truth=tuple(
                ArtifactTruth(**value) for value in item.get("artifact_truth", [])
            ),
            decisions=tuple(
                WorkflowStageDecision(
                    run_id=str(value["run_id"]),
                    job_id=None if value.get("job_id") is None else str(value["job_id"]),
                    request_digest=str(value["request_digest"]),
                    run_state=str(value["run_state"]),
                    submitted_tasks=tuple(int(task) for task in value.get("submitted_tasks", [])),
                    reused_verified_tasks=tuple(
                        int(task) for task in value.get("reused_verified_tasks", [])
                    ),
                    recovery_attempt=int(value.get("recovery_attempt", 0)),
                )
                for value in item.get("decisions", [])
            ),
        )
        for item in payload["stages"]
    )
    return WorkflowManifest(
        workflow_id=str(payload["workflow_id"]),
        owner=str(payload["owner"]),
        stages=stages,
        state=str(payload.get("state", "pending")),
        version=version,
        cancelled_by=None if payload.get("cancelled_by") is None else str(payload["cancelled_by"]),
    )


def _request_payload(request: RunSubmitRequest) -> dict[str, Any]:
    plan = request.resource_plan
    return {
        "owner": request.owner,
        "workdir": str(request.workdir),
        "script": request.script,
        "job_name": request.job_name,
        "contract_id": request.contract_id,
        "parent_run_id": request.parent_run_id,
        "lineage_reason": request.lineage_reason,
        "remediation_plan_id": request.remediation_plan_id,
        "workflow": request.workflow.to_payload(),
        "resource_plan": {
            "partition": plan.partition,
            "qos": plan.qos,
            "nodes": plan.nodes,
            "ntasks": plan.ntasks,
            "cpus_per_task": plan.cpus_per_task,
            "memory_value": plan.memory_value,
            "memory_unit": plan.memory_unit,
            "gpus_per_node": plan.gpus_per_node,
            "gpus_total": plan.gpus_total,
            "gpu_type": plan.gpu_type,
            "time_limit": plan.time_limit,
            "array": None
            if plan.array is None
            else {
                "expression": plan.array.expression,
                "max_concurrency": plan.array.max_concurrency,
            },
        },
    }


def _request_from_payload(payload: dict[str, Any]) -> RunSubmitRequest:
    raw_plan = payload["resource_plan"]
    raw_array = raw_plan.get("array")
    return RunSubmitRequest(
        owner=str(payload["owner"]),
        workdir=Path(str(payload["workdir"])),
        script=str(payload["script"]),
        job_name=None if payload.get("job_name") is None else str(payload["job_name"]),
        contract_id=None if payload.get("contract_id") is None else str(payload["contract_id"]),
        parent_run_id=None
        if payload.get("parent_run_id") is None
        else str(payload["parent_run_id"]),
        lineage_reason=None
        if payload.get("lineage_reason") is None
        else str(payload["lineage_reason"]),
        remediation_plan_id=None
        if payload.get("remediation_plan_id") is None
        else str(payload["remediation_plan_id"]),
        workflow=WorkflowPolicy.from_payload(payload.get("workflow")),
        resource_plan=ResourcePlan(
            partition=str(raw_plan["partition"]),
            qos=None if raw_plan.get("qos") is None else str(raw_plan["qos"]),
            nodes=int(raw_plan["nodes"]),
            ntasks=int(raw_plan["ntasks"]),
            cpus_per_task=int(raw_plan["cpus_per_task"]),
            memory_value=None
            if raw_plan.get("memory_value") is None
            else int(raw_plan["memory_value"]),
            memory_unit=None
            if raw_plan.get("memory_unit") is None
            else str(raw_plan["memory_unit"]),
            gpus_per_node=None
            if raw_plan.get("gpus_per_node") is None
            else int(raw_plan["gpus_per_node"]),
            gpus_total=None if raw_plan.get("gpus_total") is None else int(raw_plan["gpus_total"]),
            gpu_type=None if raw_plan.get("gpu_type") is None else str(raw_plan["gpu_type"]),
            time_limit=None if raw_plan.get("time_limit") is None else str(raw_plan["time_limit"]),
            array=None
            if raw_array is None
            else ArraySpec(
                str(raw_array["expression"]),
                None
                if raw_array.get("max_concurrency") is None
                else int(raw_array["max_concurrency"]),
            ),
        ),
    )
