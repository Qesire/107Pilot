"""Persistent multi-turn remediation domain model and state invariants."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RemediationState(StrEnum):
    WAITING_EVIDENCE = "waiting_evidence"
    DIAGNOSING = "diagnosing"
    PLANNING = "planning"
    AWAITING_INPUT = "awaiting_input"
    AWAITING_APPROVAL = "awaiting_approval"
    READY = "ready"
    PREPARING = "preparing"
    EXECUTING = "executing"
    EVALUATING = "evaluating"
    SUCCEEDED = "succeeded"
    EXHAUSTED = "exhausted"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_REMEDIATION_STATES = frozenset(
    {
        RemediationState.SUCCEEDED,
        RemediationState.EXHAUSTED,
        RemediationState.BLOCKED,
        RemediationState.FAILED,
        RemediationState.CANCELLED,
    }
)


class EvaluationOutcome(StrEnum):
    VERIFIED_SUCCESS = "verified_success"
    EXECUTION_SUCCESS_UNVERIFIED = "execution_success_unverified"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class RemediationConflict(RuntimeError):
    """Raised when a state/version/lease compare-and-swap fails."""


class RemediationInvariantError(ValueError):
    """Raised when a domain transition or budget is invalid."""


@dataclass(frozen=True)
class RemediationBudget:
    max_attempts: int = 3
    max_submissions: int = 2
    max_wall_time_seconds: int = 3600
    max_llm_calls: int = 3
    max_llm_tokens: int = 6000

    def __post_init__(self) -> None:
        for name, value in self.to_payload().items():
            if not isinstance(value, int) or value < 0:
                raise RemediationInvariantError(f"{name} must be a non-negative integer")
        if self.max_attempts == 0:
            raise RemediationInvariantError("max_attempts must be positive")

    def to_payload(self) -> dict[str, int]:
        return {
            "max_attempts": self.max_attempts,
            "max_submissions": self.max_submissions,
            "max_wall_time_seconds": self.max_wall_time_seconds,
            "max_llm_calls": self.max_llm_calls,
            "max_llm_tokens": self.max_llm_tokens,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RemediationBudget:
        return cls(
            max_attempts=_int_value(payload, "max_attempts", 3),
            max_submissions=_int_value(payload, "max_submissions", 2),
            max_wall_time_seconds=_int_value(payload, "max_wall_time_seconds", 3600),
            max_llm_calls=_int_value(payload, "max_llm_calls", 3),
            max_llm_tokens=_int_value(payload, "max_llm_tokens", 6000),
        )


@dataclass(frozen=True)
class RemediationUsage:
    attempts: int = 0
    submissions: int = 0
    wall_time_seconds: int = 0
    llm_calls: int = 0
    llm_tokens: int = 0

    def __post_init__(self) -> None:
        for name, value in self.to_payload().items():
            if not isinstance(value, int) or value < 0:
                raise RemediationInvariantError(f"{name} must be a non-negative integer")

    def to_payload(self) -> dict[str, int]:
        return {
            "attempts": self.attempts,
            "submissions": self.submissions,
            "wall_time_seconds": self.wall_time_seconds,
            "llm_calls": self.llm_calls,
            "llm_tokens": self.llm_tokens,
        }

    def exhausted_reason(self, budget: RemediationBudget) -> str | None:
        limits = (
            ("attempts", self.attempts, budget.max_attempts),
            ("submissions", self.submissions, budget.max_submissions),
            ("wall_time", self.wall_time_seconds, budget.max_wall_time_seconds),
            ("llm_calls", self.llm_calls, budget.max_llm_calls),
            ("llm_tokens", self.llm_tokens, budget.max_llm_tokens),
        )
        for name, used, maximum in limits:
            if used >= maximum:
                return f"budget_{name}_exhausted"
        return None


@dataclass(frozen=True)
class RemediationSession:
    session_id: str
    owner: str
    request_key: str
    state: RemediationState
    version: int
    source_run_id: str
    source_contract_id: str | None
    source_diagnosis_digest: str
    source_evidence_digest: str
    automation_policy: str
    budget: RemediationBudget
    usage: RemediationUsage
    stop_reason: str | None
    takeover_reason: str | None
    lease_owner: str | None
    lease_expires_at: str | None
    created_at: str
    updated_at: str
    provider: str = "none"


@dataclass(frozen=True)
class AgentTurn:
    turn_id: str
    session_id: str
    turn_index: int
    state: str
    source_run_id: str
    advice_id: str | None
    payload: dict[str, Any]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ActionProposal:
    proposal_id: str
    session_id: str
    turn_id: str
    action_id: str
    action_type: str
    source: str
    risk: str
    approval_required: bool
    policy_status: str
    payload: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class ActionDecision:
    decision_id: str
    session_id: str
    proposal_id: str
    actor: str
    decision: str
    expected_session_version: int
    note: str | None
    created_at: str


@dataclass(frozen=True)
class ActionExecution:
    execution_id: str
    session_id: str
    proposal_id: str
    state: str
    derived_contract_id: str | None
    derived_run_id: str | None
    error_code: str | None
    error_message: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class EvaluationResult:
    evaluation_id: str
    session_id: str
    execution_id: str
    source_run_id: str
    derived_run_id: str
    outcome: EvaluationOutcome
    checks: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    comparison: dict[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    created_at: str = ""


@dataclass(frozen=True)
class RemediationEvent:
    event_id: int
    session_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: str


_TRANSITIONS: dict[RemediationState, frozenset[RemediationState]] = {
    RemediationState.WAITING_EVIDENCE: frozenset(
        {RemediationState.DIAGNOSING, RemediationState.BLOCKED, RemediationState.CANCELLED}
    ),
    RemediationState.DIAGNOSING: frozenset(
        {
            RemediationState.PLANNING,
            RemediationState.BLOCKED,
            RemediationState.FAILED,
            RemediationState.CANCELLED,
        }
    ),
    RemediationState.PLANNING: frozenset(
        {
            RemediationState.AWAITING_INPUT,
            RemediationState.AWAITING_APPROVAL,
            RemediationState.READY,
            RemediationState.BLOCKED,
            RemediationState.FAILED,
            RemediationState.CANCELLED,
        }
    ),
    RemediationState.AWAITING_INPUT: frozenset(
        {RemediationState.PLANNING, RemediationState.BLOCKED, RemediationState.CANCELLED}
    ),
    RemediationState.AWAITING_APPROVAL: frozenset(
        {RemediationState.READY, RemediationState.BLOCKED, RemediationState.CANCELLED}
    ),
    RemediationState.READY: frozenset(
        {
            RemediationState.PREPARING,
            RemediationState.EXHAUSTED,
            RemediationState.BLOCKED,
            RemediationState.CANCELLED,
        }
    ),
    RemediationState.PREPARING: frozenset(
        {
            RemediationState.READY,
            RemediationState.EXECUTING,
            RemediationState.AWAITING_APPROVAL,
            RemediationState.BLOCKED,
            RemediationState.FAILED,
            RemediationState.CANCELLED,
        }
    ),
    RemediationState.EXECUTING: frozenset(
        {
            RemediationState.EVALUATING,
            RemediationState.BLOCKED,
            RemediationState.FAILED,
            RemediationState.CANCELLED,
        }
    ),
    RemediationState.EVALUATING: frozenset(
        {
            RemediationState.SUCCEEDED,
            RemediationState.PLANNING,
            RemediationState.EXHAUSTED,
            RemediationState.BLOCKED,
            RemediationState.FAILED,
            RemediationState.CANCELLED,
        }
    ),
    **{state: frozenset() for state in TERMINAL_REMEDIATION_STATES},
}


def assert_remediation_transition(
    current: RemediationState,
    target: RemediationState,
) -> None:
    if target not in _TRANSITIONS[current]:
        raise RemediationInvariantError(
            f"invalid remediation transition: {current.value} -> {target.value}"
        )


def _int_value(payload: dict[str, Any], key: str, default: int) -> int:
    value = payload.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise RemediationInvariantError(f"{key} must be an integer")
    return value
