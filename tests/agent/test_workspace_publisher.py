from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.publisher import (
    WorkspacePublicationState,
    WorkspacePublisher,
)
from pilot107.agent.sandbox import SandboxExecutor
from pilot107.agent.workspace import (
    AgentWorkspaceRecord,
    WorkspaceApproval,
    WorkspaceChangeSet,
    WorkspaceChangeSetState,
    WorkspaceFileChange,
    WorkspaceSnapshot,
)
from pilot107.api.project_agent_routes import ProjectAgentRoutes
from pilot107.core.identity import UserIdentity
from pilot107.services.project_agent_service import ProjectAgentService


class MemoryPublicationRelay:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)
        self.directories: set[str] = set()
        self.crash_after_commit_once = False

    def path_sha256(self, *, path: str, owner: str, timeout_seconds: float = 30.0) -> str | None:
        del owner, timeout_seconds
        content = self.files.get(path)
        return None if content is None else hashlib.sha256(content).hexdigest()

    def make_dir(self, *, path: str, owner: str, timeout_seconds: float = 30.0) -> None:
        del owner, timeout_seconds
        self.directories.add(path)

    def write_bytes_chunk(
        self,
        *,
        path: str,
        data_b64: str,
        offset: int,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> int:
        del owner, timeout_seconds
        content = base64.b64decode(data_b64, validate=True)
        if offset == 0:
            self.files[path] = content
        elif offset == -1:
            self.files[path] = self.files.get(path, b"") + content
        else:
            raise ValueError("invalid offset")
        return len(self.files[path])

    def compare_and_swap_file(
        self,
        *,
        staged_path: str,
        target_path: str,
        expected_sha256: str | None,
        desired_sha256: str,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> str:
        del owner, timeout_seconds
        current = self.path_sha256(path=target_path, owner="alice")
        if current == desired_sha256:
            return "already_committed"
        if current != expected_sha256:
            return "conflict"
        staged = self.files[staged_path]
        if hashlib.sha256(staged).hexdigest() != desired_sha256:
            return "staged_digest_mismatch"
        self.files[target_path] = staged
        del self.files[staged_path]
        if self.crash_after_commit_once:
            self.crash_after_commit_once = False
            raise RuntimeError("crash after rename")
        return "committed"

    def compare_and_delete_file(
        self,
        *,
        target_path: str,
        expected_sha256: str,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> str:
        del owner, timeout_seconds
        current = self.path_sha256(path=target_path, owner="alice")
        if current is None:
            return "already_committed"
        if current != expected_sha256:
            return "conflict"
        del self.files[target_path]
        return "committed"

    def remove_path(self, *, path: str, owner: str, timeout_seconds: float = 30.0) -> None:
        del owner, timeout_seconds
        prefix = path.rstrip("/") + "/"
        for candidate in list(self.files):
            if candidate == path or candidate.startswith(prefix):
                del self.files[candidate]


@pytest.fixture
def publisher(
    tmp_path: Path,
) -> tuple[WorkspacePublisher, MemoryPublicationRelay, WorkspaceChangeSet]:
    before = b"print('old')\n"
    after = b"print('new')\n"
    target_root = "/public/home/alice/project"
    store = SQLiteProjectStore(tmp_path / "projects.db")
    project = store.create_project(
        owner="alice",
        origin="existing",
        goal="publish code",
        request_key="publisher-project",
    )
    local_root = tmp_path / "workspaces" / "alice" / "workspace-publish"
    local_root.mkdir(parents=True)
    (local_root / "main.py").write_bytes(after)
    workspace = AgentWorkspaceRecord(
        workspace_id="workspace-publish",
        project_id=project.project_id,
        owner="alice",
        local_root=str(local_root),
        snapshot=WorkspaceSnapshot(
            source_ref=target_root,
            digest="a" * 64,
            entries=(),
            captured_at="2026-08-19T00:00:00Z",
        ),
        created_at="2026-08-19T00:00:00Z",
        updated_at="2026-08-19T00:00:00Z",
    )
    store.save_workspace(workspace)
    change_set = _change_set(
        project_id=project.project_id,
        workspace_id=workspace.workspace_id,
        before=before,
        after=after,
    )
    store.save_change_set(change_set, diff_text="--- a/main.py\n+++ b/main.py\n")
    relay = MemoryPublicationRelay({f"{target_root}/main.py": before})
    return (
        WorkspacePublisher(
            store=store,
            relay=relay,
            owner_roots=("/public/home/{owner}",),
        ),
        relay,
        change_set,
    )


def test_publish_detects_changed_remote_source(
    publisher: tuple[WorkspacePublisher, MemoryPublicationRelay, WorkspaceChangeSet],
) -> None:
    service, remote, change_set = publisher
    service.prepare(change_set.change_set_id, actor="alice")
    remote.files["/public/home/alice/project/main.py"] = b"user changed it\n"

    result = service.publish(change_set.change_set_id, actor="alice")

    assert result.state is WorkspacePublicationState.CONFLICTED
    assert result.error_code == "workspace_conflict"
    assert remote.files["/public/home/alice/project/main.py"] == b"user changed it\n"


def test_retry_reconciles_rename_completed_before_progress_persisted(
    publisher: tuple[WorkspacePublisher, MemoryPublicationRelay, WorkspaceChangeSet],
) -> None:
    service, remote, change_set = publisher
    prepared = service.prepare(change_set.change_set_id, actor="alice")
    remote.crash_after_commit_once = True

    with pytest.raises(RuntimeError, match="after rename"):
        service.publish(change_set.change_set_id, actor="alice")
    recovered = service.reconcile(change_set.change_set_id, actor="alice")

    assert recovered.state is WorkspacePublicationState.PUBLISHED
    assert recovered.files[0].state == "committed"
    assert remote.files["/public/home/alice/project/main.py"] == b"print('new')\n"
    assert service.reconcile(change_set.change_set_id, actor="alice") == recovered
    assert prepared.approved_digest == change_set.digest


def test_prepare_rejects_stale_approval_digest(
    publisher: tuple[WorkspacePublisher, MemoryPublicationRelay, WorkspaceChangeSet],
) -> None:
    service, _remote, change_set = publisher
    stale = replace(
        change_set,
        approval=WorkspaceApproval(
            actor="alice",
            approved_digest="f" * 64,
            approved_at="2026-08-19T00:00:00Z",
        ),
    )
    service.store.replace_change_set(stale, expected_version=change_set.version)

    with pytest.raises(ValueError, match="approval digest"):
        service.prepare(change_set.change_set_id, actor="alice")


def test_owner_isolation_masks_publication(
    publisher: tuple[WorkspacePublisher, MemoryPublicationRelay, WorkspaceChangeSet],
) -> None:
    service, _remote, change_set = publisher

    with pytest.raises(KeyError):
        service.prepare(change_set.change_set_id, actor="bob")


def test_publish_route_binds_actor_version_and_exact_digest(
    publisher: tuple[WorkspacePublisher, MemoryPublicationRelay, WorkspaceChangeSet],
) -> None:
    workspace_publisher, _remote, change_set = publisher
    reviewable = workspace_publisher.store.replace_change_set(
        replace(
            change_set,
            state=WorkspaceChangeSetState.REVIEWABLE,
            approval=None,
        ),
        expected_version=change_set.version,
    )
    workspace = workspace_publisher.store.get_workspace(change_set.workspace_id, owner="alice")
    service = ProjectAgentService(
        store=workspace_publisher.store,
        workspace_root=Path(workspace.local_root).parent.parent,
        sandbox=SandboxExecutor(store=workspace_publisher.store),
        publisher=workspace_publisher,
    )

    response = ProjectAgentRoutes(service).handle_post(
        ["agent-changesets", change_set.change_set_id, "publish"],
        body=json.dumps(
            {
                "project_id": change_set.project_id,
                "workspace_id": change_set.workspace_id,
                "expected_version": reviewable.version,
                "approved_digest": change_set.digest,
            }
        ).encode(),
        identity=UserIdentity(username="alice"),
    )

    assert response is not None and response.status == 200
    assert response.payload["state"] == "published"
    persisted = workspace_publisher.store.get_change_set(change_set.change_set_id, owner="alice")
    assert persisted.state is WorkspaceChangeSetState.PUBLISHED
    assert persisted.approval is not None
    assert persisted.approval.actor == "alice"
    assert persisted.approval.approved_digest == change_set.digest


def _change_set(
    *, project_id: str, workspace_id: str, before: bytes, after: bytes
) -> WorkspaceChangeSet:
    digest = "b" * 64
    return WorkspaceChangeSet(
        change_set_id="changeset-publish",
        project_id=project_id,
        workspace_id=workspace_id,
        owner="alice",
        base_snapshot_digest="a" * 64,
        digest=digest,
        state=WorkspaceChangeSetState.APPROVED,
        version=1,
        files=(
            WorkspaceFileChange(
                path=str(PurePosixPath("main.py")),
                operation="modify",
                before_sha256=hashlib.sha256(before).hexdigest(),
                after_sha256=hashlib.sha256(after).hexdigest(),
                diff_sha256="c" * 64,
                size_bytes=len(after),
            ),
        ),
        sandbox_results=(),
        approval=WorkspaceApproval(
            actor="alice",
            approved_digest=digest,
            approved_at="2026-08-19T00:00:00Z",
        ),
        created_at="2026-08-19T00:00:00Z",
        updated_at="2026-08-19T00:00:00Z",
    )
