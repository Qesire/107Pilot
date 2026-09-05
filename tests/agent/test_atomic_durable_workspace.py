from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pilot107.agent.durable_workspace_atomic import AtomicDurableWorkspaceEditor
from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.sandbox import SandboxExecutor
from pilot107.agent.workspace import WorkspacePatch
from pilot107.services.project_agent_service import ProjectAgentService


class SimulatedHardCrash(BaseException):
    pass


def _workspace(tmp_path: Path):
    database = tmp_path / "pilot107.db"
    store = SQLiteProjectStore(database)
    service = ProjectAgentService(
        store=store,
        workspace_root=tmp_path / "agent-workspaces",
        sandbox=SandboxExecutor(store=store),
    )
    view = service.create_project(
        owner="alice",
        origin="blank",
        goal="pin atomic workspace boundary",
        request_key="atomic-boundary-project",
    )
    assert isinstance(service.editor, AtomicDurableWorkspaceEditor)
    return database, store, view.workspace


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
        state_root=tmp_path / "atomic-state",
        crash_hook=crash if crash_stage is not None else None,
    )


def _create_patch() -> tuple[tuple[str, str | None, WorkspacePatch], ...]:
    return (("main.py", None, WorkspacePatch(operation="create", content="value = 1\n")),)


def test_finalize_database_failure_rolls_back_files_without_changeset(tmp_path: Path) -> None:
    database, store, workspace = _workspace(tmp_path)
    editor = _editor(store, tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_atomic_changeset
            BEFORE INSERT ON agent_workspace_changesets
            BEGIN
                SELECT RAISE(ABORT, 'reject atomic changeset');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="reject atomic changeset"):
        editor.apply_patches(workspace.workspace_id, "alice", _create_patch())

    assert not Path(workspace.local_root, "main.py").exists()
    assert store.list_change_sets(workspace.project_id, owner="alice") == []
    head = editor.live_store.get_head(workspace.workspace_id, owner="alice")
    assert head.live_revision == 1
    with sqlite3.connect(database) as connection:
        journal = connection.execute(
            "SELECT state, change_set_id FROM agent_workspace_mutation_journal"
        ).fetchone()
    assert journal == ("rolled_back", None)


def test_hard_crash_after_files_applied_recovers_without_ghost_changeset(
    tmp_path: Path,
) -> None:
    database, store, workspace = _workspace(tmp_path)
    crashing = _editor(store, tmp_path, crash_stage="after_files_applied")

    with pytest.raises(SimulatedHardCrash):
        crashing.apply_patches(workspace.workspace_id, "alice", _create_patch())

    assert Path(workspace.local_root, "main.py").read_text() == "value = 1\n"
    assert store.list_change_sets(workspace.project_id, owner="alice") == []
    with sqlite3.connect(database) as connection:
        journal = connection.execute(
            "SELECT state, change_set_id FROM agent_workspace_mutation_journal"
        ).fetchone()
    assert journal == ("prepared", None)

    recovered = _editor(store, tmp_path).recover_workspace(workspace.workspace_id, "alice")

    assert len(recovered.rolled_back) == 1
    assert not Path(workspace.local_root, "main.py").exists()
    assert store.list_change_sets(workspace.project_id, owner="alice") == []
    head = crashing.live_store.get_head(workspace.workspace_id, owner="alice")
    assert head.live_revision == 1
    with sqlite3.connect(database) as connection:
        journal = connection.execute(
            "SELECT state, change_set_id FROM agent_workspace_mutation_journal"
        ).fetchone()
    assert journal == ("rolled_back", None)


def test_hard_crash_after_commit_preserves_revision_and_changeset(tmp_path: Path) -> None:
    database, store, workspace = _workspace(tmp_path)
    crashing = _editor(store, tmp_path, crash_stage="after_commit")

    with pytest.raises(SimulatedHardCrash):
        crashing.apply_patches(workspace.workspace_id, "alice", _create_patch())

    assert Path(workspace.local_root, "main.py").read_text() == "value = 1\n"
    changes = store.list_change_sets(workspace.project_id, owner="alice")
    assert len(changes) == 1
    assert changes[0].state.value == "draft"
    head = crashing.live_store.get_head(workspace.workspace_id, owner="alice")
    assert head.live_revision == 2
    with sqlite3.connect(database) as connection:
        journal = connection.execute(
            """
            SELECT state, change_set_id, to_revision, to_digest
            FROM agent_workspace_mutation_journal
            """
        ).fetchone()
    assert journal is not None
    assert journal[0] == "committed"
    assert journal[1] == changes[0].change_set_id
    assert journal[2] == 2
    assert journal[3] == head.live_digest

    report = _editor(store, tmp_path).recover_workspace(workspace.workspace_id, "alice")
    assert report.committed == ()
    assert report.rolled_back == ()
    assert report.conflicted == ()
