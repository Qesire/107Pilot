"""Domain models for M2 repair ticket handoff.

ArtifactManifest is metadata-only: it records revision identifiers and local
test summaries chosen by the user.  It never contains source code, full diffs,
or repository content.

RepairTicket represents the Agent-to-local-tool handoff when a diagnosis points
to a code problem.  The ticket carries bound evidence facts and an optional
restricted code-context window, but never grants shell access or full repo
traversal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# ArtifactManifest
# ---------------------------------------------------------------------------

DISCLOSURE_LEVELS = frozenset({"metadata_only", "summary"})


@dataclass(frozen=True)
class ArtifactManifest:
    """User-supplied metadata about a local code fix.

    This is NOT an upload of code.  107Pilot stores only the identifiers and
    summaries the user explicitly provides; the real Slurm job still runs from
    the user's existing workdir.
    """

    manifest_id: str
    owner: str
    run_id: str | None
    revision: str
    dirty_diff_digest: str | None = None
    bundle_digest: str | None = None
    remote_workdir: str | None = None
    local_test_summary: str | None = None
    disclosure: str = "metadata_only"
    created_at: str = ""

    def __post_init__(self) -> None:
        if self.disclosure not in DISCLOSURE_LEVELS:
            raise RepairTicketInvariantError(
                f"disclosure must be one of {sorted(DISCLOSURE_LEVELS)}"
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "owner": self.owner,
            "run_id": self.run_id,
            "revision": self.revision,
            "dirty_diff_digest": self.dirty_diff_digest,
            "bundle_digest": self.bundle_digest,
            "remote_workdir": self.remote_workdir,
            "local_test_summary": self.local_test_summary,
            "disclosure": self.disclosure,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# RepairTicket
# ---------------------------------------------------------------------------


class RepairTicketState(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    ABANDONED = "abandoned"


TERMINAL_TICKET_STATES = frozenset(
    {RepairTicketState.RESOLVED, RepairTicketState.ABANDONED}
)

_TRANSITIONS: dict[RepairTicketState, frozenset[RepairTicketState]] = {
    RepairTicketState.OPEN: frozenset(
        {RepairTicketState.RESOLVED, RepairTicketState.ABANDONED}
    ),
    RepairTicketState.RESOLVED: frozenset(),
    RepairTicketState.ABANDONED: frozenset(),
}


class RepairTicketInvariantError(ValueError):
    """Raised when a domain transition or field constraint is violated."""


class RepairTicketConflict(RuntimeError):
    """Raised when a compare-and-swap or ownership check fails."""


def assert_ticket_transition(
    current: RepairTicketState,
    target: RepairTicketState,
) -> None:
    if target not in _TRANSITIONS[current]:
        raise RepairTicketInvariantError(
            f"invalid repair ticket transition: {current.value} -> {target.value}"
        )


@dataclass(frozen=True)
class RepairTicket:
    """A code-repair handoff ticket created by the Agent.

    The ticket binds diagnoses and cited evidence facts from a failed Run,
    optionally includes a restricted code-context window, and describes the
    requested change.  It does NOT grant shell access or full repo traversal.
    """

    ticket_id: str
    owner: str
    state: RepairTicketState
    source_run_id: str
    source_contract_id: str | None = None
    session_id: str | None = None
    diagnosis_ids: tuple[str, ...] = field(default_factory=tuple)
    cited_facts: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    code_context: dict[str, Any] | None = None
    requested_change: str | None = None
    no_go_constraints: tuple[str, ...] = field(default_factory=tuple)
    resolution_manifest_id: str | None = None
    resolution_run_id: str | None = None
    resolution_comparison: dict[str, Any] | None = None
    abandon_reason: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "owner": self.owner,
            "state": self.state.value,
            "source_run_id": self.source_run_id,
            "source_contract_id": self.source_contract_id,
            "session_id": self.session_id,
            "diagnosis_ids": list(self.diagnosis_ids),
            "cited_facts": list(self.cited_facts),
            "code_context": self.code_context,
            "requested_change": self.requested_change,
            "no_go_constraints": list(self.no_go_constraints),
            "resolution_manifest_id": self.resolution_manifest_id,
            "resolution_run_id": self.resolution_run_id,
            "resolution_comparison": self.resolution_comparison,
            "abandon_reason": self.abandon_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
