from __future__ import annotations

from pathlib import Path

import pytest

from pilot107.agent.durable_workspace import DurableWorkspaceEditor
from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.workspace import WorkspacePatch
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


def test_postgres_like_project_store_reads_can_start_but_mutation_fails_closed(
    tmp_path: Path,
) -> None:
    class PostgresLikeStore:
        dsn = "postgresql://example.invalid/pilot107"

    editor = build_authoritative_workspace_editor(
        store=PostgresLikeStore(),  # type: ignore[arg-type]
        workspace_root=tmp_path / "agent-workspaces",
    )

    with pytest.raises(WorkspaceDurabilityUnavailable, match="PostgreSQL") as caught:
        editor.apply_patches(
            "workspace-1",
            "alice",
            (("a.py", None, WorkspacePatch(operation="create", content="x = 1\n")),),
        )
    assert caught.value.code == "AGENT.TOOL.WORKSPACE_DURABILITY_UNAVAILABLE"
    assert caught.value.retryable is False


def test_unknown_project_store_is_mutation_fail_closed(tmp_path: Path) -> None:
    editor = build_authoritative_workspace_editor(
        store=object(),  # type: ignore[arg-type]
        workspace_root=tmp_path / "agent-workspaces",
    )

    with pytest.raises(WorkspaceDurabilityUnavailable, match="no supported") as caught:
        editor.apply_patches(
            "workspace-1",
            "alice",
            (("a.py", None, WorkspacePatch(operation="create", content="x = 1\n")),),
        )
    assert caught.value.code == "AGENT.TOOL.WORKSPACE_DURABILITY_UNAVAILABLE"
