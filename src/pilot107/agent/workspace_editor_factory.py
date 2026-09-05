"""Backend-aware construction for authoritative Agent Workspace mutation editors."""

from __future__ import annotations

from pathlib import Path

from pilot107.agent.durable_workspace_atomic import AtomicDurableWorkspaceEditor
from pilot107.agent.operation_context import current_agent_operation_key
from pilot107.agent.postgres_workspace_atomic import PostgresAtomicDurableWorkspaceEditor
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


def _translate_workspace_error(exc: Exception) -> None:
    if current_agent_operation_key() is None:
        raise exc
    if isinstance(exc, WorkspacePolicyError):
        raise AgentToolGatewayError(
            "Workspace patch violates the mutation policy",
            code="AGENT.TOOL.WORKSPACE_POLICY",
            retryable=False,
        ) from None
    if isinstance(exc, WorkspaceConflict):
        raise AgentToolGatewayError(
            "Workspace content changed before the mutation could commit",
            code="AGENT.TOOL.WORKSPACE_CONFLICT",
            retryable=True,
        ) from None
    raise exc


class AuthoritativeSQLiteWorkspaceEditor(AtomicDurableWorkspaceEditor):
    """SQLite AC4 editor with Gateway-facing error classification."""

    def apply_patches(
        self,
        workspace_id: str,
        owner: str,
        patches: tuple[tuple[str, str | None, WorkspacePatch], ...],
    ) -> WorkspaceChangeSet:
        try:
            return super().apply_patches(workspace_id, owner, patches)
        except (WorkspacePolicyError, WorkspaceConflict) as exc:
            _translate_workspace_error(exc)
            raise AssertionError("unreachable")


class AuthoritativePostgresWorkspaceEditor(PostgresAtomicDurableWorkspaceEditor):
    """PostgreSQL AC4 editor with the same Gateway error contract as SQLite."""

    def apply_patches(
        self,
        workspace_id: str,
        owner: str,
        patches: tuple[tuple[str, str | None, WorkspacePatch], ...],
    ) -> WorkspaceChangeSet:
        try:
            return super().apply_patches(workspace_id, owner, patches)
        except (WorkspacePolicyError, WorkspaceConflict) as exc:
            _translate_workspace_error(exc)
            raise AssertionError("unreachable")


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

    Both supported persistence backends use the AC4 protocol: an OS lock,
    durable live-head revision, fenced writer lease, write-ahead mutation
    journal, verified filesystem backup, and atomic publication of ChangeSet +
    live-head advance + COMMITTED receipt. PostgreSQL is the production
    authority; SQLite remains the development/test implementation.
    """

    state_root = workspace_root.resolve().parent / "agent-workspace-state"
    db_path = getattr(store, "db_path", None)
    if isinstance(db_path, Path):
        return AuthoritativeSQLiteWorkspaceEditor(
            store=store,
            state_root=state_root,
        )
    dsn = getattr(store, "dsn", None)
    if isinstance(dsn, str) and dsn:
        return AuthoritativePostgresWorkspaceEditor(
            store=store,
            state_root=state_root,
        )
    return UnavailableWorkspaceEditor(
        "Agent Workspace mutation store has no supported AC4 durability authority"
    )
