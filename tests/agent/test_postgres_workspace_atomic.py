from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pilot107.agent.postgres_project_store import PostgresProjectStore
from pilot107.agent.postgres_workspace_atomic import PostgresAtomicDurableWorkspaceEditor
from pilot107.agent.workspace import AgentWorkspaceRecord, WorkspacePatch, WorkspaceSnapshot
from pilot107.agent.workspace_journal import WorkspaceMutationState


PG_ENABLED = bool(
    os.environ.get("PILOT107_TEST_POSTGRES_DSN")
    and os.environ.get("PILOT107_TEST_POSTGRES_ALLOW_RESET") == "1"
)


class SimulatedProcessCrash(BaseException):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _reset(editor: PostgresAtomicDurableWorkspaceEditor) -> None:
    with editor.journal_store.connect() as connection:
        connection.execute(
            "TRUNCATE agent_workspace_mutation_journal, agent_workspace_live_heads, "
            "agent_workspace_changesets, agent_workspaces, agent_experiment_projects "
            "RESTART IDENTITY CASCADE"
        )


def _workspace(
    store: PostgresProjectStore,
    *,
    root: Path,
    request_key: str,
) -> AgentWorkspaceRecord:
    project = store.create_project(
        owner="alice",
        origin="blank",
        goal="atomic Workspace mutation",
        request_key=request_key,
    )
    root.mkdir(parents=True, exist_ok=True)
    now = _now()
    return store.save_workspace(
        AgentWorkspaceRecord(
            workspace_id=f"workspace-{request_key}",
            project_id=project.project_id,
            owner="alice",
            local_root=str(root),
            snapshot=WorkspaceSnapshot(
                source_ref=f"/__pilot107_blank__/{request_key}",
                digest=("a" * 63) + ("1" if request_key.endswith("1") else "2"),
                entries=(),
                captured_at=now,
            ),
            created_at=now,
            updated_at=now,
        )
    )


@pytest.mark.skipif(
    not PG_ENABLED,
    reason="set a dedicated PostgreSQL DSN and explicit reset opt-in",
)
def test_postgres_workspace_change_set_head_and_receipt_commit_together(
    tmp_path: Path,
) -> None:
    dsn = os.environ["PILOT107_TEST_POSTGRES_DSN"]
    store = PostgresProjectStore(dsn)
    editor = PostgresAtomicDurableWorkspaceEditor(
        store=store,
        state_root=tmp_path / "agent-workspace-state",
    )
    _reset(editor)
    root = tmp_path / "agent-workspaces" / "alice" / "workspace-pg-1"
    workspace = _workspace(store, root=root, request_key="pg-1")
    before = editor.live_store.ensure_head(workspace)

    change_set = editor.apply_patches(
        workspace.workspace_id,
        "alice",
        (("train.py", None, WorkspacePatch(operation="create", content="x = 1\n")),),
    )

    after = editor.live_store.get_head(workspace.workspace_id, owner="alice")
    stored = store.get_change_set(change_set.change_set_id, owner="alice")
    with editor.journal_store.connect() as connection:
        receipt = connection.execute(
            """
            SELECT state, change_set_id, from_revision, to_revision,
                   from_digest, to_digest
            FROM agent_workspace_mutation_journal
            WHERE workspace_id = %s
            """,
            (workspace.workspace_id,),
        ).fetchone()

    assert (root / "train.py").read_text() == "x = 1\n"
    assert stored == change_set
    assert after.live_revision == before.live_revision + 1
    assert after.live_digest != before.live_digest
    assert receipt is not None
    assert receipt["state"] == "committed"
    assert receipt["change_set_id"] == change_set.change_set_id
    assert int(receipt["from_revision"]) == before.live_revision
    assert int(receipt["to_revision"]) == after.live_revision
    assert receipt["from_digest"] == before.live_digest
    assert receipt["to_digest"] == after.live_digest


@pytest.mark.skipif(
    not PG_ENABLED,
    reason="set a dedicated PostgreSQL DSN and explicit reset opt-in",
)
def test_postgres_workspace_crash_before_db_finalize_recovers_to_old_revision(
    tmp_path: Path,
) -> None:
    dsn = os.environ["PILOT107_TEST_POSTGRES_DSN"]
    store = PostgresProjectStore(dsn)

    def crash(point: str) -> None:
        if point == "after_files_applied":
            raise SimulatedProcessCrash(point)

    editor = PostgresAtomicDurableWorkspaceEditor(
        store=store,
        state_root=tmp_path / "agent-workspace-state",
        crash_hook=crash,
    )
    _reset(editor)
    root = tmp_path / "agent-workspaces" / "alice" / "workspace-pg-2"
    workspace = _workspace(store, root=root, request_key="pg-2")
    before = editor.live_store.ensure_head(workspace)

    with pytest.raises(SimulatedProcessCrash):
        editor.apply_patches(
            workspace.workspace_id,
            "alice",
            (("train.py", None, WorkspacePatch(operation="create", content="x = 2\n")),),
        )

    # BaseException bypasses the editor's synchronous rollback path: this is the
    # durable restart boundary. Files changed, but no authoritative DB publish occurred.
    assert (root / "train.py").read_text() == "x = 2\n"
    assert store.list_change_sets(workspace.project_id, owner="alice") == []
    interim_head = editor.live_store.get_head(workspace.workspace_id, owner="alice")
    assert interim_head.live_revision == before.live_revision
    assert interim_head.live_digest == before.live_digest
    open_journal = editor.journal_store.list_open(workspace.workspace_id, owner="alice")
    assert len(open_journal) == 1
    assert open_journal[0].state is WorkspaceMutationState.PREPARED

    restarted = PostgresAtomicDurableWorkspaceEditor(
        store=store,
        state_root=tmp_path / "agent-workspace-state",
    )
    report = restarted.recover_workspace(workspace.workspace_id, "alice")
    recovered_head = restarted.live_store.get_head(workspace.workspace_id, owner="alice")
    receipt = restarted.journal_store.get(open_journal[0].mutation_id, owner="alice")

    assert report.rolled_back == (open_journal[0].mutation_id,)
    assert report.committed == ()
    assert report.conflicted == ()
    assert not (root / "train.py").exists()
    assert recovered_head.live_revision == before.live_revision
    assert recovered_head.live_digest == before.live_digest
    assert receipt.state is WorkspaceMutationState.ROLLED_BACK
    assert store.list_change_sets(workspace.project_id, owner="alice") == []
