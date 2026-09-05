"""PostgreSQL-only construction for authoritative Agent Workspace mutations."""

from __future__ import annotations

from pathlib import Path

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
    """The Project persistence backend is not the PostgreSQL AC4 authority."""

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


class AuthoritativePostgresWorkspaceEditor(PostgresAtomicDurableWorkspaceEditor):
    """PostgreSQL AC4 editor with Gateway-facing error classification."""

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
            raise AssertionError("unreachable") from exc


def build_authoritative_workspace_editor(
    *,
    store: ProjectStore,
    workspace_root: Path,
) -> WorkspaceEditor:
    """Build the sole Agent Workspace mutation authority.

    Competition/runtime composition is PostgreSQL-only. A store without a valid
    PostgreSQL DSN is a configuration error; no SQLite editor, local fallback or
    read-compatible mutation placeholder is constructed.
    """

    dsn = getattr(store, "dsn", None)
    if not isinstance(dsn, str) or not dsn:
        raise WorkspaceDurabilityUnavailable(
            "Agent Workspace mutation requires PostgreSQL AC4 durability authority"
        )
    state_root = workspace_root.resolve().parent / "agent-workspace-state"
    return AuthoritativePostgresWorkspaceEditor(
        store=store,
        state_root=state_root,
    )
