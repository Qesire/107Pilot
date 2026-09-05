"""PostgreSQL persistence for Experiment Projects and Blueprints."""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from dataclasses import replace
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
    _assert_builder_submission_identity,
    _assert_builder_submission_replay,
    _assert_create_replay,
    _create_values,
    _key,
    _latest_builder_submission,
    _limit,
    _row_to_builder_submission,
    _row_to_project,
    _version,
)
from pilot107.core.postgres_control_repository import PostgresDriverUnavailable
from pilot107.core.postgres_domain_schema import initialize_postgres_domain_schema

if TYPE_CHECKING:
    from pilot107.agent.builder_workflow import BuilderSubmissionRecord
    from pilot107.agent.publisher import WorkspacePublication
    from pilot107.agent.workspace import (
        AgentWorkspaceRecord,
        SandboxResultRecord,
        WorkspaceChangeSet,
    )


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
                    "SELECT * FROM agent_experiment_projects WHERE owner = %s AND request_key = %s",
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
                "SELECT * FROM agent_experiment_projects WHERE project_id = %s AND owner = %s",
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
                "SELECT * FROM agent_experiment_projects WHERE owner = %s "
                "ORDER BY updated_at DESC, project_id DESC LIMIT %s",
                (owner, limit),
            ).fetchall()
        return [_row_to_project(row) for row in rows]

    def block_for_model_unavailability(
        self, project_id: str, *, owner: str
    ) -> ExperimentProjectSessionRecord:
        """Idempotently block only a Project still doing generative work."""

        _key(project_id, "project_id")
        _key(owner, "owner")
        with self.connect() as connection:
            row = connection.execute(
                """
                UPDATE agent_experiment_projects
                SET state = 'blocked', version = version + 1, updated_at = %s
                WHERE project_id = %s AND owner = %s
                  AND state IN ('drafting', 'editing', 'validating')
                RETURNING *
                """,
                (self._now(), project_id, owner),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM agent_experiment_projects WHERE project_id = %s AND owner = %s",
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
                    "SELECT * FROM agent_workspaces WHERE workspace_id = %s AND owner = %s",
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
                "SELECT payload_json FROM agent_workspaces WHERE workspace_id = %s AND owner = %s",
                (workspace_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(workspace_id)
        payload = row["payload_json"]
        if not isinstance(payload, dict):
            raise TypeError("Workspace payload must be an object")
        return workspace_from_payload(payload)

    def list_workspaces(self, project_id: str, *, owner: str) -> list[AgentWorkspaceRecord]:
        from pilot107.agent.workspace import workspace_from_payload

        self.get_project(project_id, owner=owner)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM agent_workspaces "
                "WHERE project_id = %s AND owner = %s "
                "ORDER BY updated_at DESC, workspace_id DESC",
                (project_id, owner),
            ).fetchall()
        records: list[AgentWorkspaceRecord] = []
        for row in rows:
            payload = row["payload_json"]
            if not isinstance(payload, dict):
                raise TypeError("Workspace payload must be an object")
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
        with self.connect() as connection:
            row = connection.execute(
                """
                INSERT INTO agent_workspace_changesets (
                    change_set_id, project_id, workspace_id, owner, digest,
                    state, version, payload_json, diff_text, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (change_set_id) DO NOTHING
                RETURNING *
                """,
                (
                    change_set.change_set_id,
                    change_set.project_id,
                    change_set.workspace_id,
                    change_set.owner,
                    change_set.digest,
                    change_set.state.value,
                    change_set.version,
                    self._jsonb(change_set_payload(change_set)),
                    diff_text,
                    change_set.created_at,
                    change_set.updated_at,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM agent_workspace_changesets "
                    "WHERE change_set_id = %s AND owner = %s",
                    (change_set.change_set_id, change_set.owner),
                ).fetchone()
        if row is None:
            raise RuntimeError("ChangeSet insert did not produce a row")
        if str(row["digest"]) != change_set.digest or str(row["diff_text"]) != diff_text:
            raise ProjectConflict("change_set_id refers to different content")
        payload = row["payload_json"]
        if not isinstance(payload, dict):
            raise TypeError("ChangeSet payload must be an object")
        return change_set_from_payload(payload)

    def get_change_set(self, change_set_id: str, *, owner: str) -> WorkspaceChangeSet:
        from pilot107.agent.workspace import change_set_from_payload

        _key(change_set_id, "change_set_id")
        _key(owner, "owner")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM agent_workspace_changesets "
                "WHERE change_set_id = %s AND owner = %s",
                (change_set_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(change_set_id)
        payload = row["payload_json"]
        if not isinstance(payload, dict):
            raise TypeError("ChangeSet payload must be an object")
        return change_set_from_payload(payload)

    def get_change_set_diff(self, change_set_id: str, *, owner: str) -> str:
        _key(change_set_id, "change_set_id")
        _key(owner, "owner")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT diff_text FROM agent_workspace_changesets "
                "WHERE change_set_id = %s AND owner = %s",
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
                "WHERE project_id = %s AND owner = %s "
                "ORDER BY updated_at DESC, change_set_id DESC",
                (project_id, owner),
            ).fetchall()
        records: list[WorkspaceChangeSet] = []
        for row in rows:
            payload = row["payload_json"]
            if not isinstance(payload, dict):
                raise TypeError("ChangeSet payload must be an object")
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
        with self.connect() as connection, connection.transaction():
            row = connection.execute(
                "SELECT payload_json FROM agent_workspace_changesets "
                "WHERE change_set_id = %s AND owner = %s FOR UPDATE",
                (change_set_id, owner),
            ).fetchone()
            if row is None:
                raise KeyError(change_set_id)
            payload_value = row["payload_json"]
            if not isinstance(payload_value, dict):
                raise TypeError("ChangeSet payload must be an object")
            current = change_set_from_payload(payload_value)
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
            now = self._now()
            payload["updated_at"] = now.isoformat().replace("+00:00", "Z")
            connection.execute(
                """
                UPDATE agent_workspace_changesets
                SET state = %s, version = %s, payload_json = %s, updated_at = %s
                WHERE change_set_id = %s AND owner = %s AND version = %s
                """,
                (
                    payload["state"],
                    payload["version"],
                    self._jsonb(payload),
                    now,
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
        payload = change_set_payload(replace(change_set, version=expected_version + 1))
        with self.connect() as connection:
            row = connection.execute(
                """
                UPDATE agent_workspace_changesets
                SET state = %s, version = %s, payload_json = %s, updated_at = %s
                WHERE change_set_id = %s AND owner = %s AND version = %s AND digest = %s
                RETURNING payload_json
                """,
                (
                    payload["state"],
                    payload["version"],
                    self._jsonb(payload),
                    payload["updated_at"],
                    change_set.change_set_id,
                    change_set.owner,
                    expected_version,
                    change_set.digest,
                ),
            ).fetchone()
            if row is None:
                existing = connection.execute(
                    "SELECT change_set_id FROM agent_workspace_changesets "
                    "WHERE change_set_id = %s AND owner = %s",
                    (change_set.change_set_id, change_set.owner),
                ).fetchone()
        if row is None:
            if existing is None:
                raise KeyError(change_set.change_set_id)
            raise ProjectConflict("ChangeSet version or content changed during update")
        value = row["payload_json"]
        if not isinstance(value, dict):
            raise TypeError("ChangeSet payload must be an object")
        return change_set_from_payload(value)

    def save_workspace_publication(self, publication: WorkspacePublication) -> WorkspacePublication:
        from pilot107.agent.publisher import publication_from_payload, publication_payload

        change_set = self.get_change_set(publication.change_set_id, owner=publication.owner)
        if (
            change_set.project_id != publication.project_id
            or change_set.workspace_id != publication.workspace_id
        ):
            raise ProjectConflict("Publication does not match its ChangeSet")
        with self.connect() as connection:
            row = connection.execute(
                """
                INSERT INTO agent_workspace_publications (
                    publication_id, change_set_id, project_id, workspace_id, owner,
                    target_root, approved_digest, state, version, payload_json,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (change_set_id) DO NOTHING
                RETURNING payload_json
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
                    self._jsonb(publication_payload(publication)),
                    publication.created_at,
                    publication.updated_at,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT payload_json FROM agent_workspace_publications "
                    "WHERE change_set_id = %s AND owner = %s",
                    (publication.change_set_id, publication.owner),
                ).fetchone()
        if row is None:
            raise RuntimeError("Publication insert did not produce a row")
        value = row["payload_json"]
        if not isinstance(value, dict):
            raise TypeError("Publication payload must be an object")
        result = publication_from_payload(value)
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
                "WHERE change_set_id = %s AND owner = %s",
                (change_set_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(change_set_id)
        value = row["payload_json"]
        if not isinstance(value, dict):
            raise TypeError("Publication payload must be an object")
        return publication_from_payload(value)

    def replace_workspace_publication(
        self, publication: WorkspacePublication, *, expected_version: int
    ) -> WorkspacePublication:
        from pilot107.agent.publisher import publication_from_payload, publication_payload

        _version(expected_version)
        payload = publication_payload(replace(publication, version=expected_version + 1))
        with self.connect() as connection:
            row = connection.execute(
                """
                UPDATE agent_workspace_publications
                SET state = %s, version = %s, payload_json = %s, updated_at = %s
                WHERE change_set_id = %s AND owner = %s AND version = %s
                RETURNING payload_json
                """,
                (
                    payload["state"],
                    payload["version"],
                    self._jsonb(payload),
                    payload["updated_at"],
                    publication.change_set_id,
                    publication.owner,
                    expected_version,
                ),
            ).fetchone()
        if row is None:
            raise ProjectConflict("Publication version changed during update")
        value = row["payload_json"]
        if not isinstance(value, dict):
            raise TypeError("Publication payload must be an object")
        return publication_from_payload(value)

    def create_builder_submission(self, record: BuilderSubmissionRecord) -> BuilderSubmissionRecord:
        from pilot107.agent.builder_workflow import BuilderSubmissionRecord

        if not isinstance(record, BuilderSubmissionRecord):
            raise TypeError("record must be a BuilderSubmissionRecord")
        workspace = self.get_workspace(record.workspace_id, owner=record.owner)
        if workspace.project_id != record.project_id:
            raise ProjectConflict("Builder submission Project does not own the Workspace")
        receipt = None if record.receipt is None else self._jsonb(dict(record.receipt))
        with self.connect() as connection:
            row = connection.execute(
                """
                INSERT INTO agent_builder_submissions (
                    submission_id, owner, session_id, turn_id, project_id,
                    workspace_id, request_key, input_digest, phase, state,
                    version, base_change_set_id, change_set_id,
                    sandbox_result_id, task_id, receipt_json, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (owner, request_key) DO NOTHING
                RETURNING *
                """,
                (
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
                    receipt,
                    record.created_at,
                    record.updated_at,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM agent_builder_submissions WHERE owner = %s AND request_key = %s",
                    (record.owner, record.request_key),
                ).fetchone()
        if row is None:
            raise RuntimeError("Builder submission insert did not produce a row")
        result = _row_to_builder_submission(row)
        _assert_builder_submission_replay(result, record)
        return result

    def get_builder_submission(self, submission_id: str, *, owner: str) -> BuilderSubmissionRecord:
        _key(submission_id, "submission_id")
        _key(owner, "owner")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_builder_submissions WHERE submission_id = %s AND owner = %s",
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
                "SELECT * FROM agent_builder_submissions WHERE owner = %s AND request_key = %s",
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
            rows = connection.execute(
                "SELECT * FROM agent_builder_submissions "
                "WHERE owner = %s AND session_id = %s AND project_id = %s "
                "AND workspace_id = %s "
                "ORDER BY updated_at DESC, submission_id DESC LIMIT 100",
                (owner, session_id, project_id, workspace_id),
            ).fetchall()
        return _latest_builder_submission([_row_to_builder_submission(row) for row in rows])

    def list_builder_submissions(
        self,
        *,
        owner: str,
        session_id: str,
        project_id: str,
        workspace_id: str,
        limit: int = 100,
    ) -> list[BuilderSubmissionRecord]:
        for value, label in (
            (owner, "owner"),
            (session_id, "session_id"),
            (project_id, "project_id"),
            (workspace_id, "workspace_id"),
        ):
            _key(value, label)
        _limit(limit)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_builder_submissions "
                "WHERE owner = %s AND session_id = %s AND project_id = %s "
                "AND workspace_id = %s "
                "ORDER BY created_at ASC, submission_id ASC LIMIT %s",
                (owner, session_id, project_id, workspace_id, limit),
            ).fetchall()
        return [_row_to_builder_submission(row) for row in rows]

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
        receipt = None if record.receipt is None else self._jsonb(dict(record.receipt))
        with self.connect() as connection:
            row = connection.execute(
                """
                UPDATE agent_builder_submissions
                SET phase = %s, state = %s, version = %s,
                    base_change_set_id = %s, change_set_id = %s,
                    sandbox_result_id = %s, task_id = %s,
                    receipt_json = %s, updated_at = %s
                WHERE submission_id = %s AND owner = %s AND version = %s
                RETURNING *
                """,
                (
                    record.phase.value,
                    record.state.value,
                    record.version,
                    record.base_change_set_id,
                    record.change_set_id,
                    record.sandbox_result_id,
                    record.task_id,
                    receipt,
                    record.updated_at,
                    record.submission_id,
                    record.owner,
                    expected_version,
                ),
            ).fetchone()
        if row is None:
            raise BuilderSubmissionConflict("Builder submission version changed during update")
        return _row_to_builder_submission(row)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Project Store clock must return an aware datetime")
        return value.astimezone(UTC)
