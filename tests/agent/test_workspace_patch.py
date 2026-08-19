from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.workspace import (
    AgentWorkspaceRecord,
    WorkspaceConflict,
    WorkspaceEditor,
    WorkspacePatch,
    WorkspacePolicyError,
    WorkspaceSnapshot,
)


@pytest.fixture
def editing(tmp_path: Path):
    store = SQLiteProjectStore(tmp_path / "projects.db")
    project = store.create_project(
        owner="alice",
        origin="blank",
        goal="edit code",
        request_key="patch-project",
    )
    root = tmp_path / "workspaces" / "alice" / "workspace-patch"
    root.mkdir(parents=True)
    (root / "main.py").write_text("print(1)\n")
    workspace = AgentWorkspaceRecord(
        workspace_id="workspace-patch",
        project_id=project.project_id,
        owner="alice",
        local_root=str(root),
        snapshot=WorkspaceSnapshot(
            source_ref="/public/home/alice/project",
            digest="a" * 64,
            entries=(),
            captured_at="2026-08-19T00:00:00Z",
        ),
        created_at="2026-08-19T00:00:00Z",
        updated_at="2026-08-19T00:00:00Z",
    )
    store.save_workspace(workspace)
    return store, workspace, WorkspaceEditor(store=store)


def test_patch_requires_expected_source_digest(editing) -> None:
    _, workspace, editor = editing

    with pytest.raises(WorkspaceConflict):
        editor.apply_patch(
            workspace.workspace_id,
            "alice",
            "main.py",
            "0" * 64,
            WorkspacePatch(operation="modify", content="print(2)\n"),
        )

    assert Path(workspace.local_root, "main.py").read_text() == "print(1)\n"


def test_patch_persists_deterministic_change_set_and_diff(editing) -> None:
    store, workspace, editor = editing
    before = hashlib.sha256(b"print(1)\n").hexdigest()

    change_set = editor.apply_patch(
        workspace.workspace_id,
        "alice",
        "main.py",
        before,
        WorkspacePatch(operation="modify", content="print(2)\n"),
    )

    assert change_set.base_snapshot_digest == workspace.snapshot.digest
    assert change_set.files[0].before_sha256 == before
    assert change_set.files[0].after_sha256 == hashlib.sha256(b"print(2)\n").hexdigest()
    assert store.get_change_set(change_set.change_set_id, owner="alice") == change_set
    assert editor.diff(change_set.change_set_id, "alice") == (
        "--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-print(1)\n+print(2)\n"
    )


@pytest.mark.parametrize("relative", ["../escape.py", "/tmp/escape.py", "a/../../b.py"])
def test_patch_rejects_path_traversal(editing, relative: str) -> None:
    _, workspace, editor = editing

    with pytest.raises(WorkspacePolicyError):
        editor.apply_patch(
            workspace.workspace_id,
            "alice",
            relative,
            None,
            WorkspacePatch(operation="create", content="unsafe"),
        )


def test_patch_rejects_symlink_targets(editing, tmp_path: Path) -> None:
    _, workspace, editor = editing
    target = tmp_path / "outside.py"
    target.write_text("secret")
    Path(workspace.local_root, "link.py").symlink_to(target)

    with pytest.raises(WorkspacePolicyError, match="symlink"):
        editor.apply_patch(
            workspace.workspace_id,
            "alice",
            "link.py",
            hashlib.sha256(b"secret").hexdigest(),
            WorkspacePatch(operation="modify", content="changed"),
        )


def test_patch_rejects_oversized_diff_before_mutation(editing) -> None:
    store, workspace, _ = editing
    editor = WorkspaceEditor(store=store, max_diff_bytes=32)
    before = hashlib.sha256(b"print(1)\n").hexdigest()

    with pytest.raises(WorkspacePolicyError, match="diff"):
        editor.apply_patch(
            workspace.workspace_id,
            "alice",
            "main.py",
            before,
            WorkspacePatch(operation="modify", content="x" * 200),
        )

    assert Path(workspace.local_root, "main.py").read_text() == "print(1)\n"
