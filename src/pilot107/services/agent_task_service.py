"""Durable scheduling and reconciliation for bounded Slurm validation tasks."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import stat
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from pilot107.agent.project import ProjectBlueprint, is_project_agent_profile
from pilot107.agent.store import AgentSessionStore
from pilot107.agent.task_store import AgentTaskStore
from pilot107.agent.tasks import (
    TERMINAL_TASK_STATES,
    AgentResourceEnvelope,
    AgentTaskCompletionPolicy,
    AgentTaskGateState,
    AgentTaskLease,
    AgentTaskRecord,
    AgentTaskRequest,
    AgentTaskResult,
    AgentTaskScheduleReceipt,
    AgentTaskState,
    ResourceEnvelopeExceeded,
)
from pilot107.agent.tool_gateway import AgentReadHandler, AgentReadResult, AgentToolGatewayError
from pilot107.core.control_repository import ControlRepository, OutboxMessage
from pilot107.core.evidence_binding import EvidenceBinder, EvidenceBindingError
from pilot107.core.resources import ResourcePlan
from pilot107.core.run_service import RunService, RunSubmitRequest, WorkflowPolicy
from pilot107.core.states import TERMINAL_RUN_STATES, CapsuleState, CollectionState, RunState
from pilot107.services.agent_session_service import AgentSessionService

AGENT_TASK_EXECUTE_TOPIC = "agent.task.execute.v1"
AGENT_TASK_READY_TOPIC = "agent.task.ready.v1"

type WorkspaceResolver = Callable[[str, str, str], Path]
type RunWorkdirResolver = Callable[[str], Path]
type EnvelopeResolver = Callable[[str, str], AgentResourceEnvelope]
type ProvenanceAuthorityResolver = Callable[[str, str, str], tuple[str, str]]
type CapsuleAuthorityResolver = Callable[[str], str]

_MAX_SNAPSHOT_FILES = 256
_MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class AgentTaskDispatchError:
    task_id: str
    message: str
    retryable: bool = True
    code: str = "AGENT.TASK.DISPATCH_FAILED"


class AgentTaskProvenanceError(ValueError):
    """Stable fail-closed error for unavailable server-side provenance."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class PlatformSnapshotAuthorityStore(Protocol):
    def latest_usable(self, *, owner: str) -> object | None: ...


class CapsuleAuthorityService(Protocol):
    def get_raw_capsule(self, run_id: str) -> object: ...


def build_server_provenance_authority(
    *,
    workspace_resolver: WorkspaceResolver,
    platform_snapshot_store: PlatformSnapshotAuthorityStore,
) -> ProvenanceAuthorityResolver:
    """Bind Run provenance only to approved Workspace and platform authorities."""

    def resolve(owner: str, workspace_id: str, snapshot_digest: str) -> tuple[str, str]:
        workspace_resolver(owner, workspace_id, snapshot_digest)
        try:
            selection = platform_snapshot_store.latest_usable(owner=owner)
        except Exception as exc:
            raise AgentTaskProvenanceError(
                "AgentTask 平台事实权威不可用，未创建 Run。",
                code="AGENT.TASK.PROVENANCE_AUTHORITY_UNAVAILABLE",
            ) from exc
        record = None if selection is None else getattr(selection, "record", None)
        snapshot_id = None if record is None else getattr(record, "snapshot_id", None)
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise AgentTaskProvenanceError(
                "AgentTask 平台事实权威不可用，未创建 Run。",
                code="AGENT.TASK.PROVENANCE_AUTHORITY_UNAVAILABLE",
            )
        return (
            f"workspace-snapshot:sha256:{snapshot_digest}",
            f"snapshot:{snapshot_id}",
        )

    return resolve


def build_verified_capsule_authority(
    service: CapsuleAuthorityService,
) -> CapsuleAuthorityResolver:
    """Return manifest-bound references only for verified raw Capsules."""

    def resolve(run_id: str) -> str:
        capsule = service.get_raw_capsule(run_id)
        if getattr(capsule, "valid", None) is not True:
            raise ValueError("Capsule integrity verification failed")
        capsule_id = getattr(capsule, "capsule_id", None)
        if not isinstance(capsule_id, str) or not capsule_id or "\0" in capsule_id:
            raise ValueError("Capsule identity is invalid")
        digest = getattr(capsule, "manifest_sha256", None)
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("Capsule manifest digest is invalid")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise ValueError("Capsule manifest digest is invalid") from exc
        return f"capsule:{capsule_id}:sha256:{digest.lower()}"

    return resolve


@dataclass(frozen=True)
class AgentTaskDispatchBatch:
    checked: int
    succeeded: int
    errors: list[AgentTaskDispatchError] = field(default_factory=list)


class AgentTaskService:
    def __init__(
        self,
        *,
        store: AgentTaskStore,
        session_store: AgentSessionStore,
        session_service: AgentSessionService,
        run_service: RunService,
        control_repository: ControlRepository,
        workspace_resolver: WorkspaceResolver,
        provenance_authority_resolver: ProvenanceAuthorityResolver | None = None,
        evidence_binder: EvidenceBinder | None = None,
        capsule_authority_resolver: CapsuleAuthorityResolver | None = None,
        run_workdir_resolver: RunWorkdirResolver | None = None,
        worker_id: str,
        lease_seconds: int = 60,
        max_attempts: int = 5,
    ) -> None:
        if not worker_id:
            raise ValueError("AgentTask worker_id is required")
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("AgentTask lease_seconds is invalid")
        if not 1 <= max_attempts <= 100:
            raise ValueError("AgentTask max_attempts is invalid")
        self.store = store
        self.session_store = session_store
        self.session_service = session_service
        self.run_service = run_service
        self.control_repository = control_repository
        self.workspace_resolver = workspace_resolver
        self.provenance_authority_resolver = provenance_authority_resolver
        self.evidence_binder = evidence_binder
        self.capsule_authority_resolver = capsule_authority_resolver
        self.run_workdir_resolver = run_workdir_resolver
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts

    def schedule_validation(
        self,
        *,
        owner: str,
        session_id: str,
        turn_id: str,
        project_id: str,
        workspace_id: str,
        request_key: str,
        request: AgentTaskRequest,
        envelope: AgentResourceEnvelope,
        completion_policy: AgentTaskCompletionPolicy = (
            AgentTaskCompletionPolicy.EVIDENCE_REQUIRED
        ),
    ) -> tuple[AgentTaskRecord, bool]:
        session = self.session_store.get_session(session_id, owner=owner)
        turn = self.session_store.get_turn(turn_id, owner=owner)
        if turn.session_id != session.session_id:
            raise ValueError("AgentTask Turn does not belong to its Session")
        if not is_project_agent_profile(session.profile_id):
            raise ValueError("Slurm validation requires a Project profile")
        if (
            session.source.get("project_id") != project_id
            or session.source.get("workspace_id") != workspace_id
        ):
            raise ValueError("AgentTask Project or Workspace binding is invalid")
        task, created = self.store.create_task(
            owner=owner,
            session_id=session_id,
            turn_id=turn_id,
            project_id=project_id,
            workspace_id=workspace_id,
            task_kind="slurm_validation",
            request_key=request_key,
            request=request,
            envelope=envelope,
            completion_policy=completion_policy,
        )
        self.control_repository.enqueue(
            message_id=_execute_message_id(task.task_id, task.version),
            topic=AGENT_TASK_EXECUTE_TOPIC,
            aggregate_id=task.task_id,
            payload={"task_id": task.task_id, "owner": task.owner},
        )
        return task, created

    def schedule_blueprint_validation(
        self,
        *,
        owner: str,
        session_id: str,
        turn_id: str,
        project_id: str,
        workspace_id: str,
        request_key: str,
        blueprint: ProjectBlueprint,
        envelope: AgentResourceEnvelope,
    ) -> tuple[AgentTaskRecord, bool]:
        """Derive one closed Slurm request from a typed Blueprint and approval."""

        if not isinstance(blueprint, ProjectBlueprint):
            raise TypeError("blueprint must be a ProjectBlueprint")
        validations = [
            validation for validation in blueprint.validations if validation.execution == "slurm"
        ]
        if len(validations) != 1:
            raise ValueError("Blueprint must declare exactly one Slurm validation")
        validation = validations[0]
        hints = blueprint.contract_intent.resource_hints
        partition = _resource_text(hints, "partition", envelope.partition)
        qos = _resource_text(hints, "qos", envelope.qos)
        cpus = _resource_int(hints, "cpus_per_task", envelope.cpus)
        memory_mib = _resource_int(hints, "memory_mib", envelope.memory_mib)
        gpus = _resource_int(hints, "gpus", envelope.gpus, allow_zero=True)
        walltime_seconds = _resource_walltime(hints, envelope.walltime_seconds)
        request = AgentTaskRequest(
            partition=partition,
            qos=qos,
            cpus=cpus,
            memory_mib=memory_mib,
            gpu_type=envelope.gpu_type if gpus else None,
            gpus=gpus,
            walltime_seconds=walltime_seconds,
            tasks=1,
            submissions=1,
            workspace_snapshot_digest=envelope.workspace_snapshot_digest,
            payload={
                "script": shlex.join(validation.argv),
                "job_name": validation.validation_id,
                "expected_outputs": list(validation.expected_outputs),
            },
        )
        return self.schedule_validation(
            owner=owner,
            session_id=session_id,
            turn_id=turn_id,
            project_id=project_id,
            workspace_id=workspace_id,
            request_key=request_key,
            request=request,
            envelope=envelope,
        )

    def request_cancel(self, task_id: str, *, owner: str, expected_version: int) -> AgentTaskRecord:
        task = self.store.request_cancel(
            task_id,
            owner=owner,
            expected_version=expected_version,
        )
        if task.state in TERMINAL_TASK_STATES:
            self._enqueue_ready(task)
        elif task.linked_run_id is not None:
            self.run_service.cancel(task.linked_run_id)
        return self.store.get_task(task_id, owner=owner)

    def resume_after_auth(
        self, task_id: str, *, owner: str, expected_version: int
    ) -> AgentTaskRecord:
        task = self.store.resume_after_auth(
            task_id,
            owner=owner,
            expected_version=expected_version,
        )
        if task.linked_run_id is not None:
            run = self.run_service.store.get_run(task.linked_run_id)
            if run.state is RunState.AUTH_REQUIRED and run.job_id is not None:
                self.run_service.store.update_state(
                    run.run_id,
                    RunState.UNKNOWN,
                    event_type="run.auth_resumed",
                )
        self.control_repository.enqueue(
            message_id=_execute_message_id(task.task_id, task.version),
            topic=AGENT_TASK_EXECUTE_TOPIC,
            aggregate_id=task.task_id,
            payload={"task_id": task.task_id, "owner": task.owner},
        )
        return task

    def build_tool_handler(self, envelope_resolver: EnvelopeResolver) -> AgentReadHandler:
        def schedule(owner: str, arguments: Mapping[str, object]) -> AgentReadResult:
            try:
                allowed = {
                    "project_id",
                    "workspace_id",
                    "session_id",
                    "turn_id",
                    "request_key",
                    "cpus",
                    "memory_mib",
                    "gpus",
                    "walltime_seconds",
                    "tasks",
                    "submissions",
                    "script",
                    "job_name",
                }
                if set(arguments) != allowed:
                    raise _tool_error("validation request fields are invalid")
                session_id = _tool_string(arguments, "session_id")
                envelope = envelope_resolver(owner, session_id)
                request = AgentTaskRequest(
                    partition=envelope.partition,
                    qos=envelope.qos,
                    cpus=_tool_int(arguments, "cpus"),
                    memory_mib=_tool_int(arguments, "memory_mib"),
                    gpu_type=envelope.gpu_type if _tool_int(arguments, "gpus") else None,
                    gpus=_tool_int(arguments, "gpus"),
                    walltime_seconds=_tool_int(arguments, "walltime_seconds"),
                    tasks=_tool_int(arguments, "tasks"),
                    submissions=_tool_int(arguments, "submissions"),
                    workspace_snapshot_digest=envelope.workspace_snapshot_digest,
                    payload={
                        "script": _tool_string(arguments, "script", maximum=262_144),
                        "job_name": _tool_string(arguments, "job_name"),
                    },
                )
                task, _ = self.schedule_validation(
                    owner=owner,
                    session_id=session_id,
                    turn_id=_tool_string(arguments, "turn_id"),
                    project_id=_tool_string(arguments, "project_id"),
                    workspace_id=_tool_string(arguments, "workspace_id"),
                    request_key=_tool_string(arguments, "request_key"),
                    request=request,
                    envelope=envelope,
                )
            except AgentToolGatewayError:
                raise
            except ResourceEnvelopeExceeded:
                raise AgentToolGatewayError(
                    "Validation request exceeds its approved resource envelope",
                    code="AGENT.TOOL.RESOURCE_ENVELOPE_EXCEEDED",
                ) from None
            except (TypeError, ValueError):
                raise _tool_error("validation request is invalid") from None
            return AgentReadResult(
                result={
                    "task_id": task.task_id,
                    "state": task.state.value,
                    "linked_run_id": task.linked_run_id,
                    "terminate": True,
                },
                evidence_refs=(f"agent-task:{task.task_id}",),
            )

        return schedule

    def dispatch_due(self, *, limit: int) -> AgentTaskDispatchBatch:
        if not 1 <= limit <= 1000:
            raise ValueError("limit is invalid")
        checked = 0
        succeeded = 0
        errors: list[AgentTaskDispatchError] = []
        while checked < limit:
            messages = self.control_repository.claim_outbox(
                owner=self.worker_id,
                limit=1,
                lease_seconds=self.lease_seconds,
                topics=(AGENT_TASK_EXECUTE_TOPIC, AGENT_TASK_READY_TOPIC),
            )
            if not messages:
                break
            message = messages[0]
            checked += 1
            try:
                if message.topic == AGENT_TASK_READY_TOPIC:
                    processed = self._dispatch_ready(message)
                else:
                    processed = self._dispatch_execute(message)
            except Exception as exc:
                error = _dispatch_error(message.aggregate_id, exc)
                self._retry(message, error.message)
                errors.append(error)
            else:
                succeeded += int(processed)
        return AgentTaskDispatchBatch(checked=checked, succeeded=succeeded, errors=errors)

    def reconcile_active(self, *, limit: int) -> AgentTaskDispatchBatch:
        if not 1 <= limit <= 1000:
            raise ValueError("limit is invalid")
        checked = 0
        succeeded = 0
        errors: list[AgentTaskDispatchError] = []
        for candidate in self.store.list_recoverable_tasks(limit=limit):
            if candidate.state is not AgentTaskState.RUNNING:
                continue
            checked += 1
            lease = self.store.claim_task(
                candidate.task_id,
                owner=candidate.owner,
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
            )
            if lease is None:
                continue
            try:
                if self._reconcile_one(candidate.task_id, lease):
                    succeeded += 1
            except Exception:
                with suppress(Exception):
                    self.store.release_task(lease)
                errors.append(
                    AgentTaskDispatchError(
                        task_id=candidate.task_id,
                        message="AgentTask reconciliation failed",
                    )
                )
        return AgentTaskDispatchBatch(checked=checked, succeeded=succeeded, errors=errors)

    def _dispatch_execute(self, message: OutboxMessage) -> bool:
        task_id, owner = _message_identity(message, AGENT_TASK_EXECUTE_TOPIC)
        current = self.store.get_task(task_id, owner=owner)
        if current.state in TERMINAL_TASK_STATES or current.state is AgentTaskState.AUTH_REQUIRED:
            self._acknowledge(message)
            return True
        lease = self.store.claim_task(
            task_id,
            owner=owner,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        if lease is None:
            self._retry(message, "AgentTask is not currently claimable")
            return False
        try:
            task = self.store.get_task(task_id, owner=owner)
            if task.cancel_requested:
                self._persist_terminal_and_enqueue(
                    task_id=task.task_id,
                    owner=task.owner,
                    persist=lambda: self.store.complete_task(
                        task_id,
                        lease=lease,
                        result=AgentTaskResult.cancelled("cancelled before Run creation"),
                    ),
                )
                self._acknowledge(message)
                return True
            run_request = self._run_request(task)
            run_id = _run_id(task.task_id)
            self.run_service.prepare(run_request, run_id=run_id, idempotent=True)
            self.store.link_run(task_id, lease=lease, run_id=run_id)
            self.run_service.enqueue_submission(run_id)
            scheduled = self.store.advance_gate(
                task_id,
                lease=lease,
                gate_state=AgentTaskGateState.PENDING,
                receipt=_schedule_receipt(task, run_id),
                completion_policy=task.completion_policy,
            )
            self.store.release_task(replace(lease, version=scheduled.version))
            self._acknowledge(message)
            return True
        except AgentTaskProvenanceError as exc:
            try:
                self._finalize_without_gate(
                    task=task,
                    lease=lease,
                    result=AgentTaskResult(
                        status="failed",
                        evidence_refs=(),
                        error_code=exc.code,
                        message=str(exc),
                    ),
                    gate_state=AgentTaskGateState.BLOCKED,
                )
                self._acknowledge(message)
                return True
            except Exception:
                with suppress(Exception):
                    self.store.release_task(lease)
                raise
        except Exception:
            with suppress(Exception):
                self.store.release_task(lease)
            raise

    def _dispatch_ready(self, message: OutboxMessage) -> bool:
        task_id, owner = _message_identity(message, AGENT_TASK_READY_TOPIC)
        task = self.store.get_task(task_id, owner=owner)
        message_version = message.payload.get("version")
        if (
            isinstance(message_version, bool)
            or not isinstance(message_version, int)
            or message_version < 0
        ):
            raise ValueError("AgentTask ready message version is invalid")
        if task.version < message_version:
            self._retry(message, "AgentTask terminal transition is not committed")
            return False
        if task.version > message_version:
            self._acknowledge(message)
            return True
        if (
            task.state not in TERMINAL_TASK_STATES
            and task.state is not AgentTaskState.AUTH_REQUIRED
        ):
            raise RuntimeError("AgentTask is not ready to wake its Session")
        if task.state is AgentTaskState.SUCCEEDED and (
            task.gate_state is not AgentTaskGateState.COMPLETED
            or task.gate_receipt is None
            or task.legacy_gate_unverified
        ):
            raise RuntimeError("AgentTask successful result has no verified Evidence gate")
        session = self.session_store.get_session(task.session_id, owner=owner)
        self.session_service.submit_message(
            session_id=task.session_id,
            owner=owner,
            request_key=_ready_request_key(task),
            message=_followup_message(task),
            expected_state_version=session.state_version,
        )
        self._acknowledge(message)
        return True

    def _reconcile_one(self, task_id: str, lease: AgentTaskLease) -> bool:
        task = self.store.get_task(task_id, owner=lease.owner)
        if task.linked_run_id is None:
            self.store.release_task(lease)
            return False
        run = self.run_service.store.get_run(task.linked_run_id)
        if run.owner != task.owner:
            raise RuntimeError("AgentTask Run owner binding is invalid")
        if task.cancel_requested and run.state not in TERMINAL_RUN_STATES:
            run = self.run_service.cancel(run.run_id)
        if run.state not in TERMINAL_RUN_STATES:
            waiting = self.store.advance_gate(
                task_id,
                lease=lease,
                gate_state=AgentTaskGateState.AWAITING_RUN_TERMINAL,
            )
            self.store.release_task(replace(lease, version=waiting.version))
            return False
        if run.state is RunState.AUTH_REQUIRED:
            return self._finalize_without_gate(
                task=task,
                lease=lease,
                result=_result_for_run(run.state, run.run_id),
                gate_state=AgentTaskGateState.INPUT_REQUIRED,
            )
        if run.state is not RunState.SUCCEEDED:
            if run.state is RunState.CANCELLED:
                return self._finalize_without_gate(
                    task=task,
                    lease=lease,
                    result=_result_for_run(run.state, run.run_id),
                    gate_state=AgentTaskGateState.CANCELLED,
                )
            if run.state is RunState.COLLECTION_FAILED or run.collection_state in {
                CollectionState.FAILED,
                CollectionState.DEGRADED,
            }:
                return self._finalize_without_gate(
                    task=task,
                    lease=lease,
                    result=AgentTaskResult(
                        status="failed",
                        evidence_refs=self._terminal_evidence_refs(run.run_id),
                        error_code="EVIDENCE.UNAVAILABLE",
                        message="Evidence 收集已达到重试上限，任务无法完成。",
                    ),
                    gate_state=AgentTaskGateState.FAILED,
                )
            if run.state is RunState.FAILED and run.collection_state in {
                CollectionState.PENDING,
                CollectionState.RUNNING,
            }:
                waiting = self.store.advance_gate(
                    task_id,
                    lease=lease,
                    gate_state=AgentTaskGateState.AWAITING_EVIDENCE,
                )
                self.store.release_task(replace(lease, version=waiting.version))
                return False
            refs = self._terminal_evidence_refs(run.run_id)
            return self._finalize_without_gate(
                task=task,
                lease=lease,
                result=_result_for_run(run.state, run.run_id, evidence_refs=refs),
                gate_state=(
                    AgentTaskGateState.ORPHANED
                    if run.state is RunState.ORPHANED
                    else AgentTaskGateState.FAILED
                ),
            )
        if run.collection_state in {CollectionState.PENDING, CollectionState.RUNNING}:
            waiting = self.store.advance_gate(
                task_id,
                lease=lease,
                gate_state=AgentTaskGateState.AWAITING_EVIDENCE,
            )
            self.store.release_task(replace(lease, version=waiting.version))
            return False
        if run.collection_state in {CollectionState.FAILED, CollectionState.DEGRADED}:
            return self._finalize_without_gate(
                task=task,
                lease=lease,
                result=AgentTaskResult(
                    status="failed",
                    evidence_refs=self._terminal_evidence_refs(run.run_id),
                    error_code="EVIDENCE.UNAVAILABLE",
                    message="Evidence 收集已达到重试上限，任务无法完成。",
                ),
                gate_state=AgentTaskGateState.FAILED,
            )
        if self.evidence_binder is None:
            return self._finalize_without_gate(
                task=task,
                lease=lease,
                result=AgentTaskResult(
                    status="failed",
                    evidence_refs=(),
                    error_code="EVIDENCE.AUTHORITY_UNAVAILABLE",
                    message="Evidence 权威校验服务不可用，任务已安全终止。",
                ),
                gate_state=AgentTaskGateState.BLOCKED,
            )
        refs = self._terminal_evidence_refs(run.run_id)
        try:
            receipt = self.evidence_binder.verify_terminal_gate(
                run.run_id,
                refs,
                {
                    "workspace_revision": run.workspace_revision,
                    "workspace_digest": run.workspace_digest,
                    "legacy_boundary": run.workspace_revision is None,
                    "source_revision": run.source_revision,
                    "platform_snapshot_ref": run.platform_snapshot_ref,
                },
                task_id=task.task_id,
            )
        except EvidenceBindingError:
            return self._finalize_without_gate(
                task=task,
                lease=lease,
                result=AgentTaskResult(
                    status="failed",
                    evidence_refs=refs,
                    error_code="EVIDENCE.INTEGRITY_FAILED",
                    message="Evidence 完整性校验失败，任务已阻止完成。",
                ),
                gate_state=AgentTaskGateState.BLOCKED,
            )
        except Exception:
            return self._finalize_without_gate(
                task=task,
                lease=lease,
                result=AgentTaskResult(
                    status="failed",
                    evidence_refs=refs,
                    error_code="EVIDENCE.AUTHORITY_UNAVAILABLE",
                    message="Evidence 权威校验服务不可用，任务已安全终止。",
                ),
                gate_state=AgentTaskGateState.BLOCKED,
            )
        if task.completion_policy.requires_capsule:
            if self.capsule_authority_resolver is None:
                return self._finalize_without_gate(
                    task=task,
                    lease=lease,
                    result=AgentTaskResult(
                        status="failed",
                        evidence_refs=refs,
                        error_code="CAPSULE.AUTHORITY_UNAVAILABLE",
                        message="Capsule 权威校验服务不可用，任务已安全终止。",
                    ),
                    gate_state=AgentTaskGateState.BLOCKED,
                )
            if run.capsule_state in {CapsuleState.PENDING, CapsuleState.RUNNING}:
                waiting = self.store.advance_gate(
                    task_id,
                    lease=lease,
                    gate_state=AgentTaskGateState.AWAITING_CAPSULE,
                )
                self.store.release_task(replace(lease, version=waiting.version))
                return False
            if run.capsule_state is not CapsuleState.READY:
                return self._finalize_without_gate(
                    task=task,
                    lease=lease,
                    result=AgentTaskResult(
                        status="failed",
                        evidence_refs=refs,
                        error_code="CAPSULE.UNAVAILABLE",
                        message="Capsule 构建失败，任务已安全终止。",
                    ),
                    gate_state=AgentTaskGateState.FAILED,
                )
            try:
                capsule_ref = self.capsule_authority_resolver(run.run_id)
                receipt = replace(
                    receipt,
                    capsule_ref=_required_provenance_text(capsule_ref, "capsule_ref"),
                    capsule_state="READY",
                )
            except Exception:
                return self._finalize_without_gate(
                    task=task,
                    lease=lease,
                    result=AgentTaskResult(
                        status="failed",
                        evidence_refs=refs,
                        error_code="CAPSULE.INTEGRITY_FAILED",
                        message="Capsule 完整性校验失败，任务已阻止完成。",
                    ),
                    gate_state=AgentTaskGateState.BLOCKED,
                )
            gate_state = AgentTaskGateState.AWAITING_CAPSULE
        else:
            gate_state = AgentTaskGateState.AWAITING_INTEGRITY
        gated = self.store.advance_gate(
            task_id,
            lease=lease,
            gate_state=gate_state,
            receipt=receipt,
        )
        final_lease = replace(lease, version=gated.version)
        completed = self._persist_terminal_and_enqueue(
            task_id=task.task_id,
            owner=task.owner,
            persist=lambda: self.store.finalize_task(
                task_id,
                lease=final_lease,
                gate_receipt=receipt,
                result=AgentTaskResult.succeeded(receipt.evidence_refs),
            ),
        )
        return completed.state is AgentTaskState.SUCCEEDED

    def _terminal_evidence_refs(self, run_id: str) -> tuple[str, ...]:
        return tuple(
            f"evidence://runs/{run_id}/{item.logical_path}"
            for item in self.run_service.store.list_evidence_objects(run_id)
        )

    def _finalize_without_gate(
        self,
        *,
        task: AgentTaskRecord,
        lease: AgentTaskLease,
        result: AgentTaskResult,
        gate_state: AgentTaskGateState,
    ) -> bool:
        current_lease = lease
        if gate_state is not AgentTaskGateState.INPUT_REQUIRED:
            advanced = self.store.advance_gate(
                task.task_id,
                lease=current_lease,
                gate_state=gate_state,
            )
            current_lease = replace(current_lease, version=advanced.version)
        completed = self._persist_terminal_and_enqueue(
            task_id=task.task_id,
            owner=task.owner,
            persist=lambda: self.store.complete_task(
                task.task_id,
                lease=current_lease,
                result=result,
            ),
        )
        return (
            completed.state in TERMINAL_TASK_STATES
            or completed.state is AgentTaskState.AUTH_REQUIRED
        )

    def _persist_terminal_and_enqueue(
        self,
        *,
        task_id: str,
        owner: str,
        persist: Callable[[], AgentTaskRecord],
    ) -> AgentTaskRecord:
        try:
            completed = persist()
        except Exception:
            with suppress(Exception):
                recovered = self.store.get_task(task_id, owner=owner)
                if (
                    recovered.state in TERMINAL_TASK_STATES
                    or recovered.state is AgentTaskState.AUTH_REQUIRED
                ):
                    self._enqueue_ready(recovered)
            raise
        self._enqueue_ready(completed)
        return completed

    def _run_request(self, task: AgentTaskRecord) -> RunSubmitRequest:
        request = task.request
        workspace = self.workspace_resolver(
            task.owner,
            task.workspace_id,
            request.workspace_snapshot_digest,
        ).resolve()
        if not workspace.is_dir():
            raise ValueError("AgentTask Workspace is unavailable")
        if "source_revision" in request.payload or "platform_snapshot_ref" in request.payload:
            raise AgentTaskProvenanceError(
                "请求包含不可信的 provenance 字段。",
                code="AGENT.TASK.PROVENANCE_PAYLOAD_FORBIDDEN",
            )
        if self.provenance_authority_resolver is None:
            raise AgentTaskProvenanceError(
                "AgentTask 权威来源不可用，未创建 Run。",
                code="AGENT.TASK.PROVENANCE_AUTHORITY_UNAVAILABLE",
            )
        try:
            authority_values = self.provenance_authority_resolver(
                task.owner,
                task.workspace_id,
                request.workspace_snapshot_digest,
            )
        except AgentTaskProvenanceError:
            raise
        except Exception as exc:
            raise AgentTaskProvenanceError(
                "AgentTask 权威来源不可用，未创建 Run。",
                code="AGENT.TASK.PROVENANCE_AUTHORITY_UNAVAILABLE",
            ) from exc
        if not isinstance(authority_values, tuple) or len(authority_values) != 2:
            raise AgentTaskProvenanceError(
                "AgentTask 权威来源返回了无效事实，未创建 Run。",
                code="AGENT.TASK.PROVENANCE_AUTHORITY_INVALID",
            )
        source_revision, platform_snapshot_ref = authority_values
        try:
            source_revision = _required_provenance_text(source_revision, "source_revision")
            platform_snapshot_ref = _required_provenance_text(
                platform_snapshot_ref,
                "platform_snapshot_ref",
            )
        except (TypeError, ValueError) as exc:
            raise AgentTaskProvenanceError(
                "AgentTask 权威来源返回了无效事实，未创建 Run。",
                code="AGENT.TASK.PROVENANCE_AUTHORITY_INVALID",
            ) from exc
        script = request.payload.get("script")
        if not isinstance(script, str) or not script or len(script.encode()) > 262_144:
            raise ValueError("AgentTask validation script is invalid")
        job_name = request.payload.get("job_name", "agent-validation")
        if not isinstance(job_name, str) or not job_name or len(job_name) > 128:
            raise ValueError("AgentTask validation job name is invalid")
        workdir = (
            self.run_workdir_resolver(task.owner)
            if self.run_workdir_resolver is not None
            else workspace
        )
        return RunSubmitRequest(
            owner=task.owner,
            workdir=workdir,
            script=_materialize_snapshot_script(workspace, script),
            job_name=job_name,
            resource_plan=ResourcePlan(
                partition=request.partition,
                qos=request.qos,
                nodes=1,
                ntasks=1,
                cpus_per_task=request.cpus,
                memory_value=request.memory_mib,
                memory_unit="M",
                gpus_total=request.gpus,
                gpu_type=request.gpu_type,
                time_limit=_slurm_time(request.walltime_seconds),
            ),
            workflow=WorkflowPolicy(
                automation_level="bounded_auto",
                require_approval=False,
            ),
            # Phase 1 only permits the immutable legacy snapshot boundary.  A
            # live workspace revision must come from the Phase 3 authority
            # resolver and is deliberately not accepted from task payloads.
            workspace_revision=None,
            workspace_digest=request.workspace_snapshot_digest,
            source_revision=source_revision,
            platform_snapshot_ref=platform_snapshot_ref,
        )

    def _enqueue_ready(self, task: AgentTaskRecord) -> None:
        self._enqueue_ready_intent(
            task_id=task.task_id,
            owner=task.owner,
            version=task.version,
        )

    def _enqueue_ready_intent(self, *, task_id: str, owner: str, version: int) -> None:
        self.control_repository.enqueue(
            message_id=_ready_message_id(task_id, version),
            topic=AGENT_TASK_READY_TOPIC,
            aggregate_id=task_id,
            payload={
                "task_id": task_id,
                "owner": owner,
                "version": version,
            },
        )

    def _acknowledge(self, message: OutboxMessage) -> None:
        if message.lease_owner != self.worker_id:
            raise RuntimeError("AgentTask outbox lease ownership is invalid")
        self.control_repository.acknowledge(
            message_id=message.message_id,
            owner=self.worker_id,
            fencing_token=message.fencing_token,
        )

    def _retry(self, message: OutboxMessage, error: str) -> None:
        if message.lease_owner != self.worker_id:
            raise RuntimeError("AgentTask outbox lease ownership is invalid")
        self.control_repository.retry(
            message_id=message.message_id,
            owner=self.worker_id,
            fencing_token=message.fencing_token,
            error=error,
            delay_seconds=1,
            max_attempts=self.max_attempts,
        )


def _message_identity(message: OutboxMessage, topic: str) -> tuple[str, str]:
    task_id = message.payload.get("task_id")
    owner = message.payload.get("owner")
    if (
        message.topic != topic
        or message.aggregate_id != task_id
        or not isinstance(task_id, str)
        or not isinstance(owner, str)
        or not task_id
        or not owner
    ):
        raise ValueError("AgentTask outbox identity is invalid")
    return task_id, owner


def _dispatch_error(task_id: str, error: Exception) -> AgentTaskDispatchError:
    if isinstance(error, AgentTaskProvenanceError):
        return AgentTaskDispatchError(
            task_id=task_id,
            message=str(error),
            retryable=True,
            code=error.code,
        )
    return AgentTaskDispatchError(
        task_id=task_id,
        message="AgentTask 调度失败。",
    )


def _result_for_run(
    state: RunState,
    run_id: str,
    *,
    evidence_refs: tuple[str, ...] | None = None,
) -> AgentTaskResult:
    refs = evidence_refs if evidence_refs is not None else (f"run:{run_id}",)
    if state is RunState.SUCCEEDED:
        return AgentTaskResult.succeeded(refs)
    if state is RunState.CANCELLED:
        return AgentTaskResult(
            status="cancelled",
            evidence_refs=refs,
            error_code=None,
            message="validation Run was cancelled",
        )
    if state is RunState.AUTH_REQUIRED:
        return AgentTaskResult(
            status="auth_required",
            evidence_refs=refs,
            error_code="AUTH.REQUIRED",
            message="cluster authentication is required",
        )
    return AgentTaskResult(
        status="failed",
        evidence_refs=refs,
        error_code="VALIDATION.RUN_FAILED",
        message=f"validation Run ended in {state.value}",
    )


def _schedule_receipt(task: AgentTaskRecord, run_id: str) -> AgentTaskScheduleReceipt:
    request_digest = _canonical_digest(
        {
            "partition": task.request.partition,
            "qos": task.request.qos,
            "cpus": task.request.cpus,
            "memory_mib": task.request.memory_mib,
            "gpu_type": task.request.gpu_type,
            "gpus": task.request.gpus,
            "walltime_seconds": task.request.walltime_seconds,
            "tasks": task.request.tasks,
            "submissions": task.request.submissions,
            "workspace_snapshot_digest": task.request.workspace_snapshot_digest,
            "payload": task.request.payload,
        }
    )
    envelope_digest = _canonical_digest(
        {
            "partition": task.resource_envelope.partition,
            "qos": task.resource_envelope.qos,
            "cpus": task.resource_envelope.cpus,
            "memory_mib": task.resource_envelope.memory_mib,
            "gpu_type": task.resource_envelope.gpu_type,
            "gpus": task.resource_envelope.gpus,
            "walltime_seconds": task.resource_envelope.walltime_seconds,
            "max_tasks": task.resource_envelope.max_tasks,
            "max_submissions": task.resource_envelope.max_submissions,
            "workspace_snapshot_digest": (task.resource_envelope.workspace_snapshot_digest),
            "expires_at": task.resource_envelope.expires_at,
            "approved_by": task.resource_envelope.approved_by,
        }
    )
    return AgentTaskScheduleReceipt(
        receipt_id=f"receipt-{hashlib.sha256(f'{task.task_id}:{run_id}'.encode()).hexdigest()[:40]}",
        task_id=task.task_id,
        owner=task.owner,
        session_id=task.session_id,
        originating_turn_id=task.turn_id,
        request_digest=request_digest,
        idempotency_key=task.request_key,
        run_id=run_id,
        submit_state="pending",
        resource_envelope_id=envelope_digest,
        workspace_revision=None,
        workspace_digest=task.request.workspace_snapshot_digest,
        completion_policy=task.completion_policy,
        created_at=task.updated_at,
        legacy_boundary=True,
    )


def _canonical_digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _followup_message(task: AgentTaskRecord) -> str:
    result = task.result
    status = result.status if result is not None else task.state.value
    refs = ", ".join(result.evidence_refs if result is not None else ()) or "none"
    return (
        f"Validation task {task.task_id} is {status}. Review persisted Evidence references: {refs}."
    )


def _run_id(task_id: str) -> str:
    digest = hashlib.sha256(task_id.encode()).hexdigest()
    return f"run_agent_{digest[:40]}"


def _execute_message_id(task_id: str, version: int) -> str:
    return f"agent-task:{task_id}:execute:{version}"


def _ready_message_id(task_id: str, version: int) -> str:
    return f"agent-task:{task_id}:ready:{version}"


def _ready_request_key(task: AgentTaskRecord) -> str:
    if task.state is AgentTaskState.AUTH_REQUIRED:
        return f"agent-task:{task.task_id}:auth:{task.version}"
    return f"agent-task:{task.task_id}:ready"


def _slurm_time(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"


def _materialize_snapshot_script(workspace: Path, validation_script: str) -> str:
    """Build a bounded, self-contained script for a remote compute node."""

    files: list[tuple[Path, bytes, int]] = []
    total_bytes = 0
    for directory, directory_names, file_names in os.walk(workspace, followlinks=False):
        directory_path = Path(directory)
        for name in sorted(directory_names):
            if (directory_path / name).is_symlink():
                raise ValueError("AgentTask Workspace cannot contain symbolic links")
        for name in sorted(file_names):
            path = directory_path / name
            if path.is_symlink():
                raise ValueError("AgentTask Workspace cannot contain symbolic links")
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("AgentTask Workspace can contain regular files only")
            content = path.read_bytes()
            total_bytes += len(content)
            if len(files) >= _MAX_SNAPSHOT_FILES or total_bytes > _MAX_SNAPSHOT_BYTES:
                raise ValueError("AgentTask Workspace snapshot exceeds materialization limits")
            files.append((path.relative_to(workspace), content, stat.S_IMODE(metadata.st_mode)))

    lines = [
        "#!/bin/bash",
        "set -euo pipefail",
        'pilot107_workspace="$(mktemp -d "${SLURM_TMPDIR:-/tmp}/pilot107-agent-task-XXXXXX")"',
        "trap 'rm -rf -- \"$pilot107_workspace\"' EXIT",
    ]
    for relative_path, content, mode in files:
        quoted_path = shlex.quote(relative_path.as_posix())
        quoted_parent = shlex.quote(relative_path.parent.as_posix())
        encoded = base64.b64encode(content).decode("ascii")
        lines.extend(
            (
                f'mkdir -p "$pilot107_workspace"/{quoted_parent}',
                f"printf '%s' {shlex.quote(encoded)} | base64 --decode > "
                f'"$pilot107_workspace"/{quoted_path}',
                f'chmod {mode:o} "$pilot107_workspace"/{quoted_path}',
            )
        )
    lines.extend(('cd "$pilot107_workspace"', _without_shebang(validation_script)))
    return "\n".join(lines).rstrip() + "\n"


def _without_shebang(script: str) -> str:
    if not script.startswith("#!"):
        return script
    _, separator, remainder = script.partition("\n")
    return remainder if separator else ""


def _resource_text(hints: Mapping[str, str | int], key: str, default: str) -> str:
    value = hints.get(key, default)
    if not isinstance(value, str):
        raise TypeError(f"Blueprint resource hint {key} must be text")
    return value


def _resource_int(
    hints: Mapping[str, str | int],
    key: str,
    default: int,
    *,
    allow_zero: bool = False,
) -> int:
    value = hints.get(key, default)
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"Blueprint resource hint {key} is invalid")
    return value


def _resource_walltime(hints: Mapping[str, str | int], default_seconds: int) -> int:
    value = hints.get("time_limit")
    if value is None:
        return default_seconds
    if not isinstance(value, str):
        raise TypeError("Blueprint time_limit must be text")
    day_text, separator, clock_text = value.partition("-")
    if separator:
        if not day_text.isdigit():
            raise ValueError("Blueprint time_limit is invalid")
        days = int(day_text)
    else:
        days = 0
        clock_text = day_text
    parts = clock_text.split(":")
    if not all(part.isdigit() for part in parts):
        raise ValueError("Blueprint time_limit is invalid")
    numbers = [int(part) for part in parts]
    if len(numbers) == 1:
        seconds = numbers[0] * 60
    elif len(numbers) == 2 and numbers[1] < 60:
        seconds = numbers[0] * 60 + numbers[1]
    elif len(numbers) == 3 and numbers[1] < 60 and numbers[2] < 60:
        seconds = numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    else:
        raise ValueError("Blueprint time_limit is invalid")
    if separator and numbers[0] >= 24:
        raise ValueError("Blueprint time_limit is invalid")
    total = days * 86_400 + seconds
    if not 1 <= total <= 31_536_000:
        raise ValueError("Blueprint time_limit is invalid")
    return total


def _tool_string(arguments: Mapping[str, object], key: str, *, maximum: int = 128) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value or len(value) > maximum or "\0" in value:
        raise _tool_error("validation request is invalid")
    return value


def _tool_int(arguments: Mapping[str, object], key: str) -> int:
    value = arguments.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _tool_error("validation resources are invalid")
    return value


def _tool_error(message: str) -> AgentToolGatewayError:
    return AgentToolGatewayError(message, code="AGENT.TOOL.INVALID")


def _optional_provenance_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError(f"{name} is invalid")
    return value


def _required_provenance_text(value: object, name: str) -> str:
    normalized = _optional_provenance_text(value, name)
    if normalized is None:
        raise ValueError(f"{name} is required from the provenance authority")
    return normalized
