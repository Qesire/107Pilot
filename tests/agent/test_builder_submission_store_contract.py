from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pilot107.agent.builder_workflow import (
    BuilderPhase,
    BuilderSubmissionConflict,
    BuilderSubmissionRecord,
    BuilderSubmissionState,
)
from pilot107.agent.project_store import ProjectStore, SQLiteProjectStore
from pilot107.agent.workspace import AgentWorkspaceRecord, WorkspaceSnapshot


def _submission(
    *,
    project_id: str,
    request_key: str = "build-1",
    input_digest: str = "a" * 64,
) -> BuilderSubmissionRecord:
    return BuilderSubmissionRecord(
        submission_id="builder-submission-1",
        owner="alice",
        session_id="session-1",
        turn_id="turn-1",
        project_id=project_id,
        workspace_id="workspace-builder",
        request_key=request_key,
        input_digest=input_digest,
        phase=BuilderPhase.DRAFTING,
        state=BuilderSubmissionState.RUNNING,
        version=1,
        base_change_set_id=None,
        change_set_id=None,
        sandbox_result_id=None,
        task_id=None,
        receipt=None,
        created_at="2026-08-29T00:00:00Z",
        updated_at="2026-08-29T00:00:00Z",
    )


def _store(tmp_path: Path) -> tuple[SQLiteProjectStore, str]:
    store = SQLiteProjectStore(tmp_path / "builder.db")
    project = store.create_project(
        owner="alice",
        origin="blank",
        goal="build a bounded scientific experiment",
        request_key="builder-project",
    )
    store.save_workspace(
        AgentWorkspaceRecord(
            workspace_id="workspace-builder",
            project_id=project.project_id,
            owner="alice",
            local_root=str(tmp_path.resolve()),
            snapshot=WorkspaceSnapshot(
                source_ref="/public/home/alice/builder",
                digest="b" * 64,
                entries=(),
                captured_at="2026-08-29T00:00:00Z",
            ),
            created_at="2026-08-29T00:00:00Z",
            updated_at="2026-08-29T00:00:00Z",
        )
    )
    return store, project.project_id


def exercise_builder_submission_store_contract(store: ProjectStore, project_id: str) -> None:
    first = store.create_builder_submission(_submission(project_id=project_id))
    replay = store.create_builder_submission(_submission(project_id=project_id))

    assert replay == first
    assert store.get_builder_submission(first.submission_id, owner="alice") == first
    assert store.get_builder_submission_by_request_key("alice", "build-1") == first
    assert store.get_builder_submission_by_request_key("alice", "missing") is None
    with pytest.raises(KeyError):
        store.get_builder_submission(first.submission_id, owner="bob")

    with pytest.raises(BuilderSubmissionConflict):
        store.create_builder_submission(
            replace(_submission(project_id=project_id), input_digest="f" * 64)
        )

    completed = replace(
        first,
        state=BuilderSubmissionState.SANDBOX_FAILED,
        phase=BuilderPhase.SANDBOX_FAILED,
        version=2,
        change_set_id="changeset-builder",
        sandbox_result_id="sandbox-builder",
        receipt={"status": "repair_required"},
        updated_at="2026-08-29T00:01:00Z",
    )
    assert store.replace_builder_submission(completed, expected_version=1) == completed
    with pytest.raises(BuilderSubmissionConflict):
        store.replace_builder_submission(completed, expected_version=1)


def test_sqlite_builder_submission_store_satisfies_contract(tmp_path: Path) -> None:
    store, project_id = _store(tmp_path)
    exercise_builder_submission_store_contract(store, project_id)


def test_sqlite_builder_submission_survives_reopen(tmp_path: Path) -> None:
    store, project_id = _store(tmp_path)
    created = store.create_builder_submission(_submission(project_id=project_id))

    reopened = SQLiteProjectStore(store.db_path)

    assert reopened.get_builder_submission(created.submission_id, owner="alice") == created
