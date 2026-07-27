"""Run lifecycle states.

RunState only describes Slurm/execution lifecycle. Evidence collection, diagnosis,
and capsule generation are tracked by independent substates.
"""

from __future__ import annotations

from enum import StrEnum


class RunState(StrEnum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETING = "COMPLETING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"
    SUBMIT_FAILED = "SUBMIT_FAILED"
    COLLECTION_FAILED = "COLLECTION_FAILED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    SUBMISSION_UNCERTAIN = "SUBMISSION_UNCERTAIN"
    EVIDENCE_PARTIAL = "EVIDENCE_PARTIAL"
    ORPHANED = "ORPHANED"


class CollectionState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    FAILED = "failed"


class DiagnosisState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"


class CapsuleState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"


class ResultStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    INVALID = "INVALID"


ACTIVE_JOB_RUN_STATES = frozenset(
    {
        RunState.SUBMITTED,
        RunState.PENDING,
        RunState.RUNNING,
        RunState.COMPLETING,
        RunState.UNKNOWN,
        RunState.SUBMISSION_UNCERTAIN,
    }
)

TERMINAL_RUN_STATES = frozenset(
    {
        RunState.SUCCEEDED,
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.SUBMIT_FAILED,
        RunState.COLLECTION_FAILED,
        RunState.AUTH_REQUIRED,
        RunState.ORPHANED,
    }
)


SLURM_TO_RUN_STATE: dict[str, RunState] = {
    "PENDING": RunState.PENDING,
    "CONFIGURING": RunState.PENDING,
    "RUNNING": RunState.RUNNING,
    "COMPLETING": RunState.COMPLETING,
    "COMPLETED": RunState.SUCCEEDED,
    "FAILED": RunState.FAILED,
    "TIMEOUT": RunState.FAILED,
    "OUT_OF_MEMORY": RunState.FAILED,
    "NODE_FAIL": RunState.FAILED,
    "PREEMPTED": RunState.FAILED,
    "CANCELLED": RunState.CANCELLED,
}


def normalize_slurm_state(raw_state: str | list[str]) -> tuple[RunState, list[str]]:
    """Normalize Slurm state while preserving all raw flags.

    Slurm REST may return either a scalar state or a list of state flags. The
    first known flag becomes the primary state; all flags remain available for
    evidence and diagnostics.
    """

    flags = [raw_state] if isinstance(raw_state, str) else list(raw_state)
    normalized_flags = [flag.upper() for flag in flags if flag]
    for flag in normalized_flags:
        if flag in SLURM_TO_RUN_STATE:
            return SLURM_TO_RUN_STATE[flag], normalized_flags
    return RunState.UNKNOWN, normalized_flags
