from __future__ import annotations

from pathlib import Path

import pytest

from pilot107.agent.durable_workspace import DurableWorkspaceEditor
from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.workspace_editor_factory import (
    WorkspaceDurabilityUnavailable,
    build_authoritative_workspace_editor,
)


def test_sqlite_project_store_selects_durable_workspace_editor(tmp_path: Path) -> None:
    store = SQLiteProjectStore(tmp_path / "pilot107.db")

    editor = build_authoritative_workspace_editor(
        store=store,
        workspace_root=tmp_path / "agent-workspaces",
    )

    assert isinstance(editor, DurableWorkspaceEditor)


def test_postgres_like_project_store_does_not_fall_back_to_legacy_editor(
    tmp_path: Path,
) -> None:
    class PostgresLikeStore:
        dsn = "postgresql://example.invalid/pilot107"

    with pytest.raises(WorkspaceDurabilityUnavailable, match="PostgreSQL"):
        build_authoritative_workspace_editor(
            store=PostgresLikeStore(),  # type: ignore[arg-type]
            workspace_root=tmp_path / "agent-workspaces",
        )


def test_unknown_project_store_is_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceDurabilityUnavailable, match="no supported"):
        build_authoritative_workspace_editor(
            store=object(),  # type: ignore[arg-type]
            workspace_root=tmp_path / "agent-workspaces",
        )
