"""Immutable AgentTask records and approved resource-envelope invariants."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

AgentTaskKind = Literal["slurm_validation"]

_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


class AgentTaskConflict(RuntimeError):
    """A task replay, transition, version, or fencing check conflicted."""


class ResourceEnvelopeExceeded(ValueError):
    """A task request is outside its explicitly approved resource envelope."""


class AgentTaskState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    AUTH_REQUIRED = "auth_required"


TERMINAL_TASK_STATES = frozenset(
    {AgentTaskState.SUCCEEDED, AgentTaskState.FAILED, AgentTaskState.CANCELLED}
)


class AgentTaskCompletionPolicy(StrEnum):
    """Explicit evidence gate policy for an AgentTask."""

    EVIDENCE_REQUIRED = "evidence_required"
    EVIDENCE_AND_CAPSULE_REQUIRED = "evidence_and_capsule_required"

    @property
    def requires_capsule(self) -> bool:
        return self is self.EVIDENCE_AND_CAPSULE_REQUIRED


class AgentTaskGateState(StrEnum):
    """Durable gate projection; legacy task state remains wire-compatible."""

    CREATED = "created"
    ADMITTED = "admitted"
    SUBMITTING = "submitting"
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_RUN_TERMINAL = "awaiting_run_terminal"
    AWAITING_EVIDENCE = "awaiting_evidence"
    AWAITING_INTEGRITY = "awaiting_integrity"
    AWAITING_CAPSULE = "awaiting_capsule"
    COMPLETED = "completed"
    INPUT_REQUIRED = "input_required"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    FAILED = "failed"
    BLOCKED = "blocked"
    ORPHANED = "orphaned"


_SCHEDULE_SUBMIT_STATES = frozenset(
    {"admitted", "submitting", "pending", "submitted", "submission_uncertain"}
)
_CAPSULE_STATES = frozenset({"READY", "not_required"})
_RUN_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "orphaned"})


@dataclass(frozen=True)
class AgentResourceEnvelope:
    partition: str
    qos: str
    cpus: int
    memory_mib: int
    gpu_type: str | None
    gpus: int
    walltime_seconds: int
    max_tasks: int
    max_submissions: int
    workspace_snapshot_digest: str
    expires_at: str
    approved_by: str

    def __post_init__(self) -> None:
        _bounded_text(self.partition, "partition")
        _bounded_text(self.qos, "qos")
        _positive_int(self.cpus, "cpus", maximum=1_048_576)
        _positive_int(self.memory_mib, "memory_mib", maximum=1_125_899_906_842_624)
        if self.gpu_type is not None:
            _bounded_text(self.gpu_type, "gpu_type")
        _non_negative_int(self.gpus, "gpus", maximum=1_048_576)
        if self.gpus and self.gpu_type is None:
            raise ValueError("gpu_type is required when gpus are approved")
        _positive_int(self.walltime_seconds, "walltime_seconds", maximum=31_536_000)
        _positive_int(self.max_tasks, "max_tasks", maximum=1024)
        _positive_int(self.max_submissions, "max_submissions", maximum=1024)
        _digest(self.workspace_snapshot_digest, "workspace_snapshot_digest")
        object.__setattr__(
            self,
            "expires_at",
            timestamp(parse_timestamp(self.expires_at, "expires_at")),
        )
        _identifier(self.approved_by, "approved_by")

    def assert_allows(
        self,
        request: AgentTaskRequest,
        *,
        owner: str,
        now: datetime,
    ) -> None:
        if self.approved_by != owner:
            raise ResourceEnvelopeExceeded("resource envelope approver does not match owner")
        if parse_timestamp(self.expires_at, "expires_at") <= _aware_utc(now):
            raise ResourceEnvelopeExceeded("resource envelope has expired")
        exact = (
            (request.partition, self.partition, "partition"),
            (request.qos, self.qos, "qos"),
            (
                request.workspace_snapshot_digest,
                self.workspace_snapshot_digest,
                "workspace snapshot",
            ),
        )
        for requested_text, approved_text, label in exact:
            if requested_text != approved_text:
                raise ResourceEnvelopeExceeded(f"requested {label} is not approved")
        if request.gpus and request.gpu_type != self.gpu_type:
            raise ResourceEnvelopeExceeded("requested GPU type is not approved")
        limits = (
            (request.cpus, self.cpus, "CPU"),
            (request.memory_mib, self.memory_mib, "memory"),
            (request.gpus, self.gpus, "GPU"),
            (request.walltime_seconds, self.walltime_seconds, "walltime"),
            (request.tasks, self.max_tasks, "task count"),
            (request.submissions, self.max_submissions, "submission count"),
        )
        for requested_count, approved_count, label in limits:
            if requested_count > approved_count:
                raise ResourceEnvelopeExceeded(f"requested {label} exceeds approval")


@dataclass(frozen=True)
class AgentTaskRequest:
    partition: str
    qos: str
    cpus: int
    memory_mib: int
    gpu_type: str | None
    gpus: int
    walltime_seconds: int
    tasks: int
    submissions: int
    workspace_snapshot_digest: str
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        _bounded_text(self.partition, "partition")
        _bounded_text(self.qos, "qos")
        _positive_int(self.cpus, "cpus", maximum=1_048_576)
        _positive_int(self.memory_mib, "memory_mib", maximum=1_125_899_906_842_624)
        if self.gpu_type is not None:
            _bounded_text(self.gpu_type, "gpu_type")
        _non_negative_int(self.gpus, "gpus", maximum=1_048_576)
        if self.gpus and self.gpu_type is None:
            raise ValueError("gpu_type is required when gpus are requested")
        _positive_int(self.walltime_seconds, "walltime_seconds", maximum=31_536_000)
        _positive_int(self.tasks, "tasks", maximum=1024)
        _positive_int(self.submissions, "submissions", maximum=1024)
        _digest(self.workspace_snapshot_digest, "workspace_snapshot_digest")
        if not isinstance(self.payload, dict):
            raise TypeError("AgentTask request payload must be an object")
        try:
            canonical = json.dumps(
                self.payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("AgentTask request payload must be finite JSON") from exc
        if len(canonical.encode("utf-8")) > 1_048_576:
            raise ValueError("AgentTask request payload exceeds 1 MiB")
        object.__setattr__(self, "payload", json.loads(canonical))


@dataclass(frozen=True)
class AgentTaskResult:
    status: Literal["succeeded", "failed", "cancelled", "auth_required"]
    evidence_refs: tuple[str, ...]
    error_code: str | None
    message: str | None

    def __post_init__(self) -> None:
        if self.status not in {"succeeded", "failed", "cancelled", "auth_required"}:
            raise ValueError("AgentTask result status is invalid")
        refs = tuple(self.evidence_refs)
        if len(refs) > 4096:
            raise ValueError("AgentTask result has too many Evidence references")
        for value in refs:
            _bounded_text(value, "evidence_ref", maximum=4096)
        object.__setattr__(self, "evidence_refs", refs)
        if self.error_code is not None:
            _identifier(self.error_code, "error_code")
        if self.message is not None:
            _bounded_text(self.message, "message", maximum=4096)

    @classmethod
    def succeeded(cls, evidence_refs: tuple[str, ...]) -> AgentTaskResult:
        return cls(
            status="succeeded",
            evidence_refs=evidence_refs,
            error_code=None,
            message=None,
        )

    @classmethod
    def cancelled(cls, message: str | None = None) -> AgentTaskResult:
        return cls(
            status="cancelled",
            evidence_refs=(),
            error_code=None,
            message=message,
        )


@dataclass(frozen=True)
class AgentTaskScheduleReceipt:
    """Non-terminal acknowledgement that a validation request was admitted."""

    task_id: str
    run_id: str
    completion_policy: AgentTaskCompletionPolicy
    submit_state: str
    receipt_id: str | None = None
    owner: str | None = None
    session_id: str | None = None
    originating_turn_id: str | None = None
    request_digest: str | None = None
    idempotency_key: str | None = None
    slurm_job_id: str | None = None
    resource_envelope_id: str | None = None
    workspace_revision: int | None = None
    workspace_digest: str | None = None
    created_at: str | None = None
    legacy_boundary: bool | None = None

    def __post_init__(self) -> None:
        _identifier(self.task_id, "task_id")
        _identifier(self.run_id, "run_id")
        if not isinstance(self.completion_policy, AgentTaskCompletionPolicy):
            try:
                object.__setattr__(
                    self,
                    "completion_policy",
                    AgentTaskCompletionPolicy(self.completion_policy),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("completion_policy is invalid") from exc
        _bounded_text(self.submit_state, "submit_state")
        if self.submit_state not in _SCHEDULE_SUBMIT_STATES:
            raise ValueError("submit_state is invalid")
        for value, label in (
            (self.receipt_id, "receipt_id"),
            (self.owner, "owner"),
            (self.session_id, "session_id"),
            (self.originating_turn_id, "originating_turn_id"),
            (self.idempotency_key, "idempotency_key"),
            (self.slurm_job_id, "slurm_job_id"),
            (self.resource_envelope_id, "resource_envelope_id"),
        ):
            if value is not None:
                _identifier(value, label)
        for value, label in (
            (self.request_digest, "request_digest"),
            (self.workspace_digest, "workspace_digest"),
        ):
            if value is not None:
                _digest(value, label)
        if self.workspace_revision is not None:
            _non_negative_int(self.workspace_revision, "workspace_revision")
        if self.legacy_boundary is None:
            object.__setattr__(self, "legacy_boundary", self.workspace_revision is None)
        elif not isinstance(self.legacy_boundary, bool):
            raise TypeError("legacy_boundary must be boolean")
        if self.workspace_revision is None and self.legacy_boundary is not True:
            raise ValueError("legacy workspace boundary is required without a revision")
        if self.workspace_revision is not None and self.legacy_boundary is not False:
            raise ValueError("legacy workspace boundary cannot mark a live revision")
        if self.created_at is not None:
            object.__setattr__(
                self, "created_at", timestamp(parse_timestamp(self.created_at, "created_at"))
            )

    @property
    def is_terminal(self) -> bool:
        return False


@dataclass(frozen=True)
class AgentTaskGateReceipt:
    """Immutable facts proving a terminal Evidence gate was verified."""

    task_id: str
    run_id: str
    run_terminal_state: str
    evidence_refs: tuple[str, ...]
    evidence_digest: str
    integrity_verified_at: str
    workspace_revision: int | None
    workspace_digest: str
    legacy_boundary: bool
    capsule_ref: str | None
    capsule_state: str
    evidence_state: str = "finalized"
    integrity_state: str = "verified"
    platform_snapshot_ref: str | None = None
    source_revision: str | None = None
    terminal_at: str | None = None
    seal_digest: str | None = None
    seal_marker_ref: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.task_id, "task_id")
        _identifier(self.run_id, "run_id")
        _bounded_text(self.run_terminal_state, "run_terminal_state")
        if self.run_terminal_state not in _RUN_TERMINAL_STATES:
            raise ValueError("run_terminal_state must be terminal")
        refs = tuple(self.evidence_refs)
        if len(refs) > 4096:
            raise ValueError("evidence_refs has too many entries")
        for value in refs:
            _bounded_text(value, "evidence_ref", maximum=4096)
        object.__setattr__(self, "evidence_refs", refs)
        _digest(self.evidence_digest, "evidence_digest")
        object.__setattr__(
            self,
            "integrity_verified_at",
            timestamp(parse_timestamp(self.integrity_verified_at, "integrity_verified_at")),
        )
        _digest(self.workspace_digest, "workspace_digest")
        if self.workspace_revision is not None:
            _non_negative_int(self.workspace_revision, "workspace_revision")
        if not isinstance(self.legacy_boundary, bool):
            raise TypeError("legacy_boundary must be boolean")
        if self.workspace_revision is None and self.legacy_boundary is not True:
            raise ValueError("legacy workspace boundary is required without a revision")
        if self.workspace_revision is not None and self.legacy_boundary is not False:
            raise ValueError("legacy workspace boundary cannot mark a live revision")
        if self.capsule_ref is not None:
            _bounded_text(self.capsule_ref, "capsule_ref", maximum=4096)
        _bounded_text(self.capsule_state, "capsule_state")
        if self.capsule_state not in _CAPSULE_STATES:
            raise ValueError("capsule_state is invalid")
        _bounded_text(self.evidence_state, "evidence_state")
        _bounded_text(self.integrity_state, "integrity_state")
        if self.evidence_state != "finalized":
            raise ValueError("evidence_state must be finalized")
        if self.integrity_state != "verified":
            raise ValueError("integrity_state must be verified")
        if self.capsule_state == "READY" and self.capsule_ref is None:
            raise ValueError("capsule_ref is required when capsule is READY")
        if self.capsule_state == "not_required" and self.capsule_ref is not None:
            raise ValueError("capsule_ref must be absent when capsule is not required")
        if self.platform_snapshot_ref is not None:
            _bounded_text(self.platform_snapshot_ref, "platform_snapshot_ref", maximum=4096)
        if self.source_revision is not None:
            _bounded_text(self.source_revision, "source_revision", maximum=4096)
        if self.seal_digest is not None:
            _digest(self.seal_digest, "seal_digest")
        if self.seal_marker_ref is not None:
            _bounded_text(self.seal_marker_ref, "seal_marker_ref", maximum=4096)
        if self.terminal_at is not None:
            object.__setattr__(
                self, "terminal_at", timestamp(parse_timestamp(self.terminal_at, "terminal_at"))
            )


@dataclass(frozen=True)
class AgentTaskLease:
    task_id: str
    owner: str
    worker_id: str
    version: int
    fencing_token: int
    expires_at: str

    def __post_init__(self) -> None:
        _identifier(self.task_id, "task_id")
        _identifier(self.owner, "owner")
        _identifier(self.worker_id, "worker_id")
        _non_negative_int(self.version, "version")
        _positive_int(self.fencing_token, "fencing_token")
        object.__setattr__(
            self,
            "expires_at",
            timestamp(parse_timestamp(self.expires_at, "expires_at")),
        )


@dataclass(frozen=True)
class AgentTaskRecord:
    task_id: str
    owner: str
    session_id: str
    turn_id: str
    project_id: str
    workspace_id: str
    task_kind: AgentTaskKind
    state: AgentTaskState
    version: int
    request_key: str
    request: AgentTaskRequest
    resource_envelope: AgentResourceEnvelope
    linked_run_id: str | None
    result: AgentTaskResult | None
    cancel_requested: bool
    lease_owner: str | None
    lease_expires_at: str | None
    fencing_token: int
    created_at: str
    updated_at: str
    schema_version: str = "pilot107.agent-task/v1"
    completion_policy: AgentTaskCompletionPolicy = AgentTaskCompletionPolicy.EVIDENCE_REQUIRED
    gate_state: AgentTaskGateState = AgentTaskGateState.CREATED
    schedule_receipt: AgentTaskScheduleReceipt | None = None
    gate_receipt: AgentTaskGateReceipt | None = None
    legacy_gate_unverified: bool = False

    def __post_init__(self) -> None:
        for value, label in (
            (self.task_id, "task_id"),
            (self.owner, "owner"),
            (self.session_id, "session_id"),
            (self.turn_id, "turn_id"),
            (self.project_id, "project_id"),
            (self.workspace_id, "workspace_id"),
            (self.request_key, "request_key"),
        ):
            _identifier(value, label)
        if self.task_kind != "slurm_validation":
            raise ValueError("AgentTask kind is invalid")
        if not isinstance(self.state, AgentTaskState):
            raise TypeError("AgentTask state is invalid")
        _non_negative_int(self.version, "version")
        _non_negative_int(self.fencing_token, "fencing_token")
        if not isinstance(self.request, AgentTaskRequest):
            raise TypeError("AgentTask request is invalid")
        if not isinstance(self.resource_envelope, AgentResourceEnvelope):
            raise TypeError("AgentTask resource envelope is invalid")
        if self.linked_run_id is not None:
            _identifier(self.linked_run_id, "linked_run_id")
        if self.result is not None and not isinstance(self.result, AgentTaskResult):
            raise TypeError("AgentTask result is invalid")
        if not isinstance(self.cancel_requested, bool):
            raise TypeError("cancel_requested must be boolean")
        if (self.lease_owner is None) != (self.lease_expires_at is None):
            raise ValueError("AgentTask lease fields must be paired")
        if self.lease_owner is not None:
            _identifier(self.lease_owner, "lease_owner")
            object.__setattr__(
                self,
                "lease_expires_at",
                timestamp(parse_timestamp(self.lease_expires_at or "", "lease_expires_at")),
            )
        if self.state in TERMINAL_TASK_STATES and self.result is None:
            raise ValueError("terminal AgentTask requires a result")
        if self.result is not None and self.result.status != self.state.value:
            raise ValueError("AgentTask result does not match task state")
        if not isinstance(self.completion_policy, AgentTaskCompletionPolicy):
            try:
                object.__setattr__(
                    self,
                    "completion_policy",
                    AgentTaskCompletionPolicy(self.completion_policy),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("completion_policy is invalid") from exc
        if not isinstance(self.gate_state, AgentTaskGateState):
            try:
                object.__setattr__(self, "gate_state", AgentTaskGateState(self.gate_state))
            except (TypeError, ValueError) as exc:
                raise ValueError("gate_state is invalid") from exc
        if self.schedule_receipt is not None and not isinstance(
            self.schedule_receipt, AgentTaskScheduleReceipt
        ):
            raise TypeError("AgentTask schedule receipt is invalid")
        if self.schedule_receipt is not None:
            if self.schedule_receipt.task_id != self.task_id:
                raise ValueError("schedule receipt task_id does not match AgentTask")
            if self.linked_run_id is None:
                if self.schedule_receipt.submit_state != "admitted":
                    raise ValueError("schedule receipt must remain admitted before linked Run")
            elif self.schedule_receipt.run_id != self.linked_run_id:
                raise ValueError("schedule receipt Run does not match linked Run")
        if (
            self.schedule_receipt is not None
            and self.schedule_receipt.completion_policy is not self.completion_policy
        ):
            raise ValueError("AgentTask completion_policy does not match schedule receipt")
        if self.gate_receipt is not None and not isinstance(
            self.gate_receipt, AgentTaskGateReceipt
        ):
            raise TypeError("AgentTask gate receipt is invalid")
        if self.gate_receipt is not None:
            if self.gate_receipt.task_id != self.task_id:
                raise ValueError("gate receipt task_id does not match AgentTask")
            if self.linked_run_id is None:
                raise ValueError("gate receipt requires a linked Run")
            if self.gate_receipt.run_id != self.linked_run_id:
                raise ValueError("gate receipt Run does not match linked Run")
            if self.completion_policy.requires_capsule and (
                self.gate_receipt.capsule_state != "READY" or self.gate_receipt.capsule_ref is None
            ):
                raise ValueError("Capsule-required AgentTask needs a READY Capsule reference")
        if not isinstance(self.legacy_gate_unverified, bool):
            raise TypeError("legacy_gate_unverified must be boolean")
        object.__setattr__(
            self,
            "created_at",
            timestamp(parse_timestamp(self.created_at, "created_at")),
        )
        object.__setattr__(
            self,
            "updated_at",
            timestamp(parse_timestamp(self.updated_at, "updated_at")),
        )


def agent_task_payload(value: AgentTaskRecord) -> dict[str, Any]:
    """Return the authority-safe wire representation frozen by the v1 schema."""

    if not isinstance(value, AgentTaskRecord):
        raise TypeError("value must be AgentTaskRecord")
    envelope = value.resource_envelope
    result = value.result
    lease = None
    if value.lease_owner is not None:
        lease = {
            "owner": value.lease_owner,
            "expires_at": value.lease_expires_at,
            "fencing_token": value.fencing_token,
        }
    payload = {
        "schema_version": value.schema_version,
        "task_id": value.task_id,
        "owner": value.owner,
        "session_id": value.session_id,
        "turn_id": value.turn_id,
        "project_id": value.project_id,
        "workspace_id": value.workspace_id,
        "task_kind": value.task_kind,
        "state": value.state.value,
        "version": value.version,
        "request_key": value.request_key,
        "completion_policy": value.completion_policy.value,
        "gate_state": value.gate_state.value,
        "schedule_receipt": (
            agent_task_schedule_receipt_payload(value.schedule_receipt)
            if value.schedule_receipt is not None
            else None
        ),
        "gate_receipt": (
            agent_task_gate_receipt_payload(value.gate_receipt)
            if value.gate_receipt is not None
            else None
        ),
        "legacy_gate_unverified": value.legacy_gate_unverified,
        "cancel_requested": value.cancel_requested,
        "resource_envelope": {
            "partition": envelope.partition,
            "qos": envelope.qos,
            "cpus": envelope.cpus,
            "memory_mib": envelope.memory_mib,
            "gpu_type": envelope.gpu_type,
            "gpus": envelope.gpus,
            "walltime_seconds": envelope.walltime_seconds,
            "max_tasks": envelope.max_tasks,
            "max_submissions": envelope.max_submissions,
            "workspace_snapshot_digest": envelope.workspace_snapshot_digest,
            "expires_at": envelope.expires_at,
            "approved_by": envelope.approved_by,
        },
        "linked_run_id": value.linked_run_id,
        "result": (
            {
                "status": result.status,
                "evidence_refs": list(result.evidence_refs),
                "error_code": result.error_code,
                "message": result.message,
            }
            if result is not None
            else None
        ),
        "lease": lease,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
    }
    return payload


def agent_task_schedule_receipt_payload(
    value: AgentTaskScheduleReceipt,
) -> dict[str, Any]:
    """Serialize a schedule receipt without presenting it as a terminal result."""

    if not isinstance(value, AgentTaskScheduleReceipt):
        raise TypeError("value must be AgentTaskScheduleReceipt")
    required = (
        value.receipt_id,
        value.owner,
        value.session_id,
        value.originating_turn_id,
        value.request_digest,
        value.idempotency_key,
        value.resource_envelope_id,
        value.workspace_digest,
        value.created_at,
    )
    if any(item is None for item in required):
        raise ValueError("schedule receipt is incomplete for wire serialization")
    return {
        "receipt_id": value.receipt_id,
        "task_id": value.task_id,
        "owner": value.owner,
        "session_id": value.session_id,
        "originating_turn_id": value.originating_turn_id,
        "request_digest": value.request_digest,
        "idempotency_key": value.idempotency_key,
        "run_id": value.run_id,
        "submit_state": value.submit_state,
        "slurm_job_id": value.slurm_job_id,
        "resource_envelope_id": value.resource_envelope_id,
        "workspace_revision": value.workspace_revision,
        "workspace_digest": value.workspace_digest,
        "completion_policy": value.completion_policy.value,
        "created_at": value.created_at,
        "legacy_boundary": value.legacy_boundary,
    }


def agent_task_gate_receipt_payload(value: AgentTaskGateReceipt) -> dict[str, Any]:
    """Serialize immutable terminal gate facts for the AgentTask wire contract."""

    if not isinstance(value, AgentTaskGateReceipt):
        raise TypeError("value must be AgentTaskGateReceipt")
    payload = {
        "task_id": value.task_id,
        "run_id": value.run_id,
        "run_terminal_state": value.run_terminal_state,
        "evidence_state": value.evidence_state,
        "evidence_refs": list(value.evidence_refs),
        "evidence_digest": value.evidence_digest,
        "integrity_verified_at": value.integrity_verified_at,
        "integrity_state": value.integrity_state,
        "workspace_revision": value.workspace_revision,
        "workspace_digest": value.workspace_digest,
        "legacy_boundary": value.legacy_boundary,
        "capsule_ref": value.capsule_ref,
        "capsule_state": value.capsule_state,
    }
    if value.platform_snapshot_ref is not None:
        payload["platform_snapshot_ref"] = value.platform_snapshot_ref
    if value.source_revision is not None:
        payload["source_revision"] = value.source_revision
    if value.terminal_at is not None:
        payload["terminal_at"] = value.terminal_at
    if value.seal_digest is not None:
        payload["seal_digest"] = value.seal_digest
    if value.seal_marker_ref is not None:
        payload["seal_marker_ref"] = value.seal_marker_ref
    return payload


def parse_timestamp(value: str, label: str) -> datetime:
    if not isinstance(value, str) or not 20 <= len(value) <= 64:
        raise ValueError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def timestamp(value: datetime) -> str:
    return _aware_utc(value).isoformat().replace("+00:00", "Z")


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _digest(value: str, label: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _bounded_text(value: str, label: str, *, maximum: int = 128) -> None:
    if not isinstance(value, str) or not value or len(value) > maximum or "\0" in value:
        raise ValueError(f"{label} is invalid")


def _non_negative_int(value: int, label: str, *, maximum: int = 2**53 - 1) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{label} is invalid")


def _positive_int(value: int, label: str, *, maximum: int = 2**53 - 1) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{label} is invalid")
