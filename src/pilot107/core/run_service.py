"""Minimal Run service for Phase 0A."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from pilot107.adapters.slurm import (
    SlurmBackend,
    SlurmBackendError,
    SlurmTransportError,
    SubmissionStrategy,
    SubmitIntent,
    SubmitReceipt,
)
from pilot107.core.preflight import PathChecker, preflight_workdir_fs, preflight_workdir_paths
from pilot107.core.resources import (
    ArraySpec,
    PreflightFinding,
    PreflightSeverity,
    ResourcePlan,
    validate_resource_plan,
)
from pilot107.core.run_store import RunRecord, RunStore
from pilot107.core.states import RunState
from pilot107.core.submission_reconcile import ReconcileBackend, reconcile_submission


@dataclass(frozen=True)
class RunSubmitRequest:
    owner: str
    workdir: Path
    script: str
    resource_plan: ResourcePlan
    contract_id: str | None = None
    parent_run_id: str | None = None
    lineage_reason: str | None = None
    remediation_plan_id: str | None = None
    workflow: WorkflowPolicy = field(default_factory=lambda: WorkflowPolicy())


@dataclass(frozen=True)
class WorkflowPolicy:
    dependencies: tuple[str, ...] = ()
    max_attempts: int = 1
    backoff_seconds: int = 0
    automation_level: str = "explain"
    require_approval: bool = True

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> WorkflowPolicy:
        if payload is None:
            return cls()
        if not isinstance(payload, dict):
            raise ValueError("workflow must be an object")
        retry = payload.get("retry", {})
        automation = payload.get("automation", {})
        if not isinstance(retry, dict) or not isinstance(automation, dict):
            raise ValueError("workflow retry and automation must be objects")
        dependencies = payload.get("dependencies", [])
        if not isinstance(dependencies, list) or not all(
            isinstance(item, str) and item for item in dependencies
        ):
            raise ValueError("workflow.dependencies must contain run ids")
        if len(dependencies) != len(set(dependencies)):
            raise ValueError("workflow.dependencies must be unique")
        policy = cls(
            dependencies=tuple(dependencies),
            max_attempts=_bounded_int(retry.get("max_attempts", 1), "max_attempts", 1, 10),
            backoff_seconds=_bounded_int(
                retry.get("backoff_seconds", 0), "backoff_seconds", 0, 86400
            ),
            automation_level=str(automation.get("level", "explain")),
            require_approval=automation.get("require_approval", True),
        )
        if policy.automation_level not in {
            "explain",
            "suggest",
            "approved_execute",
            "bounded_auto",
        }:
            raise ValueError("workflow automation level is invalid")
        if not isinstance(policy.require_approval, bool):
            raise ValueError("workflow automation require_approval must be boolean")
        return policy

    def to_payload(self) -> dict[str, Any]:
        return {
            "dependencies": list(self.dependencies),
            "retry": {
                "max_attempts": self.max_attempts,
                "backoff_seconds": self.backoff_seconds,
            },
            "automation": {
                "level": self.automation_level,
                "require_approval": self.require_approval,
            },
        }


class WorkDirPreflightError(SlurmBackendError):
    """Raised when workdir (or resource-plan) preflight blocks a submit.

    Carries the full aggregated :class:`PreflightFinding` list so the HTTP
    layer can surface a structured 422 response. Subclasses
    :class:`SlurmBackendError` so existing error handling still applies.
    """

    def __init__(self, findings: list[PreflightFinding]) -> None:
        self.findings = findings
        blockers = [f for f in findings if f.severity == PreflightSeverity.BLOCK]
        codes = ", ".join(f.code for f in blockers)
        super().__init__(f"workdir preflight blocked: {codes}")


class SubmissionUncertainError(SlurmBackendError):
    """Raised when reconciliation finds multiple candidate jobs.

    The run is already persisted in ``SUBMISSION_UNCERTAIN`` state before this
    is raised; the message intentionally contains only the candidate count and
    job_ids — never tokens or full job payloads.
    """

    def __init__(self, *, job_ids: list[str]) -> None:
        self.job_ids = job_ids
        super().__init__(
            f"submission uncertain; {len(job_ids)} candidate jobs: {job_ids}"
        )


class WorkflowDependencyError(SlurmBackendError):
    pass


class WorkflowRetryNotReadyError(SlurmBackendError):
    pass


class SubmissionInProgressError(SlurmBackendError):
    pass


class RunService:
    def __init__(
        self,
        *,
        store: RunStore,
        backend: SlurmBackend,
        workdir_preflight_enabled: bool = False,
        preflight_allowed_roots: tuple[str, ...] = (),
        preflight_shared_roots: tuple[str, ...] = (),
        preflight_local_roots: tuple[str, ...] = (),
        preflight_path_checker: PathChecker | None = None,
        preflight_path_checker_factory: Callable[[str], PathChecker] | None = None,
        idempotency_reconcile_enabled: bool = False,
        reconcile_backend: ReconcileBackend | None = None,
        job_name_marker: str = "pilot107-run",
        reconcile_time_window_seconds: float = 60.0,
    ) -> None:
        self.store = store
        self.backend = backend
        self.workdir_preflight_enabled = workdir_preflight_enabled
        self.preflight_allowed_roots = preflight_allowed_roots
        self.preflight_shared_roots = preflight_shared_roots
        self.preflight_local_roots = preflight_local_roots
        if preflight_path_checker is not None and preflight_path_checker_factory is not None:
            raise ValueError(
                "preflight_path_checker and preflight_path_checker_factory are mutually exclusive"
            )
        self.preflight_path_checker = preflight_path_checker
        self.preflight_path_checker_factory = preflight_path_checker_factory
        self.idempotency_reconcile_enabled = idempotency_reconcile_enabled
        self.reconcile_backend = reconcile_backend
        self.job_name_marker = job_name_marker
        self.reconcile_time_window_seconds = reconcile_time_window_seconds

    def submit(self, request: RunSubmitRequest) -> RunRecord:
        run = self.prepare(request)
        return self.submit_prepared(run.run_id)

    def prepare(
        self,
        request: RunSubmitRequest,
        *,
        run_id: str | None = None,
        idempotent: bool = False,
    ) -> RunRecord:
        selected_run_id = run_id or f"run_{uuid4().hex}"
        try:
            created = self.store.create_run(
                run_id=selected_run_id,
                owner=request.owner,
                workdir=str(request.workdir),
                script=request.script,
                resource_plan=_resource_plan_to_dict(request.resource_plan),
                contract_id=request.contract_id,
                parent_run_id=request.parent_run_id,
                lineage_reason=request.lineage_reason,
                remediation_plan_id=request.remediation_plan_id,
                workflow=request.workflow.to_payload(),
            )
        except sqlite3.IntegrityError:
            if not idempotent:
                raise
            created = self.store.get_run(selected_run_id)
        if idempotent and (
            created.owner != request.owner
            or created.contract_id != request.contract_id
            or created.parent_run_id != request.parent_run_id
            or created.lineage_reason != request.lineage_reason
            or created.remediation_plan_id != request.remediation_plan_id
            or created.workdir != str(request.workdir)
            or created.script != request.script
            or created.resource_plan != _resource_plan_to_dict(request.resource_plan)
            or created.workflow != request.workflow.to_payload()
        ):
            raise SlurmBackendError("idempotent run id refers to different content")
        return created

    def submit_prepared(self, run_id: str) -> RunRecord:
        run = self.store.get_run(run_id)
        if run.job_id is not None:
            return run
        if run.state == RunState.SUBMITTING:
            raise SubmissionInProgressError("run submission is already in progress")
        if run.state != RunState.VALIDATED:
            raise SlurmBackendError(f"run cannot be submitted from state: {run.state}")
        if not run.resource_plan:
            raise SlurmBackendError("run has no resource_plan")
        self._require_retry_due(run)
        dependency_job_ids = self._resolve_dependency_job_ids(run)
        self._run_preflight(run)
        if not self.store.claim_submission(run_id):
            current = self.store.get_run(run_id)
            if current.job_id is not None:
                return current
            raise SubmissionInProgressError("run submission is already in progress")
        intent = SubmitIntent(
            user=run.owner,
            workdir=Path(run.workdir),
            script=run.script,
            resource_plan=_resource_plan_from_dict(run.resource_plan),
            idempotency_key=f"{run_id}:submit",
            dependency_job_ids=dependency_job_ids,
        )
        submitted_after = time.time()
        try:
            receipt = self.backend.submit(intent)
        except SlurmTransportError:
            if self.idempotency_reconcile_enabled and self.reconcile_backend is not None:
                return self._apply_reconcile_result(run_id, run, intent, submitted_after)
            self.store.update_state(
                run_id, RunState.SUBMIT_FAILED, event_type="run.submit_failed"
            )
            raise
        except SlurmBackendError:
            self.store.update_state(
                run_id, RunState.SUBMIT_FAILED, event_type="run.submit_failed"
            )
            raise
        return self.store.apply_submit_receipt(run_id, receipt)

    def get(self, run_id: str) -> RunRecord:
        return self.store.get_run(run_id)

    def reconcile_once(self, run_id: str) -> RunRecord:
        run = self.store.get_run(run_id)
        if run.job_id is None:
            return run
        snapshot = self.backend.get_job(user=run.owner, job_id=run.job_id)
        reconciled = self.store.apply_snapshot(run_id, snapshot)
        if reconciled.state == RunState.FAILED:
            self._schedule_workflow_retry(reconciled)
        return reconciled

    def submit_due_workflow_retries(self, *, limit: int = 50) -> list[RunRecord]:
        submitted: list[RunRecord] = []
        for run in self.store.list_due_workflow_retries(limit=limit):
            submitted.append(self.submit_prepared(run.run_id))
        return submitted

    def cancel(self, run_id: str) -> RunRecord:
        run = self.store.get_run(run_id)
        if run.job_id is None:
            return self.store.update_state(
                run_id, RunState.CANCELLED, event_type="run.cancelled"
            )
        snapshot = self.backend.cancel(user=run.owner, job_id=run.job_id)
        return self.store.apply_snapshot(run_id, snapshot)

    # ------------------------------------------------------------------ #
    # Preflight + reconciliation helpers
    # ------------------------------------------------------------------ #

    def _run_preflight(self, run: RunRecord) -> None:
        """Aggregate resource-plan and workdir preflight findings.

        Resource-plan findings are always re-evaluated (basic checks; the
        QoS-aware checks run at the HTTP prepare boundary and are not
        duplicated here). Workdir findings run only when
        ``workdir_preflight_enabled`` is set. If any BLOCK finding is present,
        :class:`WorkDirPreflightError` is raised and ``backend.submit`` is
        never called.
        """
        findings: list[PreflightFinding] = list(
            validate_resource_plan(_resource_plan_from_dict(run.resource_plan))
        )
        if self.workdir_preflight_enabled:
            findings.extend(self._workdir_findings(run))
        blockers = [f for f in findings if f.severity == PreflightSeverity.BLOCK]
        if blockers:
            raise WorkDirPreflightError(findings)

    def _resolve_dependency_job_ids(self, run: RunRecord) -> tuple[str, ...]:
        policy = WorkflowPolicy.from_payload(run.workflow)
        job_ids: list[str] = []
        for dependency_id in policy.dependencies:
            if dependency_id == run.run_id:
                raise WorkflowDependencyError("run cannot depend on itself")
            try:
                dependency = self.store.get_run(dependency_id)
            except KeyError as exc:
                raise WorkflowDependencyError(
                    f"workflow dependency does not exist: {dependency_id}"
                ) from exc
            if dependency.owner != run.owner:
                raise WorkflowDependencyError("workflow dependency owner does not match run owner")
            if dependency.state == RunState.SUCCEEDED:
                continue
            if dependency.state in {RunState.FAILED, RunState.CANCELLED, RunState.SUBMIT_FAILED}:
                raise WorkflowDependencyError(
                    f"workflow dependency did not succeed: {dependency_id}"
                )
            if dependency.job_id is None:
                raise WorkflowDependencyError(
                    f"workflow dependency has not been submitted: {dependency_id}"
                )
            job_ids.append(dependency.job_id)
        if policy.dependencies:
            self.store.append_event(
                run_id=run.run_id,
                event_type="workflow.dependencies_resolved",
                payload={
                    "dependency_run_ids": list(policy.dependencies),
                    "dependency_job_ids": job_ids,
                },
            )
        return tuple(job_ids)

    def _schedule_workflow_retry(self, run: RunRecord) -> RunRecord | None:
        policy = WorkflowPolicy.from_payload(run.workflow)
        if run.attempt + 1 >= policy.max_attempts:
            self.store.append_event(
                run_id=run.run_id,
                event_type="workflow.retry_exhausted",
                payload={"attempt": run.attempt, "max_attempts": policy.max_attempts},
            )
            return None
        if policy.automation_level != "bounded_auto" or policy.require_approval:
            self.store.append_event(
                run_id=run.run_id,
                event_type="workflow.retry_approval_required",
                payload={
                    "attempt": run.attempt,
                    "max_attempts": policy.max_attempts,
                    "automation_level": policy.automation_level,
                },
            )
            return None
        digest = hashlib.sha256(f"{run.run_id}:{run.attempt + 1}".encode()).hexdigest()[:32]
        retry_run_id = f"run_retry_{digest}"
        not_before = (
            datetime.now(UTC) + timedelta(seconds=policy.backoff_seconds)
        ).isoformat()
        try:
            retry = self.store.create_run(
                run_id=retry_run_id,
                contract_id=run.contract_id,
                owner=run.owner,
                workdir=run.workdir,
                script=run.script,
                resource_plan=run.resource_plan,
                parent_run_id=run.run_id,
                lineage_reason="workflow_retry",
                workflow=run.workflow,
                retry_not_before=not_before,
            )
        except sqlite3.IntegrityError:
            return self.store.get_run(retry_run_id)
        self.store.append_event(
            run_id=run.run_id,
            event_type="workflow.retry_scheduled",
            payload={
                "retry_run_id": retry.run_id,
                "attempt": retry.attempt,
                "not_before": not_before,
            },
        )
        return retry

    @staticmethod
    def _require_retry_due(run: RunRecord) -> None:
        if run.retry_not_before is None:
            return
        if datetime.fromisoformat(run.retry_not_before) > datetime.now(UTC):
            raise WorkflowRetryNotReadyError(
                f"workflow retry is not due before {run.retry_not_before}"
            )

    def _workdir_findings(self, run: RunRecord) -> list[PreflightFinding]:
        path_checker = self.preflight_path_checker
        if self.preflight_path_checker_factory is not None:
            path_checker = self.preflight_path_checker_factory(run.owner)
        if path_checker is not None:
            return preflight_workdir_fs(
                workdir=run.workdir,
                allowed_roots=self.preflight_allowed_roots,
                shared_roots=self.preflight_shared_roots,
                local_roots=self.preflight_local_roots,
                path_checker=path_checker,
                user=run.owner,
            )
        return preflight_workdir_paths(
            workdir=run.workdir,
            allowed_roots=self.preflight_allowed_roots,
            shared_roots=self.preflight_shared_roots,
            local_roots=self.preflight_local_roots,
            user=run.owner,
        )

    def _apply_reconcile_result(
        self,
        run_id: str,
        run: RunRecord,
        intent: SubmitIntent,
        submitted_after: float,
    ) -> RunRecord:
        assert self.reconcile_backend is not None  # narrowed by caller
        result = reconcile_submission(
            backend=self.reconcile_backend,
            user=run.owner,
            job_name_marker=self.job_name_marker,
            submitted_after=submitted_after,
            time_window_seconds=self.reconcile_time_window_seconds,
        )
        if result.state == "bound" and result.job_id is not None:
            receipt = SubmitReceipt(
                job_id=result.job_id,
                run_state=RunState.SUBMITTED,
                strategy=SubmissionStrategy.REST_NATIVE,
                raw_response={"reconciled": True, "job_id": result.job_id},
            )
            return self.store.apply_submit_receipt(run_id, receipt)
        if result.state == "not_found":
            # Safe to retry the submit exactly once. A second transport error
            # is surfaced as SUBMIT_FAILED (no infinite retry loop).
            try:
                receipt = self.backend.submit(intent)
            except SlurmTransportError:
                self.store.update_state(
                    run_id, RunState.SUBMIT_FAILED, event_type="run.submit_failed"
                )
                raise
            return self.store.apply_submit_receipt(run_id, receipt)
        # uncertain: persist the state and surface to the caller.
        self.store.update_state(
            run_id,
            RunState.SUBMISSION_UNCERTAIN,
            event_type="run.submission_uncertain",
        )
        self.store.append_event(
            run_id=run_id,
            event_type="run.submission_uncertain",
            payload={"candidate_job_ids": list(result.matches)},
        )
        raise SubmissionUncertainError(job_ids=list(result.matches))


def _resource_plan_to_dict(plan: ResourcePlan) -> dict[str, Any]:
    payload: dict[str, Any] = {
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
    }
    if plan.array is not None:
        payload["array"] = {
            "expression": plan.array.expression,
            "max_concurrency": plan.array.max_concurrency,
        }
    return payload


def _resource_plan_from_dict(payload: dict[str, Any]) -> ResourcePlan:
    array_payload = payload.get("array")
    return ResourcePlan(
        partition=str(payload["partition"]),
        qos=None if payload.get("qos") is None else str(payload["qos"]),
        nodes=int(payload["nodes"]),
        ntasks=int(payload["ntasks"]),
        cpus_per_task=int(payload["cpus_per_task"]),
        memory_value=None if payload.get("memory_value") is None else int(payload["memory_value"]),
        memory_unit=None if payload.get("memory_unit") is None else str(payload["memory_unit"]),
        gpus_per_node=(
            None if payload.get("gpus_per_node") is None else int(payload["gpus_per_node"])
        ),
        gpus_total=None if payload.get("gpus_total") is None else int(payload["gpus_total"]),
        gpu_type=None if payload.get("gpu_type") is None else str(payload["gpu_type"]),
        time_limit=None if payload.get("time_limit") is None else str(payload["time_limit"]),
        array=None
        if array_payload is None
        else ArraySpec(
            expression=str(array_payload["expression"]),
            max_concurrency=None
            if array_payload.get("max_concurrency") is None
            else int(array_payload["max_concurrency"]),
        ),
    )


def _bounded_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
        or value > maximum
    ):
        raise ValueError(f"workflow {name} must be between {minimum} and {maximum}")
    return value
