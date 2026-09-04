from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pilot107.core.research_workspace import (
    ResearchWorkspaceConflict,
    SQLiteResearchWorkspaceStore,
    WorkspaceBindingSource,
    WorkspaceObjectType,
    can_inherit_workspace_binding,
)


class FixedClock:
    def __call__(self) -> datetime:
        return datetime(2026, 9, 4, 8, 0, tzinfo=UTC)


class SequenceIds:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"id-{self.value}"


def _store(path: Path) -> SQLiteResearchWorkspaceStore:
    return SQLiteResearchWorkspaceStore(
        path,
        clock=FixedClock(),
        id_factory=SequenceIds(),
    )


def test_workspace_creation_is_owner_scoped_and_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path / "workspace.db")

    first, created = store.create_workspace(
        owner="alice",
        request_key="create-wan-sparse",
        title="WAN FFN 动态稀疏",
        description="Stage1 proxy 与 Stage2 mask consumption",
    )
    replay, replay_created = store.create_workspace(
        owner="alice",
        request_key="create-wan-sparse",
        title="WAN FFN 动态稀疏",
        description="Stage1 proxy 与 Stage2 mask consumption",
    )

    assert created is True
    assert replay_created is False
    assert replay == first
    assert store.get_workspace(first.workspace_id, owner="alice") == first
    with pytest.raises(KeyError):
        store.get_workspace(first.workspace_id, owner="bob")


def test_user_explicitly_binds_run_and_agent_context(tmp_path: Path) -> None:
    store = _store(tmp_path / "workspace.db")
    workspace, _ = store.create_workspace(
        owner="alice",
        request_key="workspace-1",
        title="Experiment A",
    )

    run_binding, created = store.bind_user_selected(
        workspace_id=workspace.workspace_id,
        owner="alice",
        object_type=WorkspaceObjectType.RUN,
        object_id="run-12",
        actor="alice",
    )

    assert created is True
    assert run_binding.source is WorkspaceBindingSource.USER
    assert run_binding.parent_binding_id is None

    remediation, _ = store.inherit_binding(
        parent_binding_id=run_binding.binding_id,
        owner="alice",
        child_type=WorkspaceObjectType.REMEDIATION_SESSION,
        child_id="remediation-1",
    )
    agent_session, _ = store.inherit_binding(
        parent_binding_id=remediation.binding_id,
        owner="alice",
        child_type=WorkspaceObjectType.AGENT_SESSION,
        child_id="session-agent-1",
    )

    assert remediation.source is WorkspaceBindingSource.INHERITED
    assert agent_session.parent_binding_id == remediation.binding_id
    assert {item.object_id for item in store.list_bindings(workspace.workspace_id, owner="alice")} == {
        "run-12",
        "remediation-1",
        "session-agent-1",
    }


def test_binding_never_uses_agent_suggestion_without_owner_approval(tmp_path: Path) -> None:
    store = _store(tmp_path / "workspace.db")
    workspace, _ = store.create_workspace(
        owner="alice",
        request_key="workspace-1",
        title="Experiment A",
    )

    with pytest.raises(ResearchWorkspaceConflict, match="require owner approval"):
        store.bind_approved_agent_suggestion(
            workspace_id=workspace.workspace_id,
            owner="alice",
            object_type=WorkspaceObjectType.CONTRACT,
            object_id="contract-7",
            actor="agent-worker",
            suggestion_ref="advice-1",
        )

    binding, created = store.bind_approved_agent_suggestion(
        workspace_id=workspace.workspace_id,
        owner="alice",
        object_type=WorkspaceObjectType.CONTRACT,
        object_id="contract-7",
        actor="alice",
        suggestion_ref="advice-1",
    )

    assert created is True
    assert binding.source is WorkspaceBindingSource.APPROVED_AGENT_SUGGESTION
    assert binding.source_ref == "advice-1"
    assert binding.created_by == "alice"


def test_same_object_may_be_explicitly_bound_to_multiple_user_workspaces(tmp_path: Path) -> None:
    store = _store(tmp_path / "workspace.db")
    first, _ = store.create_workspace(
        owner="alice",
        request_key="workspace-a",
        title="Algorithm",
    )
    second, _ = store.create_workspace(
        owner="alice",
        request_key="workspace-b",
        title="Benchmark",
    )

    for workspace in (first, second):
        store.bind_user_selected(
            workspace_id=workspace.workspace_id,
            owner="alice",
            object_type=WorkspaceObjectType.RUN,
            object_id="run-shared",
            actor="alice",
        )

    assert {item.workspace_id for item in store.find_object_workspaces(
        owner="alice",
        object_type=WorkspaceObjectType.RUN,
        object_id="run-shared",
    )} == {first.workspace_id, second.workspace_id}


def test_lineage_inheritance_is_explicitly_whitelisted(tmp_path: Path) -> None:
    assert can_inherit_workspace_binding(
        WorkspaceObjectType.RUN,
        WorkspaceObjectType.REMEDIATION_SESSION,
    )
    assert can_inherit_workspace_binding(
        WorkspaceObjectType.REMEDIATION_SESSION,
        WorkspaceObjectType.RUN,
    )
    assert not can_inherit_workspace_binding(
        WorkspaceObjectType.FILE_REFERENCE,
        WorkspaceObjectType.RUN,
    )

    store = _store(tmp_path / "workspace.db")
    workspace, _ = store.create_workspace(
        owner="alice",
        request_key="workspace-1",
        title="Experiment A",
    )
    file_binding, _ = store.bind_user_selected(
        workspace_id=workspace.workspace_id,
        owner="alice",
        object_type=WorkspaceObjectType.FILE_REFERENCE,
        object_id="path:/data/model.pt",
        actor="alice",
    )

    with pytest.raises(ResearchWorkspaceConflict, match="cannot inherit"):
        store.inherit_binding(
            parent_binding_id=file_binding.binding_id,
            owner="alice",
            child_type=WorkspaceObjectType.RUN,
            child_id="run-1",
        )


def test_existing_binding_cannot_silently_change_provenance(tmp_path: Path) -> None:
    store = _store(tmp_path / "workspace.db")
    workspace, _ = store.create_workspace(
        owner="alice",
        request_key="workspace-1",
        title="Experiment A",
    )
    store.bind_user_selected(
        workspace_id=workspace.workspace_id,
        owner="alice",
        object_type=WorkspaceObjectType.RUN,
        object_id="run-1",
        actor="alice",
    )

    with pytest.raises(ResearchWorkspaceConflict, match="different provenance"):
        store.bind_approved_agent_suggestion(
            workspace_id=workspace.workspace_id,
            owner="alice",
            object_type=WorkspaceObjectType.RUN,
            object_id="run-1",
            actor="alice",
            suggestion_ref="advice-2",
        )
