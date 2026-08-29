"""SQLite persistence contract for Experiment Projects and Blueprints."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pilot107.agent.project import (
    ExperimentProjectOrigin,
    ExperimentProjectSessionRecord,
    ExperimentProjectState,
    ProjectBlueprint,
    ProjectConflict,
    ProjectSource,
    blueprint_from_payload,
    blueprint_payload,
    source_from_payload,
    source_payload,
)
from pilot107.core.schema_migrations import SchemaMigration, apply_schema_migrations

PROJECT_MIGRATIONS = (
    SchemaMigration(
        migration_id="006b.001.agent_experiment_projects",
        statements=(
            """
            CREATE TABLE agent_experiment_projects (
                project_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                request_key TEXT NOT NULL,
                origin TEXT NOT NULL,
                state TEXT NOT NULL,
                version INTEGER NOT NULL,
                goal TEXT NOT NULL,
                source_json TEXT,
                blueprint_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (owner, request_key),
                CHECK (origin IN ('blank', 'template', 'existing', 'failed_run')),
                CHECK (state IN (
                    'drafting', 'editing', 'validating', 'awaiting_approval',
                    'publishing', 'ready', 'blocked', 'cancelled'
                )),
                CHECK (version > 0)
            )
            """,
            """
            CREATE INDEX idx_agent_experiment_projects_owner_updated
            ON agent_experiment_projects(owner, updated_at DESC, project_id DESC)
            """,
        ),
    ),
    SchemaMigration(
        migration_id="006b.002.agent_workspaces",
        statements=(
            """
            CREATE TABLE agent_workspaces (
                workspace_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES agent_experiment_projects(project_id),
                owner TEXT NOT NULL,
                snapshot_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (project_id, snapshot_digest)
            )
            """,
            """
            CREATE INDEX idx_agent_workspaces_owner_updated
            ON agent_workspaces(owner, updated_at DESC, workspace_id DESC)
            """,
        ),
    ),
    SchemaMigration(
        migration_id="006b.003.agent_workspace_changesets",
        statements=(
            """
            CREATE TABLE agent_workspace_changesets (
                change_set_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES agent_experiment_projects(project_id),
                workspace_id TEXT NOT NULL REFERENCES agent_workspaces(workspace_id),
                owner TEXT NOT NULL,
                digest TEXT NOT NULL,
                state TEXT NOT NULL,
                version INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                diff_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (version > 0)
            )
            """,
            """
            CREATE INDEX idx_agent_workspace_changesets_owner_updated
            ON agent_workspace_changesets(owner, updated_at DESC, change_set_id DESC)
            """,
        ),
    ),
    SchemaMigration(
        migration_id="006b.004.agent_workspace_publications",
        statements=(
            """
            CREATE TABLE agent_workspace_publications (
                publication_id TEXT PRIMARY KEY,
                change_set_id TEXT NOT NULL UNIQUE
                    REFERENCES agent_workspace_changesets(change_set_id),
                project_id TEXT NOT NULL REFERENCES agent_experiment_projects(project_id),
                workspace_id TEXT NOT NULL REFERENCES agent_workspaces(workspace_id),
                owner TEXT NOT NULL,
                target_root TEXT NOT NULL,
                approved_digest TEXT NOT NULL,
                state TEXT NOT NULL,
                version INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (version > 0)
            )
            """,
            """
            CREATE INDEX idx_agent_workspace_publications_owner_updated
            ON agent_workspace_publications(owner, updated_at DESC, publication_id DESC)
            """,
        ),
    ),
    SchemaMigration(
        migration_id="006b.005.agent_builder_submissions",
        statements=(
            """
            CREATE TABLE agent_builder_submissions (
                submission_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                session_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                project_id TEXT NOT NULL REFERENCES agent_experiment_projects(project_id),
                workspace_id TEXT NOT NULL REFERENCES agent_workspaces(workspace_id),
                request_key TEXT NOT NULL,
                input_digest TEXT NOT NULL,
                phase TEXT NOT NULL,
                state TEXT NOT NULL,
                version INTEGER NOT NULL,
                base_change_set_id TEXT,
                change_set_id TEXT,
                sandbox_result_id TEXT,
                task_id TEXT,
                receipt_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (owner, request_key),
                CHECK (phase IN ('drafting', 'sandbox_failed', 'validation_scheduled')),
                CHECK (state IN ('running', 'sandbox_failed', 'scheduled')),
                CHECK ((state = 'running' AND phase = 'drafting') OR
                       (state = 'sandbox_failed' AND phase = 'sandbox_failed') OR
                       (state = 'scheduled' AND phase = 'validation_scheduled')),
                CHECK (version > 0)
            )
            """,
            """
            CREATE INDEX idx_agent_builder_submissions_owner_updated
            ON agent_builder_submissions(owner, updated_at DESC, submission_id DESC)
            """,
        ),
    ),
)

if TYPE_CHECKING:
    from pilot107.agent.builder_workflow import BuilderSubmissionRecord
    from pilot107.agent.publisher import WorkspacePublication
    from pilot107.agent.workspace import (
        AgentWorkspaceRecord,
        SandboxResultRecord,
        WorkspaceChangeSet,
    )


class ProjectStore(Protocol):
    def create_project(
        self,
        *,
        owner: str,
        origin: ExperimentProjectOrigin | str,
        goal: str,
        request_key: str | None = None,
        source: ProjectSource | None = None,
    ) -> ExperimentProjectSessionRecord: ...

    def get_project(self, project_id: str, *, owner: str) -> ExperimentProjectSessionRecord: ...

    def list_projects(
        self, *, owner: str, limit: int = 100
    ) -> list[ExperimentProjectSessionRecord]: ...

    def block_for_model_unavailability(
        self, project_id: str, *, owner: str
    ) -> ExperimentProjectSessionRecord: ...

    def save_blueprint(
        self,
        project_id: str,
        owner: str,
        expected_version: int,
        blueprint: ProjectBlueprint,
    ) -> ExperimentProjectSessionRecord: ...

    def save_workspace(self, workspace: AgentWorkspaceRecord) -> AgentWorkspaceRecord: ...

    def get_workspace(self, workspace_id: str, *, owner: str) -> AgentWorkspaceRecord: ...

    def list_workspaces(self, project_id: str, *, owner: str) -> list[AgentWorkspaceRecord]: ...

    def save_change_set(
        self, change_set: WorkspaceChangeSet, *, diff_text: str
    ) -> WorkspaceChangeSet: ...

    def get_change_set(self, change_set_id: str, *, owner: str) -> WorkspaceChangeSet: ...

    def get_change_set_diff(self, change_set_id: str, *, owner: str) -> str: ...

    def list_change_sets(self, project_id: str, *, owner: str) -> list[WorkspaceChangeSet]: ...

    def append_sandbox_result(
        self, change_set_id: str, *, owner: str, result: SandboxResultRecord
    ) -> WorkspaceChangeSet: ...

    def replace_change_set(
        self, change_set: WorkspaceChangeSet, *, expected_version: int
    ) -> WorkspaceChangeSet: ...

    def save_workspace_publication(
        self, publication: WorkspacePublication
    ) -> WorkspacePublication: ...

    def get_workspace_publication(
        self, change_set_id: str, *, owner: str
    ) -> WorkspacePublication: ...

    def replace_workspace_publication(
        self, publication: WorkspacePublication, *, expected_version: int
    ) -> WorkspacePublication: ...

    def create_builder_submission(
        self, record: BuilderSubmissionRecord
    ) -> BuilderSubmissionRecord: ...

    def get_builder_submission(
        self, submission_id: str, *, owner: str
    ) -> BuilderSubmissionRecord: ...

    def get_builder_submission_by_request_key(
        self, owner: str, request_key: str
    ) -> BuilderSubmissionRecord | None: ...

    def get_latest_builder_submission(
        self,
        *,
        owner: str,
        session_id: str,
        project_id: str,
        workspace_id: str,
    ) -> BuilderSubmissionRecord | None: ...

    def replace_builder_submission(
        self, record: BuilderSubmissionRecord, *, expected_version: int
    ) -> BuilderSubmissionRecord: ...


class SQLiteProjectStore:
    def __init__(
        self,
        db_path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(UTC))
        with self.connect() as connection:
            apply_schema_migrations(connection, PROJECT_MIGRATIONS)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def create_project(
        self,
        *,
        owner: str,
        origin: ExperimentProjectOrigin | str,
        goal: str,
        request_key: str | None = None,
        source: ProjectSource | None = None,
    ) -> ExperimentProjectSessionRecord:
        normalized = _create_values(
            owner=owner,
            origin=origin,
            goal=goal,
            request_key=request_key,
            source=source,
        )
        now = self._now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO agent_experiment_projects (
                    project_id, owner, request_key, origin, state, version,
                    goal, source_json, blueprint_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'drafting', 1, ?, ?, NULL, ?, ?)
                """,
                (
                    normalized.project_id,
                    normalized.owner,
                    normalized.request_key,
                    normalized.origin.value,
                    normalized.goal,
                    normalized.source_json,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM agent_experiment_projects WHERE owner = ? AND request_key = ?",
                (normalized.owner, normalized.request_key),
            ).fetchone()
        if row is None:
            raise RuntimeError("Project insert did not produce a row")
        _assert_create_replay(row, normalized)
        return _row_to_project(row)

    def get_project(self, project_id: str, *, owner: str) -> ExperimentProjectSessionRecord:
        _key(project_id, "project_id")
        _key(owner, "owner")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_experiment_projects WHERE project_id = ? AND owner = ?",
                (project_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(project_id)
        return _row_to_project(row)

    def list_projects(
        self, *, owner: str, limit: int = 100
    ) -> list[ExperimentProjectSessionRecord]:
        _key(owner, "owner")
        _limit(limit)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_experiment_projects WHERE owner = ? "
                "ORDER BY updated_at DESC, project_id DESC LIMIT ?",
                (owner, limit),
            ).fetchall()
        return [_row_to_project(row) for row in rows]

    def block_for_model_unavailability(
        self, project_id: str, *, owner: str
    ) -> ExperimentProjectSessionRecord:
        """Idempotently block only a Project still doing generative work."""

        _key(project_id, "project_id")
        _key(owner, "owner")
        now = self._now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE agent_experiment_projects
                SET state = 'blocked', version = version + 1, updated_at = ?
                WHERE project_id = ? AND owner = ?
                  AND state IN ('drafting', 'editing', 'validating')
                """,
                (now, project_id, owner),
            )
            row = connection.execute(
                "SELECT * FROM agent_experiment_projects WHERE project_id = ? AND owner = ?",
                (project_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(project_id)
        return _row_to_project(row)

    def save_blueprint(
        self,
        project_id: str,
        owner: str,
        expected_version: int,
        blueprint: ProjectBlueprint,
    ) -> ExperimentProjectSessionRecord:
        _key(project_id, "project_id")
        _key(owner, "owner")
        _version(expected_version)
        if not isinstance(blueprint, ProjectBlueprint):
            raise TypeError("blueprint must be ProjectBlueprint")
        encoded = _canonical_json(blueprint_payload(blueprint))
        now = self._now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_experiment_projects
                SET blueprint_json = ?, state = 'editing', version = version + 1,
                    updated_at = ?
                WHERE project_id = ? AND owner = ? AND version = ?
                  AND state NOT IN ('cancelled', 'publishing')
                """,
                (encoded, now, project_id, owner, expected_version),
            )
            row = connection.execute(
                "SELECT * FROM agent_experiment_projects WHERE project_id = ? AND owner = ?",
                (project_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(project_id)
        if cursor.rowcount != 1:
            raise ProjectConflict("Project version or state changed while saving Blueprint")
        return _row_to_project(row)

    def save_workspace(self, workspace: AgentWorkspaceRecord) -> AgentWorkspaceRecord:
        from pilot107.agent.workspace import workspace_from_payload, workspace_payload

        self.get_project(workspace.project_id, owner=workspace.owner)
        payload = workspace_payload(workspace)
        encoded = _canonical_json(payload)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO agent_workspaces (
                    workspace_id, project_id, owner, snapshot_digest,
                    payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace.workspace_id,
                    workspace.project_id,
                    workspace.owner,
                    workspace.snapshot.digest,
                    encoded,
                    workspace.created_at,
                    workspace.updated_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM agent_workspaces WHERE workspace_id = ? AND owner = ?",
                (workspace.workspace_id, workspace.owner),
            ).fetchone()
        if row is None:
            raise RuntimeError("Workspace insert did not produce a row")
        if (
            str(row["project_id"]) != workspace.project_id
            or str(row["snapshot_digest"]) != workspace.snapshot.digest
        ):
            raise ProjectConflict("workspace_id refers to different snapshot content")
        stored = _json_object_or_none(row["payload_json"], "payload_json")
        if stored is None:
            raise RuntimeError("Workspace payload disappeared")
        return workspace_from_payload(stored)

    def get_workspace(self, workspace_id: str, *, owner: str) -> AgentWorkspaceRecord:
        from pilot107.agent.workspace import workspace_from_payload

        _key(workspace_id, "workspace_id")
        _key(owner, "owner")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM agent_workspaces WHERE workspace_id = ? AND owner = ?",
                (workspace_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(workspace_id)
        payload = _json_object_or_none(row["payload_json"], "payload_json")
        if payload is None:
            raise RuntimeError("Workspace payload disappeared")
        return workspace_from_payload(payload)

    def list_workspaces(self, project_id: str, *, owner: str) -> list[AgentWorkspaceRecord]:
        from pilot107.agent.workspace import workspace_from_payload

        self.get_project(project_id, owner=owner)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM agent_workspaces "
                "WHERE project_id = ? AND owner = ? "
                "ORDER BY updated_at DESC, workspace_id DESC",
                (project_id, owner),
            ).fetchall()
        records: list[AgentWorkspaceRecord] = []
        for row in rows:
            payload = _json_object_or_none(row["payload_json"], "payload_json")
            if payload is None:
                raise RuntimeError("Workspace payload disappeared")
            records.append(workspace_from_payload(payload))
        return records

    def save_change_set(
        self, change_set: WorkspaceChangeSet, *, diff_text: str
    ) -> WorkspaceChangeSet:
        from pilot107.agent.workspace import change_set_from_payload, change_set_payload

        if not isinstance(diff_text, str) or len(diff_text.encode()) > 1024 * 1024:
            raise ValueError("ChangeSet diff is invalid or exceeds the storage limit")
        workspace = self.get_workspace(change_set.workspace_id, owner=change_set.owner)
        if workspace.project_id != change_set.project_id:
            raise ProjectConflict("ChangeSet project does not own the Workspace")
        encoded = _canonical_json(change_set_payload(change_set))
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO agent_workspace_changesets (
                    change_set_id, project_id, workspace_id, owner, digest,
                    state, version, payload_json, diff_text, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    change_set.change_set_id,
                    change_set.project_id,
                    change_set.workspace_id,
                    change_set.owner,
                    change_set.digest,
                    change_set.state.value,
                    change_set.version,
                    encoded,
                    diff_text,
                    change_set.created_at,
                    change_set.updated_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM agent_workspace_changesets WHERE change_set_id = ? AND owner = ?",
                (change_set.change_set_id, change_set.owner),
            ).fetchone()
        if row is None:
            raise RuntimeError("ChangeSet insert did not produce a row")
        if str(row["digest"]) != change_set.digest or str(row["diff_text"]) != diff_text:
            raise ProjectConflict("change_set_id refers to different content")
        payload = _json_object_or_none(row["payload_json"], "payload_json")
        if payload is None:
            raise RuntimeError("ChangeSet payload disappeared")
        return change_set_from_payload(payload)

    def get_change_set(self, change_set_id: str, *, owner: str) -> WorkspaceChangeSet:
        from pilot107.agent.workspace import change_set_from_payload

        _key(change_set_id, "change_set_id")
        _key(owner, "owner")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM agent_workspace_changesets "
                "WHERE change_set_id = ? AND owner = ?",
                (change_set_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(change_set_id)
        payload = _json_object_or_none(row["payload_json"], "payload_json")
        if payload is None:
            raise RuntimeError("ChangeSet payload disappeared")
        return change_set_from_payload(payload)

    def get_change_set_diff(self, change_set_id: str, *, owner: str) -> str:
        _key(change_set_id, "change_set_id")
        _key(owner, "owner")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT diff_text FROM agent_workspace_changesets "
                "WHERE change_set_id = ? AND owner = ?",
                (change_set_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(change_set_id)
        return str(row["diff_text"])

    def list_change_sets(self, project_id: str, *, owner: str) -> list[WorkspaceChangeSet]:
        from pilot107.agent.workspace import change_set_from_payload

        self.get_project(project_id, owner=owner)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM agent_workspace_changesets "
                "WHERE project_id = ? AND owner = ? "
                "ORDER BY updated_at DESC, change_set_id DESC",
                (project_id, owner),
            ).fetchall()
        records: list[WorkspaceChangeSet] = []
        for row in rows:
            payload = _json_object_or_none(row["payload_json"], "payload_json")
            if payload is None:
                raise RuntimeError("ChangeSet payload disappeared")
            records.append(change_set_from_payload(payload))
        return records

    def append_sandbox_result(
        self, change_set_id: str, *, owner: str, result: SandboxResultRecord
    ) -> WorkspaceChangeSet:
        from pilot107.agent.workspace import (
            SandboxResultRecord,
            WorkspaceChangeSetState,
            change_set_from_payload,
            change_set_payload,
        )

        if not isinstance(result, SandboxResultRecord):
            raise TypeError("result must be SandboxResultRecord")
        _key(change_set_id, "change_set_id")
        _key(owner, "owner")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_json FROM agent_workspace_changesets "
                "WHERE change_set_id = ? AND owner = ?",
                (change_set_id, owner),
            ).fetchone()
            if row is None:
                raise KeyError(change_set_id)
            payload = _json_object_or_none(row["payload_json"], "payload_json")
            if payload is None:
                raise RuntimeError("ChangeSet payload disappeared")
            current = change_set_from_payload(payload)
            if any(item.result_id == result.result_id for item in current.sandbox_results):
                return current
            if len(current.sandbox_results) >= 256:
                raise ProjectConflict("ChangeSet sandbox result limit reached")
            payload = change_set_payload(current)
            payload["sandbox_results"] = [
                *payload["sandbox_results"],
                {
                    "result_id": result.result_id,
                    "argv": list(result.argv),
                    "status": result.status,
                    "exit_code": result.exit_code,
                    "stdout_sha256": result.stdout_sha256,
                    "stderr_sha256": result.stderr_sha256,
                },
            ]
            payload["version"] = current.version + 1
            payload["state"] = (
                WorkspaceChangeSetState.REVIEWABLE.value
                if result.status == "succeeded"
                else WorkspaceChangeSetState.FAILED.value
            )
            payload["updated_at"] = self._now()
            encoded = _canonical_json(payload)
            connection.execute(
                """
                UPDATE agent_workspace_changesets
                SET state = ?, version = ?, payload_json = ?, updated_at = ?
                WHERE change_set_id = ? AND owner = ? AND version = ?
                """,
                (
                    payload["state"],
                    payload["version"],
                    encoded,
                    payload["updated_at"],
                    change_set_id,
                    owner,
                    current.version,
                ),
            )
        return change_set_from_payload(payload)

    def replace_change_set(
        self, change_set: WorkspaceChangeSet, *, expected_version: int
    ) -> WorkspaceChangeSet:
        from pilot107.agent.workspace import change_set_from_payload, change_set_payload

        _version(expected_version)
        current = self.get_change_set(change_set.change_set_id, owner=change_set.owner)
        if current.digest != change_set.digest:
            raise ProjectConflict("ChangeSet content cannot change during state update")
        payload = change_set_payload(replace(change_set, version=expected_version + 1))
        encoded = _canonical_json(payload)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_workspace_changesets
                SET state = ?, version = ?, payload_json = ?, updated_at = ?
                WHERE change_set_id = ? AND owner = ? AND version = ?
                """,
                (
                    payload["state"],
                    payload["version"],
                    encoded,
                    payload["updated_at"],
                    change_set.change_set_id,
                    change_set.owner,
                    expected_version,
                ),
            )
        if cursor.rowcount != 1:
            raise ProjectConflict("ChangeSet version changed during update")
        return change_set_from_payload(payload)

    def save_workspace_publication(self, publication: WorkspacePublication) -> WorkspacePublication:
        from pilot107.agent.publisher import publication_from_payload, publication_payload

        change_set = self.get_change_set(publication.change_set_id, owner=publication.owner)
        if (
            change_set.project_id != publication.project_id
            or change_set.workspace_id != publication.workspace_id
        ):
            raise ProjectConflict("Publication does not match its ChangeSet")
        encoded = _canonical_json(publication_payload(publication))
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO agent_workspace_publications (
                    publication_id, change_set_id, project_id, workspace_id, owner,
                    target_root, approved_digest, state, version, payload_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    publication.publication_id,
                    publication.change_set_id,
                    publication.project_id,
                    publication.workspace_id,
                    publication.owner,
                    publication.target_root,
                    publication.approved_digest,
                    publication.state.value,
                    publication.version,
                    encoded,
                    publication.created_at,
                    publication.updated_at,
                ),
            )
            row = connection.execute(
                "SELECT payload_json FROM agent_workspace_publications "
                "WHERE change_set_id = ? AND owner = ?",
                (publication.change_set_id, publication.owner),
            ).fetchone()
        if row is None:
            raise RuntimeError("Publication insert did not produce a row")
        payload = _json_object_or_none(row["payload_json"], "payload_json")
        if payload is None:
            raise RuntimeError("Publication payload disappeared")
        result = publication_from_payload(payload)
        if (
            result.approved_digest != publication.approved_digest
            or result.target_root != publication.target_root
        ):
            raise ProjectConflict("ChangeSet already has a different publication")
        return result

    def get_workspace_publication(self, change_set_id: str, *, owner: str) -> WorkspacePublication:
        from pilot107.agent.publisher import publication_from_payload

        _key(change_set_id, "change_set_id")
        _key(owner, "owner")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM agent_workspace_publications "
                "WHERE change_set_id = ? AND owner = ?",
                (change_set_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(change_set_id)
        payload = _json_object_or_none(row["payload_json"], "payload_json")
        if payload is None:
            raise RuntimeError("Publication payload disappeared")
        return publication_from_payload(payload)

    def replace_workspace_publication(
        self, publication: WorkspacePublication, *, expected_version: int
    ) -> WorkspacePublication:
        from pilot107.agent.publisher import publication_from_payload, publication_payload

        _version(expected_version)
        payload = publication_payload(replace(publication, version=expected_version + 1))
        encoded = _canonical_json(payload)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_workspace_publications
                SET state = ?, version = ?, payload_json = ?, updated_at = ?
                WHERE change_set_id = ? AND owner = ? AND version = ?
                """,
                (
                    payload["state"],
                    payload["version"],
                    encoded,
                    payload["updated_at"],
                    publication.change_set_id,
                    publication.owner,
                    expected_version,
                ),
            )
        if cursor.rowcount != 1:
            raise ProjectConflict("Publication version changed during update")
        return publication_from_payload(payload)

    def create_builder_submission(
        self, record: BuilderSubmissionRecord
    ) -> BuilderSubmissionRecord:
        from pilot107.agent.builder_workflow import BuilderSubmissionRecord

        if not isinstance(record, BuilderSubmissionRecord):
            raise TypeError("record must be a BuilderSubmissionRecord")
        workspace = self.get_workspace(record.workspace_id, owner=record.owner)
        if workspace.project_id != record.project_id:
            raise ProjectConflict("Builder submission Project does not own the Workspace")
        receipt_json = (
            None if record.receipt is None else _canonical_json(dict(record.receipt))
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO agent_builder_submissions (
                    submission_id, owner, session_id, turn_id, project_id,
                    workspace_id, request_key, input_digest, phase, state,
                    version, base_change_set_id, change_set_id,
                    sandbox_result_id, task_id, receipt_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _builder_submission_values(record, receipt_json=receipt_json),
            )
            row = connection.execute(
                "SELECT * FROM agent_builder_submissions "
                "WHERE owner = ? AND request_key = ?",
                (record.owner, record.request_key),
            ).fetchone()
        if row is None:
            raise RuntimeError("Builder submission insert did not produce a row")
        result = _row_to_builder_submission(row)
        _assert_builder_submission_replay(result, record)
        return result

    def get_builder_submission(
        self, submission_id: str, *, owner: str
    ) -> BuilderSubmissionRecord:
        _key(submission_id, "submission_id")
        _key(owner, "owner")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_builder_submissions "
                "WHERE submission_id = ? AND owner = ?",
                (submission_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(submission_id)
        return _row_to_builder_submission(row)

    def get_builder_submission_by_request_key(
        self, owner: str, request_key: str
    ) -> BuilderSubmissionRecord | None:
        _key(owner, "owner")
        _key(request_key, "request_key")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_builder_submissions "
                "WHERE owner = ? AND request_key = ?",
                (owner, request_key),
            ).fetchone()
        return None if row is None else _row_to_builder_submission(row)

    def get_latest_builder_submission(
        self,
        *,
        owner: str,
        session_id: str,
        project_id: str,
        workspace_id: str,
    ) -> BuilderSubmissionRecord | None:
        for value, label in (
            (owner, "owner"),
            (session_id, "session_id"),
            (project_id, "project_id"),
            (workspace_id, "workspace_id"),
        ):
            _key(value, label)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_builder_submissions "
                "WHERE owner = ? AND session_id = ? AND project_id = ? "
                "AND workspace_id = ? "
                "ORDER BY updated_at DESC, submission_id DESC LIMIT 1",
                (owner, session_id, project_id, workspace_id),
            ).fetchone()
        return None if row is None else _row_to_builder_submission(row)

    def replace_builder_submission(
        self, record: BuilderSubmissionRecord, *, expected_version: int
    ) -> BuilderSubmissionRecord:
        from pilot107.agent.builder_workflow import (
            BuilderSubmissionConflict,
            BuilderSubmissionRecord,
        )

        if not isinstance(record, BuilderSubmissionRecord):
            raise TypeError("record must be a BuilderSubmissionRecord")
        _version(expected_version)
        if record.version != expected_version + 1:
            raise ValueError("Builder submission version must advance by one")
        current = self.get_builder_submission(record.submission_id, owner=record.owner)
        _assert_builder_submission_identity(current, record)
        receipt_json = (
            None if record.receipt is None else _canonical_json(dict(record.receipt))
        )
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_builder_submissions
                SET phase = ?, state = ?, version = ?, base_change_set_id = ?,
                    change_set_id = ?, sandbox_result_id = ?, task_id = ?,
                    receipt_json = ?, updated_at = ?
                WHERE submission_id = ? AND owner = ? AND version = ?
                """,
                (
                    record.phase.value,
                    record.state.value,
                    record.version,
                    record.base_change_set_id,
                    record.change_set_id,
                    record.sandbox_result_id,
                    record.task_id,
                    receipt_json,
                    record.updated_at,
                    record.submission_id,
                    record.owner,
                    expected_version,
                ),
            )
        if cursor.rowcount != 1:
            raise BuilderSubmissionConflict(
                "Builder submission version changed during update"
            )
        return record

    def _now(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Project Store clock must return an aware datetime")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class _CreateValues:
    def __init__(
        self,
        *,
        project_id: str,
        owner: str,
        request_key: str,
        origin: ExperimentProjectOrigin,
        goal: str,
        source_json: str | None,
    ) -> None:
        self.project_id = project_id
        self.owner = owner
        self.request_key = request_key
        self.origin = origin
        self.goal = goal
        self.source_json = source_json


def _create_values(
    *,
    owner: str,
    origin: ExperimentProjectOrigin | str,
    goal: str,
    request_key: str | None,
    source: ProjectSource | None,
) -> _CreateValues:
    _key(owner, "owner")
    try:
        normalized_origin = ExperimentProjectOrigin(origin)
    except ValueError as exc:
        raise ValueError("Project origin is invalid") from exc
    if not isinstance(goal, str) or not goal or len(goal) > 64_000 or "\0" in goal:
        raise ValueError("Project goal is invalid")
    if source is not None and not isinstance(source, ProjectSource):
        raise TypeError("source must be ProjectSource")
    source_json = None if source is None else _canonical_json(source_payload(source))
    content = _canonical_json(
        {
            "owner": owner,
            "origin": normalized_origin.value,
            "goal": goal,
            "source": source_payload(source),
        }
    )
    stable_request_key = request_key or f"digest:{hashlib.sha256(content.encode()).hexdigest()}"
    _key(stable_request_key, "request_key")
    project_digest = hashlib.sha256(f"{owner}\0{stable_request_key}".encode()).hexdigest()[:24]
    return _CreateValues(
        project_id=f"project-{project_digest}",
        owner=owner,
        request_key=stable_request_key,
        origin=normalized_origin,
        goal=goal,
        source_json=source_json,
    )


def _assert_create_replay(row: Any, expected: _CreateValues) -> None:
    if (
        str(row["project_id"]) != expected.project_id
        or str(row["origin"]) != expected.origin.value
        or str(row["goal"]) != expected.goal
        or _json_object_or_none(row["source_json"], "source_json")
        != _json_object_or_none(expected.source_json, "source_json")
    ):
        raise ProjectConflict("request_key refers to different Project content")


def _row_to_project(row: Any) -> ExperimentProjectSessionRecord:
    source_json = _json_object_or_none(row["source_json"], "source_json")
    blueprint_json = _json_object_or_none(row["blueprint_json"], "blueprint_json")
    return ExperimentProjectSessionRecord(
        project_id=str(row["project_id"]),
        owner=str(row["owner"]),
        origin=ExperimentProjectOrigin(str(row["origin"])),
        state=ExperimentProjectState(str(row["state"])),
        version=int(row["version"]),
        goal=str(row["goal"]),
        source=source_from_payload(source_json),
        blueprint=None if blueprint_json is None else blueprint_from_payload(blueprint_json),
        created_at=_timestamp(row["created_at"]),
        updated_at=_timestamp(row["updated_at"]),
    )


def _builder_submission_values(
    record: BuilderSubmissionRecord, *, receipt_json: str | None
) -> tuple[object, ...]:
    return (
        record.submission_id,
        record.owner,
        record.session_id,
        record.turn_id,
        record.project_id,
        record.workspace_id,
        record.request_key,
        record.input_digest,
        record.phase.value,
        record.state.value,
        record.version,
        record.base_change_set_id,
        record.change_set_id,
        record.sandbox_result_id,
        record.task_id,
        receipt_json,
        record.created_at,
        record.updated_at,
    )


def _row_to_builder_submission(row: Any) -> BuilderSubmissionRecord:
    from pilot107.agent.builder_workflow import builder_submission_from_payload

    return builder_submission_from_payload(
        {
            "submission_id": row["submission_id"],
            "owner": row["owner"],
            "session_id": row["session_id"],
            "turn_id": row["turn_id"],
            "project_id": row["project_id"],
            "workspace_id": row["workspace_id"],
            "request_key": row["request_key"],
            "input_digest": row["input_digest"],
            "phase": row["phase"],
            "state": row["state"],
            "version": row["version"],
            "base_change_set_id": row["base_change_set_id"],
            "change_set_id": row["change_set_id"],
            "sandbox_result_id": row["sandbox_result_id"],
            "task_id": row["task_id"],
            "receipt": _json_object_or_none(row["receipt_json"], "receipt_json"),
            "created_at": _timestamp(row["created_at"]),
            "updated_at": _timestamp(row["updated_at"]),
        }
    )


def _assert_builder_submission_identity(
    current: BuilderSubmissionRecord, candidate: BuilderSubmissionRecord
) -> None:
    from pilot107.agent.builder_workflow import BuilderSubmissionConflict

    immutable = (
        "submission_id",
        "owner",
        "session_id",
        "turn_id",
        "project_id",
        "workspace_id",
        "request_key",
        "input_digest",
        "created_at",
    )
    if any(getattr(current, name) != getattr(candidate, name) for name in immutable):
        raise BuilderSubmissionConflict("Builder submission identity cannot change")


def _assert_builder_submission_replay(
    stored: BuilderSubmissionRecord, candidate: BuilderSubmissionRecord
) -> None:
    _assert_builder_submission_identity(stored, candidate)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object_or_none(value: object, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        raise TypeError(f"{label} must contain an object")
    return parsed


def _key(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(character in value for character in "\r\n\0")
    ):
        raise ValueError(f"{label} is invalid")
    return value


def _version(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("expected_version is invalid")
    return value


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ValueError("limit must be between 1 and 100")
    return value


def _timestamp(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)
