from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pilot107.agent.durable_workspace_atomic import AtomicDurableWorkspaceEditor
from pilot107.agent.operation_context import bind_agent_operation_key
from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.tool_gateway import AgentToolGatewayError
from pilot107.agent.workspace import AgentWorkspaceRecord, WorkspacePatch, WorkspaceSnapshot
from pilot107.agent.workspace_editor_factory import (
    WorkspaceDurabilityUnavailable,
    build_authoritative_workspace_editor,
)


def test_sqlite_project_store_selects_atomic_durable_workspace_editor(tmp_path: Path) -> None:
    store = SQLiteProjectStore(tmp_path / "pilot107.db")

    editor = build_authoritative_workspace_editor(
        store=store,
        workspace_root=tmp_path / "agent-workspaces",
    )

    assert isinstance(editor, AtomicDurableWorkspaceEditor)


def test_gateway_policy_rejection_is_terminal_without_workspace_side_effect(tmp_path: Path) -> None:
    store = SQLiteProjectStore(tmp_path / "pilot107.db")
    project = store.create_project(
        owner="alice",
        origin="blank",
        goal="policy classification",
        request_key="policy-project",
    )
    root = tmp_path / "agent-workspaces" / "alice" / "workspace-policy"
    root.mkdir(parents=True)
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    workspace = store.save_workspace(
        AgentWorkspaceRecord(
            workspace_id="workspace-policy",
            project_id=project.project_id,
            owner="alice",
            local_root=str(root),
            snapshot=WorkspaceSnapshot(
                source_ref="/__pilot107_blank__/policy-project",
                digest="a" * 64,
                entries=(),
                captured_at=now,
            ),
            created_at=now,
            updated_at=now,
        )
    )
    editor = build_authoritative_workspace_editor(
        store=store,
        workspace_root=tmp_path / "agent-workspaces",
    )

    with bind_agent_operation_key("operation-" + "1" * 64):
        with pytest.raises(AgentToolGatewayError) as caught:
            editor.apply_patches(
                workspace.workspace_id,
                "alice",
                (("weights.bin", None, WorkspacePatch(operation="create", content="x")),),
            )

    assert caught.value.code == "AGENT.TOOL.WORKSPACE_POLICY"
    assert caught.value.retryable is False
    assert not (root / "weights.bin").exists()
    assert store.list_change_sets(project.project_id, owner="alice") == []


def test_postgres_project_store_shape_selects_postgres_atomic_editor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, Path]] = []

    class PostgresLikeStore:
        dsn = "postgresql://example.invalid/pilot107"

    class SentinelPostgresEditor:
        def __init__(self, *, store: object, state_root: Path) -> None:
            calls.append((store, state_root))

    monkeypatch.setattr(
        "pilot107.agent.workspace_editor_factory.AuthoritativePostgresWorkspaceEditor",
        SentinelPostgresEditor,
    )
    store = PostgresLikeStore()

    editor = build_authoritative_workspace_editor(
        store=store,  # type: ignore[arg-type]
        workspace_root=tmp_path / "agent-workspaces",
    )

    assert isinstance(editor, SentinelPostgresEditor)
    assert calls == [
        (store, (tmp_path / "agent-workspace-state").resolve()),
    ]


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
