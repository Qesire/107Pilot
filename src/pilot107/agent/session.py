"""Durable Agent Session domain records and state invariants."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class AgentSessionConflict(RuntimeError):
    """Raised when idempotency, state-version, or fencing checks fail."""


class AgentSessionInvariantError(ValueError):
    """Raised when a durable Agent record would violate a closed invariant."""


class AgentSessionState(StrEnum):
    IDLE = "idle"
    QUEUED = "queued"
    RUNNING = "running"


class AgentTurnState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class AgentSessionRecord:
    session_id: str
    owner: str
    request_key: str
    profile_id: str
    model_profile_id: str
    source: dict[str, Any]
    state: AgentSessionState
    state_version: int
    context_checkpoint: dict[str, Any] | None
    resource_usage: dict[str, Any]
    outcome: dict[str, Any] | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AgentTurnRecord:
    turn_id: str
    session_id: str
    owner: str
    request_key: str
    input_digest: str
    message: str
    state_version: int
    state: AgentTurnState
    cancel_requested: bool
    lease_owner: str | None
    lease_expires_at: str | None
    fencing_token: int
    event_sequence: int
    final_checkpoint: dict[str, Any] | None
    error: dict[str, Any] | None
    created_at: str
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True)
class AgentTurnLease:
    turn_id: str
    session_id: str
    owner: str
    worker_id: str
    state_version: int
    fencing_token: int
    expires_at: str


@dataclass(frozen=True)
class AgentTurnEventRecord:
    event_id: int
    turn_id: str
    session_id: str
    owner: str
    sequence: int
    event_type: str
    payload: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class AgentToolInvocationRecord:
    invocation_id: str
    idempotency_key: str
    turn_id: str
    session_id: str
    owner: str
    tool_name: str
    arguments_digest: str
    state: str
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    bytes_returned: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AgentTurnToolUsage:
    invocations: int
    bytes_returned: int
    commands: int
