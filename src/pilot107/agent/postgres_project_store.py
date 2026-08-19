"""PostgreSQL persistence for Experiment Projects and Blueprints."""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pilot107.agent.project import (
    ExperimentProjectOrigin,
    ExperimentProjectSessionRecord,
    ProjectBlueprint,
    ProjectConflict,
    ProjectSource,
    blueprint_payload,
)
from pilot107.agent.project_store import (
    _assert_create_replay,
    _create_values,
    _key,
    _row_to_project,
    _version,
)
from pilot107.core.postgres_control_repository import PostgresDriverUnavailable
from pilot107.core.postgres_domain_schema import initialize_postgres_domain_schema

if TYPE_CHECKING:
    from pilot107.agent.workspace import AgentWorkspaceRecord


class PostgresProjectStore:
    def __init__(
        self,
        dsn: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not dsn or any(character in dsn for character in "\r\n\0"):
            raise ValueError("PostgreSQL DSN is invalid")
        self.dsn = dsn
        self._clock = clock or (lambda: datetime.now(UTC))
        try:
            self._psycopg = importlib.import_module("psycopg")
            self._dict_row = importlib.import_module("psycopg.rows").dict_row
            self._jsonb = importlib.import_module("psycopg.types.json").Jsonb
        except ModuleNotFoundError as exc:
            raise PostgresDriverUnavailable(
                "install pilot107[postgres] to use PostgreSQL Project repositories"
            ) from exc
        initialize_postgres_domain_schema(dsn)

    def connect(self) -> Any:
        return self._psycopg.connect(self.dsn, row_factory=self._dict_row)

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
        source_value = None
        if normalized.source_json is not None:
            source_value = self._jsonb(json.loads(normalized.source_json))
        with self.connect() as connection:
            row = connection.execute(
                """
                INSERT INTO agent_experiment_projects (
                    project_id, owner, request_key, origin, state, version,
                    goal, source_json, blueprint_json, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, 'drafting', 1, %s, %s, NULL, %s, %s)
                ON CONFLICT (owner, request_key) DO NOTHING
                RETURNING *
                """,
                (
                    normalized.project_id,
                    normalized.owner,
                    normalized.request_key,
                    normalized.origin.value,
                    normalized.goal,
                    source_value,
                    now,
                    now,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM agent_experiment_projects "
                    "WHERE owner = %s AND request_key = %s",
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
                "SELECT * FROM agent_experiment_projects "
                "WHERE project_id = %s AND owner = %s",
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
        with self.connect() as connection:
            row = connection.execute(
                """
                UPDATE agent_experiment_projects
                SET blueprint_json = %s, state = 'editing', version = version + 1,
                    updated_at = %s
                WHERE project_id = %s AND owner = %s AND version = %s
                  AND state NOT IN ('cancelled', 'publishing')
                RETURNING *
                """,
                (
                    self._jsonb(blueprint_payload(blueprint)),
                    self._now(),
                    project_id,
                    owner,
                    expected_version,
                ),
            ).fetchone()
            if row is None:
                existing = connection.execute(
                    "SELECT project_id FROM agent_experiment_projects "
                    "WHERE project_id = %s AND owner = %s",
                    (project_id, owner),
                ).fetchone()
        if row is not None:
            return _row_to_project(row)
        if existing is None:
            raise KeyError(project_id)
        raise ProjectConflict("Project version or state changed while saving Blueprint")

    def save_workspace(self, workspace: AgentWorkspaceRecord) -> AgentWorkspaceRecord:
        from pilot107.agent.workspace import workspace_from_payload, workspace_payload

        self.get_project(workspace.project_id, owner=workspace.owner)
        payload = workspace_payload(workspace)
        with self.connect() as connection:
            row = connection.execute(
                """
                INSERT INTO agent_workspaces (
                    workspace_id, project_id, owner, snapshot_digest,
                    payload_json, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (workspace_id) DO NOTHING
                RETURNING *
                """,
                (
                    workspace.workspace_id,
                    workspace.project_id,
                    workspace.owner,
                    workspace.snapshot.digest,
                    self._jsonb(payload),
                    workspace.created_at,
                    workspace.updated_at,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM agent_workspaces "
                    "WHERE workspace_id = %s AND owner = %s",
                    (workspace.workspace_id, workspace.owner),
                ).fetchone()
        if row is None:
            raise RuntimeError("Workspace insert did not produce a row")
        if (
            str(row["project_id"]) != workspace.project_id
            or str(row["snapshot_digest"]) != workspace.snapshot.digest
        ):
            raise ProjectConflict("workspace_id refers to different snapshot content")
        payload_value = row["payload_json"]
        if not isinstance(payload_value, dict):
            raise TypeError("Workspace payload must be an object")
        return workspace_from_payload(payload_value)

    def get_workspace(self, workspace_id: str, *, owner: str) -> AgentWorkspaceRecord:
        from pilot107.agent.workspace import workspace_from_payload

        _key(workspace_id, "workspace_id")
        _key(owner, "owner")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM agent_workspaces "
                "WHERE workspace_id = %s AND owner = %s",
                (workspace_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(workspace_id)
        payload = row["payload_json"]
        if not isinstance(payload, dict):
            raise TypeError("Workspace payload must be an object")
        return workspace_from_payload(payload)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Project Store clock must return an aware datetime")
        return value.astimezone(UTC)
