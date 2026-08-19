"""SQLite persistence contract for Experiment Projects and Blueprints."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
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
)

if TYPE_CHECKING:
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

    def get_project(
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

    def save_change_set(
        self, change_set: WorkspaceChangeSet, *, diff_text: str
    ) -> WorkspaceChangeSet: ...

    def get_change_set(
        self, change_set_id: str, *, owner: str
    ) -> WorkspaceChangeSet: ...

    def get_change_set_diff(self, change_set_id: str, *, owner: str) -> str: ...

    def append_sandbox_result(
        self, change_set_id: str, *, owner: str, result: SandboxResultRecord
    ) -> WorkspaceChangeSet: ...


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

    def get_project(
        self, project_id: str, *, owner: str
    ) -> ExperimentProjectSessionRecord:
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
                "SELECT payload_json FROM agent_workspaces "
                "WHERE workspace_id = ? AND owner = ?",
                (workspace_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(workspace_id)
        payload = _json_object_or_none(row["payload_json"], "payload_json")
        if payload is None:
            raise RuntimeError("Workspace payload disappeared")
        return workspace_from_payload(payload)

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
                "SELECT * FROM agent_workspace_changesets "
                "WHERE change_set_id = ? AND owner = ?",
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

    def get_change_set(
        self, change_set_id: str, *, owner: str
    ) -> WorkspaceChangeSet:
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


def _timestamp(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)
