"""Backend-aware construction for authoritative Workspace mutation editors."""

from __future__ import annotations

from pathlib import Path

from pilot107.agent.durable_workspace_atomic import AtomicDurableWorkspaceEditor
from pilot107.agent.operation_context import current_agent_operation_key
from pilot107.agent.project_store import ProjectStore
from pilot107.agent.tool_gateway import AgentToolGatewayError
from pilot107.agent.workspace import (
    WorkspaceChangeSet,
    WorkspaceConflict,
    WorkspaceEditor,
    WorkspacePatch,
    WorkspacePolicyError,
)


class WorkspaceDurabilityUnavailable(AgentToolGatewayError):
    """The selected Project persistence backend lacks AC4 mutation authority."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            code="AGENT.TOOL.WORKSPACE_DURABILITY_UNAVAILABLE",
            retryable=False,
        )


class AuthoritativeSQLiteWorkspaceEditor(AtomicDurableWorkspaceEditor):
    """Translate proven no-effect Workspace failures at the Gateway boundary."""

    def apply_patches(
        self,
        workspace_id: str,
        owner: str,
        patches: tuple[tuple[str, str | None, WorkspacePatch], ...],
    ) -> WorkspaceChangeSet:
        try:
            return super().apply_patches(workspace_id, owner, patches)
        except WorkspacePolicyError:
            if current_agent_operation_key() is None:
                raise
            raise AgentToolGatewayError(
                "Workspace patch violates the mutation policy",
                code="AGENT.TOOL.WORKSPACE_POLICY",
                retryable=False,
            ) from None
        except WorkspaceConflict:
            if current_agent_operation_key() is None:
                raise
            raise AgentToolGatewayError(
                "Workspace content changed before the mutation could commit",
                code="AGENT.TOOL.WORKSPACE_CONFLICT",
                retryable=True,
            ) from None


class UnavailableWorkspaceEditor(WorkspaceEditor):
    """Read-compatible service placeholder that rejects every file mutation."""

    def __init__(self, message: str) -> None:
        self.message = message

    def apply_patch(
        self,
        workspace_id: str,
        owner: str,
        relative_path: str,
        expected_source_digest: str | None,
        patch: WorkspacePatch,
    ) -> WorkspaceChangeSet:
        del workspace_id, owner, relative_path, expected_source_digest, patch
        raise WorkspaceDurabilityUnavailable(self.message)

    def apply_patches(
        self,
        workspace_id: str,
        owner: str,
        patches: tuple[tuple[str, str | None, WorkspacePatch], ...],
    ) -> WorkspaceChangeSet:
        del workspace_id, owner, patches
        raise WorkspaceDurabilityUnavailable(self.message)


def build_authoritative_workspace_editor(
    *,
    store: ProjectStore,
    workspace_root: Path,
) -> WorkspaceEditor:
    """Return the only editor permitted to mutate Agent Workspace files.

    SQLite has the full AC4 live-head, journal, crash recovery, and atomic
    ChangeSet publication boundary. PostgreSQL Project persistence already
    exists, but its AC4 live-head/journal transaction domain is not implemented
    yet. Falling back to the legacy editor in PostgreSQL mode would silently
    reintroduce the crash/concurrency hole, so mutations are rejected while
    read-only Project/Workspace operations remain available.
    """

    db_path = getattr(store, "db_path", None)
    if isinstance(db_path, Path):
        return AuthoritativeSQLiteWorkspaceEditor(
            store=store,
            state_root=workspace_root.resolve().parent / "agent-workspace-state",
        )
    dsn = getattr(store, "dsn", None)
    if isinstance(dsn, str) and dsn:
        return UnavailableWorkspaceEditor(
            "PostgreSQL Agent Workspace mutation requires AC4 PostgreSQL live-head/journal parity"
        )
    return UnavailableWorkspaceEditor(
        "Agent Workspace mutation store has no supported AC4 durability authority"
    )
