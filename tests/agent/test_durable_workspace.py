from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from pilot107.agent.durable_workspace import DurableWorkspaceEditor
from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.workspace import (
    AgentWorkspaceRecord,
    WorkspaceConflict,
    WorkspacePatch,
    WorkspaceSnapshot,
)
from pilot107.agent.workspace_journal import WorkspaceMutationState


class HardCrash(BaseException):
    pass


@pytest.fixture
def durable(tmp_path: Path):
    store = SQLiteProjectStore(tmp_path / "projects.db")
    project = store.create_project(
        owner="alice",
        origin="blank",
        goal="durable edit",
        request_key="durable-project",
    )
    root = tmp_path / "workspaces" / "alice" / "workspace-durable"
    root.mkdir(parents=True)
    (root / "main.py").write_text("print(1)\n")
    (root / "other.py").write_text("value = 1\n")
    os.chmod(root / "main.py", 0o640)
    os.chmod(root / "other.py", 0o600)
    workspace = AgentWorkspaceRecord(
        workspace_id="workspace-durable",
        project_id=project.project_id,
        owner="alice",
        local_root=str(root),
        snapshot=WorkspaceSnapshot(
            source_ref="/public/home/alice/project",
            digest="a" * 64,
            entries=(),
            captured_at="2026-09-05T00:00:00Z",
        ),
        created_at="2026-09-05T00:00:00Z",
        updated_at="2026-09-05T00:00:00Z",
    )
    store.save_workspace(workspace)
    editor = DurableWorkspaceEditor(
        store=store,
        state_root=tmp_path / "workspace-state",
    )
    return store, workspace, editor


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _journal_rows(store: SQLiteProjectStore) -> list[tuple[str, str, str]]:
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT mutation_id, state, backup_ref "
            "FROM agent_workspace_mutation_journal ORDER BY created_at"
        ).fetchall()
    return [(str(row[0]), str(row[1]), str(row[2])) for row in rows]


def test_durable_patch_commits_one_live_revision_and_preserves_mode(durable) -> None:
    store, workspace, editor = durable
    before = _sha(b"print(1)\n")
    initial = editor.live_store.ensure_head(workspace)

    change_set = editor.apply_patch(
        workspace.workspace_id,
        "alice",
        "main.py",
        before,
        WorkspacePatch(operation="modify", content="print(2)\n"),
    )

    head = editor.live_store.get_head(workspace.workspace_id, owner="alice")
    assert head.live_revision == initial.live_revision + 1
    assert head.live_digest != initial.live_digest
    assert Path(workspace.local_root, "main.py").read_text() == "print(2)\n"
    assert stat.S_IMODE(Path(workspace.local_root, "main.py").stat().st_mode) == 0o640
    assert store.get_change_set(change_set.change_set_id, owner="alice") == change_set
    rows = _journal_rows(store)
    assert [state for _, state, _ in rows] == [WorkspaceMutationState.COMMITTED.value]
    assert not Path(rows[0][2]).exists()


def test_durable_patch_rejects_unjournaled_local_drift(durable) -> None:
    _, workspace, editor = durable
    editor.live_store.ensure_head(workspace)
    Path(workspace.local_root, "other.py").write_text("external = True\n")

    with pytest.raises(WorkspaceConflict, match="drifted"):
        editor.apply_patch(
            workspace.workspace_id,
            "alice",
            "main.py",
            _sha(b"print(1)\n"),
            WorkspacePatch(operation="modify", content="print(2)\n"),
        )

    assert Path(workspace.local_root, "main.py").read_text() == "print(1)\n"


def test_hard_crash_mid_batch_is_rolled_back_on_recovery(durable) -> None:
    store, workspace, editor = durable

    def crash(stage: str) -> None:
        if stage == "after_file:main.py":
            raise HardCrash

    editor.crash_hook = crash
    with pytest.raises(HardCrash):
        editor.apply_patches(
            workspace.workspace_id,
            "alice",
            (
                (
                    "main.py",
                    _sha(b"print(1)\n"),
                    WorkspacePatch(operation="modify", content="print(2)\n"),
                ),
                (
                    "other.py",
                    _sha(b"value = 1\n"),
                    WorkspacePatch(operation="modify", content="value = 2\n"),
                ),
            ),
        )

    rows = _journal_rows(store)
    assert rows[0][1] == WorkspaceMutationState.PREPARED.value
    assert Path(rows[0][2]).is_dir()
    assert Path(workspace.local_root, "main.py").read_text() == "print(2)\n"
    assert Path(workspace.local_root, "other.py").read_text() == "value = 1\n"

    editor.crash_hook = None
    report = editor.recover_workspace(workspace.workspace_id, "alice")

    assert len(report.rolled_back) == 1
    assert not report.conflicted
    assert Path(workspace.local_root, "main.py").read_text() == "print(1)\n"
    assert Path(workspace.local_root, "other.py").read_text() == "value = 1\n"
    head = editor.live_store.get_head(workspace.workspace_id, owner="alice")
    assert head.live_revision == 1
    assert _journal_rows(store)[0][1] == WorkspaceMutationState.ROLLED_BACK.value


def test_hard_crash_after_files_applied_resumes_atomic_commit(durable) -> None:
    store, workspace, editor = durable

    def crash(stage: str) -> None:
        if stage == "after_files_applied":
            raise HardCrash

    editor.crash_hook = crash
    with pytest.raises(HardCrash):
        editor.apply_patch(
            workspace.workspace_id,
            "alice",
            "main.py",
            _sha(b"print(1)\n"),
            WorkspacePatch(operation="modify", content="print(2)\n"),
        )

    assert _journal_rows(store)[0][1] == WorkspaceMutationState.FILES_APPLIED.value
    head = editor.live_store.get_head(workspace.workspace_id, owner="alice")
    assert head.live_revision == 1
    assert Path(workspace.local_root, "main.py").read_text() == "print(2)\n"

    editor.crash_hook = None
    report = editor.recover_workspace(workspace.workspace_id, "alice")

    assert len(report.committed) == 1
    assert not report.conflicted
    head = editor.live_store.get_head(workspace.workspace_id, owner="alice")
    assert head.live_revision == 2
    assert _journal_rows(store)[0][1] == WorkspaceMutationState.COMMITTED.value


def test_recovery_refuses_third_state_and_keeps_backup(durable) -> None:
    store, workspace, editor = durable

    def crash(stage: str) -> None:
        if stage == "after_file:main.py":
            raise HardCrash

    editor.crash_hook = crash
    with pytest.raises(HardCrash):
        editor.apply_patches(
            workspace.workspace_id,
            "alice",
            (
                (
                    "main.py",
                    _sha(b"print(1)\n"),
                    WorkspacePatch(operation="modify", content="print(2)\n"),
                ),
                (
                    "other.py",
                    _sha(b"value = 1\n"),
                    WorkspacePatch(operation="modify", content="value = 2\n"),
                ),
            ),
        )
    Path(workspace.local_root, "main.py").write_text("student edit\n")
    editor.crash_hook = None

    report = editor.recover_workspace(workspace.workspace_id, "alice")

    assert len(report.conflicted) == 1
    rows = _journal_rows(store)
    assert rows[0][1] == WorkspaceMutationState.CONFLICTED.value
    assert Path(rows[0][2]).is_dir()
    assert Path(workspace.local_root, "main.py").read_text() == "student edit\n"


def test_nested_create_rollback_removes_only_created_parent(durable) -> None:
    _, workspace, editor = durable
    existing = Path(workspace.local_root, "existing")
    existing.mkdir()

    def crash(stage: str) -> None:
        if stage == "after_file:new/deep/script.py":
            raise HardCrash

    # Re-bootstrap after the fixture's deliberate pre-head directory creation.
    editor.crash_hook = crash
    with pytest.raises(HardCrash):
        editor.apply_patch(
            workspace.workspace_id,
            "alice",
            "new/deep/script.py",
            None,
            WorkspacePatch(operation="create", content="print('x')\n"),
        )
    editor.crash_hook = None
    report = editor.recover_workspace(workspace.workspace_id, "alice")

    assert len(report.rolled_back) == 1
    assert existing.is_dir()
    assert not Path(workspace.local_root, "new").exists()
