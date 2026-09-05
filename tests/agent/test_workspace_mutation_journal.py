from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.workspace import AgentWorkspaceRecord, WorkspaceSnapshot
from pilot107.agent.workspace_journal import (
    SQLiteWorkspaceMutationJournalStore,
    WorkspaceMutationFile,
    WorkspaceMutationState,
)
from pilot107.agent.workspace_live import (
    SQLiteWorkspaceLiveStore,
    WorkspaceLiveConflict,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 5, 1, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _workspace(
    store: SQLiteProjectStore,
    tmp_path: Path,
    *,
    suffix: str,
) -> AgentWorkspaceRecord:
    project = store.create_project(
        owner="alice",
        origin="blank",
        goal=f"journal {suffix}",
        request_key=f"journal-project-{suffix}",
    )
    root = tmp_path / "workspaces" / "alice" / f"workspace-{suffix}"
    root.mkdir(parents=True)
    (root / "main.py").write_text("print(1)\n")
    workspace = AgentWorkspaceRecord(
        workspace_id=f"workspace-{suffix}",
        project_id=project.project_id,
        owner="alice",
        local_root=str(root),
        snapshot=WorkspaceSnapshot(
            source_ref=f"/public/home/alice/{suffix}",
            digest=hashlib.sha256(f"snapshot:{suffix}".encode()).hexdigest(),
            entries=(),
            captured_at="2026-09-05T01:00:00Z",
        ),
        created_at="2026-09-05T01:00:00Z",
        updated_at="2026-09-05T01:00:00Z",
    )
    store.save_workspace(workspace)
    return workspace


@pytest.fixture
def journal_env(tmp_path: Path):
    project_store = SQLiteProjectStore(tmp_path / "projects.db")
    workspace = _workspace(project_store, tmp_path, suffix="journal")
    other = _workspace(project_store, tmp_path, suffix="other")
    clock = MutableClock()
    live_store = SQLiteWorkspaceLiveStore(project_store.db_path, clock=clock)
    journal_store = SQLiteWorkspaceMutationJournalStore(project_store.db_path, clock=clock)
    head = live_store.ensure_head(workspace)
    other_head = live_store.ensure_head(other)
    return project_store, workspace, other, live_store, journal_store, clock, head, other_head


def _file_plan() -> WorkspaceMutationFile:
    return WorkspaceMutationFile(
        path="main.py",
        operation="modify",
        before_sha256=hashlib.sha256(b"print(1)\n").hexdigest(),
        after_sha256=hashlib.sha256(b"print(2)\n").hexdigest(),
    )


def test_prepare_is_request_key_idempotent_and_content_bound(journal_env) -> None:
    _, workspace, _, live_store, journal_store, _, head, _ = journal_env
    lease = live_store.claim_writer(
        workspace.workspace_id,
        owner="alice",
        writer_id="worker-a",
        lease_seconds=60,
    )
    head = live_store.get_head(workspace.workspace_id, owner="alice")

    first = journal_store.prepare(
        head=head,
        lease=lease,
        request_key="patch-request-1",
        files=(_file_plan(),),
        backup_ref="workspace-backup:patch-request-1",
    )
    replay = journal_store.prepare(
        head=head,
        lease=lease,
        request_key="patch-request-1",
        files=(_file_plan(),),
        backup_ref="workspace-backup:replacement-location",
    )

    assert replay == first
    assert first.state is WorkspaceMutationState.PREPARED

    conflicting = WorkspaceMutationFile(
        path="main.py",
        operation="modify",
        before_sha256=_file_plan().before_sha256,
        after_sha256="f" * 64,
    )
    with pytest.raises(WorkspaceLiveConflict, match="different intent"):
        journal_store.prepare(
            head=head,
            lease=lease,
            request_key="patch-request-1",
            files=(conflicting,),
            backup_ref="workspace-backup:patch-request-1",
        )


def test_files_applied_then_commit_atomically_advances_live_head(journal_env) -> None:
    _, workspace, _, live_store, journal_store, _, _, _ = journal_env
    lease = live_store.claim_writer(
        workspace.workspace_id,
        owner="alice",
        writer_id="worker-a",
        lease_seconds=60,
    )
    head = live_store.get_head(workspace.workspace_id, owner="alice")
    journal = journal_store.prepare(
        head=head,
        lease=lease,
        request_key="patch-request-2",
        files=(_file_plan(),),
        backup_ref="workspace-backup:patch-request-2",
    )
    applied = journal_store.mark_files_applied(
        journal.mutation_id,
        owner="alice",
        lease=lease,
        to_digest="b" * 64,
    )

    assert applied.state is WorkspaceMutationState.FILES_APPLIED
    assert live_store.get_head(workspace.workspace_id, owner="alice").live_revision == 1

    committed = journal_store.commit(journal.mutation_id, owner="alice", lease=lease)
    live = live_store.get_head(workspace.workspace_id, owner="alice")
    assert committed.state is WorkspaceMutationState.COMMITTED
    assert committed.to_revision == 2
    assert live.live_revision == 2
    assert live.live_digest == "b" * 64
    assert journal_store.list_open(workspace.workspace_id, owner="alice") == []


def test_stale_writer_cannot_checkpoint_or_commit_mutation(journal_env) -> None:
    _, workspace, _, live_store, journal_store, clock, _, _ = journal_env
    first = live_store.claim_writer(
        workspace.workspace_id,
        owner="alice",
        writer_id="worker-a",
        lease_seconds=30,
    )
    head = live_store.get_head(workspace.workspace_id, owner="alice")
    journal = journal_store.prepare(
        head=head,
        lease=first,
        request_key="patch-request-3",
        files=(_file_plan(),),
        backup_ref="workspace-backup:patch-request-3",
    )

    clock.advance(31)
    live_store.claim_writer(
        workspace.workspace_id,
        owner="alice",
        writer_id="worker-b",
        lease_seconds=30,
    )

    with pytest.raises(WorkspaceLiveConflict):
        journal_store.mark_files_applied(
            journal.mutation_id,
            owner="alice",
            lease=first,
            to_digest="c" * 64,
        )


def test_foreign_workspace_lease_cannot_commit_journal(journal_env) -> None:
    _, workspace, other, live_store, journal_store, _, _, _ = journal_env
    first = live_store.claim_writer(
        workspace.workspace_id,
        owner="alice",
        writer_id="shared-writer-name",
        lease_seconds=60,
    )
    head = live_store.get_head(workspace.workspace_id, owner="alice")
    journal = journal_store.prepare(
        head=head,
        lease=first,
        request_key="patch-request-4",
        files=(_file_plan(),),
        backup_ref="workspace-backup:patch-request-4",
    )
    journal_store.mark_files_applied(
        journal.mutation_id,
        owner="alice",
        lease=first,
        to_digest="d" * 64,
    )
    foreign = live_store.claim_writer(
        other.workspace_id,
        owner="alice",
        writer_id="shared-writer-name",
        lease_seconds=60,
    )

    with pytest.raises(WorkspaceLiveConflict, match="does not own"):
        journal_store.commit(journal.mutation_id, owner="alice", lease=foreign)


def test_rollback_requires_observed_filesystem_digest_to_match_prepared_head(journal_env) -> None:
    _, workspace, _, live_store, journal_store, _, _, _ = journal_env
    lease = live_store.claim_writer(
        workspace.workspace_id,
        owner="alice",
        writer_id="worker-a",
        lease_seconds=60,
    )
    head = live_store.get_head(workspace.workspace_id, owner="alice")
    journal = journal_store.prepare(
        head=head,
        lease=lease,
        request_key="patch-request-5",
        files=(_file_plan(),),
        backup_ref="workspace-backup:patch-request-5",
    )

    with pytest.raises(WorkspaceLiveConflict, match="observed live digest"):
        journal_store.mark_rolled_back(
            journal.mutation_id,
            owner="alice",
            observed_live_digest="e" * 64,
        )

    rolled_back = journal_store.mark_rolled_back(
        journal.mutation_id,
        owner="alice",
        observed_live_digest=head.live_digest,
    )
    assert rolled_back.state is WorkspaceMutationState.ROLLED_BACK
