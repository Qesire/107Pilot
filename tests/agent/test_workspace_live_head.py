from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.workspace import (
    AgentWorkspaceRecord,
    WorkspaceChangeSet,
    WorkspaceSnapshot,
)
from pilot107.agent.workspace_live import (
    WORKSPACE_LIVE_HEAD_MIGRATION,
    SQLiteWorkspaceLiveStore,
    WorkspaceLiveConflict,
    capture_workspace_live_digest,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 5, 0, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def live_workspace(tmp_path: Path):
    project_store = SQLiteProjectStore(tmp_path / "projects.db")
    project = project_store.create_project(
        owner="alice",
        origin="blank",
        goal="live workspace",
        request_key="workspace-live-project",
    )
    root = tmp_path / "workspaces" / "alice" / "workspace-live"
    root.mkdir(parents=True)
    (root / "main.py").write_text("print(1)\n")
    (root / "scripts").mkdir()
    (root / "scripts" / "run.sh").write_text("#!/bin/sh\ntrue\n")
    workspace = AgentWorkspaceRecord(
        workspace_id="workspace-live",
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
    project_store.save_workspace(workspace)
    clock = MutableClock()
    live_store = SQLiteWorkspaceLiveStore(project_store.db_path, clock=clock)
    return project_store, workspace, live_store, clock


def test_live_head_bootstraps_from_current_local_tree_without_mutating_snapshot(
    live_workspace,
) -> None:
    _, workspace, store, _ = live_workspace
    expected = capture_workspace_live_digest(workspace)

    head = store.ensure_head(workspace)

    assert head.live_revision == 1
    assert head.live_digest == expected
    assert head.live_digest != workspace.snapshot.digest
    assert head.base_snapshot_digest == workspace.snapshot.digest
    assert head.writer_id is None
    assert head.fencing_token == 0
    assert workspace.snapshot.digest == "a" * 64


def test_live_manifest_observes_content_and_permission_changes(live_workspace) -> None:
    _, workspace, _, _ = live_workspace
    path = Path(workspace.local_root, "main.py")
    first = capture_workspace_live_digest(workspace)

    path.write_text("print(2)\n")
    second = capture_workspace_live_digest(workspace)
    assert second != first

    path.chmod(0o600)
    third = capture_workspace_live_digest(workspace)
    assert third != second


def test_single_writer_fence_and_revision_cas(live_workspace) -> None:
    _, workspace, store, _ = live_workspace
    head = store.ensure_head(workspace)
    lease = store.claim_writer(
        workspace.workspace_id,
        owner="alice",
        writer_id="agent:session-1:turn-1",
        lease_seconds=120,
    )

    next_digest = "b" * 64
    updated = store.compare_and_swap(
        lease,
        expected_revision=head.live_revision,
        expected_digest=head.live_digest,
        new_digest=next_digest,
    )

    assert updated.live_revision == 2
    assert updated.live_digest == next_digest
    assert updated.fencing_token == lease.fencing_token

    with pytest.raises(WorkspaceLiveConflict) as error:
        store.compare_and_swap(
            lease,
            expected_revision=head.live_revision,
            expected_digest=head.live_digest,
            new_digest="c" * 64,
        )
    assert error.value.current is not None
    assert error.value.current.live_revision == 2


def test_second_writer_waits_until_lease_expiry_and_fences_old_writer(live_workspace) -> None:
    _, workspace, store, clock = live_workspace
    head = store.ensure_head(workspace)
    first = store.claim_writer(
        workspace.workspace_id,
        owner="alice",
        writer_id="worker-a",
        lease_seconds=30,
    )

    with pytest.raises(WorkspaceLiveConflict, match="another writer"):
        store.claim_writer(
            workspace.workspace_id,
            owner="alice",
            writer_id="worker-b",
            lease_seconds=30,
        )

    clock.advance(31)
    second = store.claim_writer(
        workspace.workspace_id,
        owner="alice",
        writer_id="worker-b",
        lease_seconds=30,
    )
    assert second.fencing_token > first.fencing_token

    with pytest.raises(WorkspaceLiveConflict):
        store.compare_and_swap(
            first,
            expected_revision=head.live_revision,
            expected_digest=head.live_digest,
            new_digest="d" * 64,
        )

    updated = store.compare_and_swap(
        second,
        expected_revision=head.live_revision,
        expected_digest=head.live_digest,
        new_digest="e" * 64,
    )
    assert updated.live_revision == 2


def test_writer_renewal_requires_current_unexpired_fence(live_workspace) -> None:
    _, workspace, store, clock = live_workspace
    store.ensure_head(workspace)
    lease = store.claim_writer(
        workspace.workspace_id,
        owner="alice",
        writer_id="worker-a",
        lease_seconds=30,
    )

    clock.advance(10)
    renewed = store.renew_writer(lease, lease_seconds=30)
    assert renewed.fencing_token == lease.fencing_token

    clock.advance(31)
    with pytest.raises(WorkspaceLiveConflict):
        store.renew_writer(renewed, lease_seconds=30)


def test_live_head_migration_is_additive_and_workspace_ontology_stays_immutable(
    live_workspace,
) -> None:
    project_store, workspace, store, _ = live_workspace
    store.ensure_head(workspace)

    with project_store.connect() as connection:
        migrations = {
            str(row[0])
            for row in connection.execute("SELECT migration_id FROM schema_migrations").fetchall()
        }
    assert "006b.001.agent_experiment_projects" in migrations
    assert "006b.002.agent_workspaces" in migrations
    assert WORKSPACE_LIVE_HEAD_MIGRATION.migration_id in migrations

    workspace_fields = {item.name for item in fields(AgentWorkspaceRecord)}
    change_set_fields = {item.name for item in fields(WorkspaceChangeSet)}
    assert "live_revision" not in workspace_fields
    assert "live_digest" not in workspace_fields
    assert "base_snapshot_digest" in change_set_fields
