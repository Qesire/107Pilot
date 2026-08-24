"""Durable scheduling and reconciliation for bounded Slurm validation tasks."""

from __future__ import annotations

import base64
import hashlib
import os
import shlex
import stat
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from pilot107.agent.project import is_project_agent_profile
from pilot107.agent.store import AgentSessionStore
from pilot107.agent.task_store import AgentTaskStore
from pilot107.agent.tasks import (
    TERMINAL_TASK_STATES,
    AgentResourceEnvelope,
    AgentTaskLease,
    AgentTaskRecord,
    AgentTaskRequest,
    AgentTaskResult,
    AgentTaskState,
    ResourceEnvelopeExceeded,
)
from pilot107.agent.tool_gateway import AgentReadHandler, AgentReadResult, AgentToolGatewayError
from pilot107.core.control_repository import ControlRepository, OutboxMessage
from pilot107.core.resources import ResourcePlan
from pilot107.core.run_service import RunService, RunSubmitRequest, WorkflowPolicy
from pilot107.core.states import TERMINAL_RUN_STATES, RunState
from pilot107.services.agent_session_service import AgentSessionService

AGENT_TASK_EXECUTE_TOPIC = "agent.task.execute.v1"
AGENT_TASK_READY_TOPIC = "agent.task.ready.v1"

type WorkspaceResolver = Callable[[str, str, str], Path]
type RunWorkdirResolver = Callable[[str], Path]
type EnvelopeResolver = Callable[[str, str], AgentResourceEnvelope]

_MAX_SNAPSHOT_FILES = 256
_MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class AgentTaskDispatchError:
    task_id: str
    message: str
    retryable: bool = True


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
        )
        self.control_repository.enqueue(
            message_id=_execute_message_id(task.task_id, task.version),
            topic=AGENT_TASK_EXECUTE_TOPIC,
            aggregate_id=task.task_id,
            payload={"task_id": task.task_id, "owner": task.owner},
        )
        return task, created

    def request_cancel(
        self, task_id: str, *, owner: str, expected_version: int
    ) -> AgentTaskRecord:
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
            except Exception:
                self._retry(message, "AgentTask dispatch failed")
                errors.append(
                    AgentTaskDispatchError(
                        task_id=message.aggregate_id,
                        message="AgentTask dispatch failed",
                    )
                )
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
                completed = self.store.complete_task(
                    task_id,
                    lease=lease,
                    result=AgentTaskResult.cancelled("cancelled before Run creation"),
                )
                self._enqueue_ready(completed)
                self._acknowledge(message)
                return True
            run_request = self._run_request(task)
            run_id = _run_id(task.task_id)
            self.run_service.prepare(run_request, run_id=run_id, idempotent=True)
            self.store.link_run(task_id, lease=lease, run_id=run_id)
            self.run_service.enqueue_submission(run_id)
            self.store.release_task(lease)
            self._acknowledge(message)
            return True
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
        if task.version != message_version:
            self._acknowledge(message)
            return True
        if (
            task.state not in TERMINAL_TASK_STATES
            and task.state is not AgentTaskState.AUTH_REQUIRED
        ):
            raise RuntimeError("AgentTask is not ready to wake its Session")
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
            self.store.release_task(lease)
            return False
        result = _result_for_run(run.state, run.run_id)
        completed = self.store.complete_task(task_id, lease=lease, result=result)
        self._enqueue_ready(completed)
        return True

    def _run_request(self, task: AgentTaskRecord) -> RunSubmitRequest:
        request = task.request
        workspace = self.workspace_resolver(
            task.owner,
            task.workspace_id,
            request.workspace_snapshot_digest,
        ).resolve()
        if not workspace.is_dir():
            raise ValueError("AgentTask Workspace is unavailable")
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
        )

    def _enqueue_ready(self, task: AgentTaskRecord) -> None:
        self.control_repository.enqueue(
            message_id=_ready_message_id(task.task_id, task.version),
            topic=AGENT_TASK_READY_TOPIC,
            aggregate_id=task.task_id,
            payload={
                "task_id": task.task_id,
                "owner": task.owner,
                "version": task.version,
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


def _result_for_run(state: RunState, run_id: str) -> AgentTaskResult:
    evidence_refs = (f"run:{run_id}",)
    if state is RunState.SUCCEEDED:
        return AgentTaskResult.succeeded(evidence_refs)
    if state is RunState.CANCELLED:
        return AgentTaskResult(
            status="cancelled",
            evidence_refs=evidence_refs,
            error_code=None,
            message="validation Run was cancelled",
        )
    if state is RunState.AUTH_REQUIRED:
        return AgentTaskResult(
            status="auth_required",
            evidence_refs=evidence_refs,
            error_code="AUTH.REQUIRED",
            message="cluster authentication is required",
        )
    return AgentTaskResult(
        status="failed",
        evidence_refs=evidence_refs,
        error_code="VALIDATION.RUN_FAILED",
        message=f"validation Run ended in {state.value}",
    )


def _followup_message(task: AgentTaskRecord) -> str:
    result = task.result
    status = result.status if result is not None else task.state.value
    refs = ", ".join(result.evidence_refs if result is not None else ()) or "none"
    return (
        f"Validation task {task.task_id} is {status}. "
        f"Review persisted Evidence references: {refs}."
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
        'trap \'rm -rf -- "$pilot107_workspace"\' EXIT',
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


def _tool_string(
    arguments: Mapping[str, object], key: str, *, maximum: int = 128
) -> str:
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
