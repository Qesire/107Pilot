from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pilot107.agent.durable_workspace_atomic import AtomicDurableWorkspaceEditor
from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.workspace import AgentWorkspaceRecord, WorkspacePatch, WorkspaceSnapshot


class SimulatedHardCrash(BaseException):
    pass


def _editing(tmp_path: Path) -> tuple[SQLiteProjectStore, AgentWorkspaceRecord]:
    store = SQLiteProjectStore(tmp_path / "pilot107.db")
    project = store.create_project(
        owner="alice",
        origin="blank",
        goal="revision identity",
        request_key="revision-project",
    )
    root = tmp_path / "workspaces" / "alice" / "workspace-revision"
    root.mkdir(parents=True)
    workspace = AgentWorkspaceRecord(
        workspace_id="workspace-revision",
        project_id=project.project_id,
        owner="alice",
        local_root=str(root),
        snapshot=WorkspaceSnapshot(
            source_ref="/__pilot107_blank__/revision-project",
            digest="a" * 64,
            entries=(),
            captured_at="2026-09-05T00:00:00Z",
        ),
        created_at="2026-09-05T00:00:00Z",
        updated_at="2026-09-05T00:00:00Z",
    )
    store.save_workspace(workspace)
    return store, workspace


def _editor(
    store: SQLiteProjectStore,
    tmp_path: Path,
    *,
    crash_stage: str | None = None,
) -> AtomicDurableWorkspaceEditor:
    def crash(stage: str) -> None:
        if stage == crash_stage:
            raise SimulatedHardCrash(stage)

    return AtomicDurableWorkspaceEditor(
        store=store,
        state_root=tmp_path / "state",
        crash_hook=crash if crash_stage is not None else None,
    )


def _create() -> tuple[tuple[str, str | None, WorkspacePatch], ...]:
    return (("main.py", None, WorkspacePatch(operation="create", content="value = 1\n")),)


def test_same_patch_can_run_after_precommit_crash_is_rolled_back(tmp_path: Path) -> None:
    store, workspace = _editing(tmp_path)
    crashing = _editor(store, tmp_path, crash_stage="after_files_applied")
    with pytest.raises(SimulatedHardCrash):
        crashing.apply_patches(workspace.workspace_id, "alice", _create())

    recovery = _editor(store, tmp_path)
    report = recovery.recover_workspace(workspace.workspace_id, "alice")
    assert len(report.rolled_back) == 1
    assert store.list_change_sets(workspace.project_id, owner="alice") == []

    committed = recovery.apply_patches(workspace.workspace_id, "alice", _create())

    assert Path(workspace.local_root, "main.py").read_text() == "value = 1\n"
    assert store.get_change_set(committed.change_set_id, owner="alice") == committed
    assert recovery.live_store.get_head(workspace.workspace_id, owner="alice").live_revision == 2


def test_repeated_content_on_later_revision_gets_distinct_changeset_identity(
    tmp_path: Path,
) -> None:
    store, workspace = _editing(tmp_path)
    editor = _editor(store, tmp_path)

    first = editor.apply_patches(workspace.workspace_id, "alice", _create())
    source_digest = hashlib.sha256(b"value = 1\n").hexdigest()
    deleted = editor.apply_patches(
        workspace.workspace_id,
        "alice",
        (("main.py", source_digest, WorkspacePatch(operation="delete", content=None)),),
    )
    repeated = editor.apply_patches(workspace.workspace_id, "alice", _create())

    assert first.digest == repeated.digest
    assert first.change_set_id != repeated.change_set_id
    assert deleted.change_set_id not in {first.change_set_id, repeated.change_set_id}
    assert editor.live_store.get_head(workspace.workspace_id, owner="alice").live_revision == 4
