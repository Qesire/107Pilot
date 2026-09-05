"""Backend-aware construction for authoritative Workspace mutation editors."""

from __future__ import annotations

from pathlib import Path

from pilot107.agent.durable_workspace import DurableWorkspaceEditor
from pilot107.agent.project_store import ProjectStore
from pilot107.agent.workspace import WorkspaceEditor


class WorkspaceDurabilityUnavailable(RuntimeError):
    """The selected Project persistence backend lacks AC4 mutation authority."""


def build_authoritative_workspace_editor(
    *,
    store: ProjectStore,
    workspace_root: Path,
) -> WorkspaceEditor:
    """Return the only editor permitted to mutate Agent Workspace files.

    SQLite has an AC4 live-head + journal implementation.  PostgreSQL Project
    persistence already exists, but its AC4 live-head/journal transaction domain
    is not implemented yet.  Falling back to the legacy editor in PostgreSQL
    mode would silently reintroduce the crash/concurrency hole, so it is blocked.
    """

    db_path = getattr(store, "db_path", None)
    if isinstance(db_path, Path):
        return DurableWorkspaceEditor(
            store=store,
            state_root=workspace_root.resolve().parent / "agent-workspace-state",
        )
    dsn = getattr(store, "dsn", None)
    if isinstance(dsn, str) and dsn:
        raise WorkspaceDurabilityUnavailable(
            "PostgreSQL Agent Workspace mutation requires AC4 PostgreSQL live-head/journal parity"
        )
    raise WorkspaceDurabilityUnavailable(
        "Agent Workspace mutation store has no supported AC4 durability authority"
    )
