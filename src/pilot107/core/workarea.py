"""WorkArea boundary aggregate for the scientific provenance graph.

``WorkArea`` is the product-level boundary of one research/experiment context.
It is deliberately different from the Agent's ``Workspace``: Workspace is
reserved for the Agent-owned, writable, versioned and recoverable filesystem
context.

WorkArea has no lifecycle state machine. It owns only durable membership edges
between existing domain facts:

- Contracts may be reused by multiple WorkAreas.
- A Run has one primary WorkArea once bound; Run lineage may cross WorkAreas.
- An Agent Project has one primary WorkArea once bound.
- File/data/model references may be reused by multiple WorkAreas.
- Evidence, diagnosis and repair facts are reached through Run/AgentProject
  provenance and are not duplicated here.

PostgreSQL is the production authority. SQLite is not given a second production
WorkArea implementation.
"""

from __future__ import annotations

import hashlib
import importlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pilot107.core.identity import is_safe_username
from pilot107.core.postgres_domain_schema import initialize_postgres_domain_schema

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_ASSET_KINDS = frozenset({"file", "directory", "dataset", "model", "code", "external"})

# Frozen historical migrations. Do not edit: their checksums may already exist
# in schema_migrations. Terminology correction is performed by 006c.004.
_MIGRATION_002_ID = "006c.002.research_workspace_boundary"
_MIGRATION_002_STATEMENTS = (
    """
    CREATE TABLE research_workspaces (
        research_workspace_id TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        request_key TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        UNIQUE (owner, request_key)
    )
    """,
    """
    CREATE INDEX idx_research_workspaces_owner_updated
    ON research_workspaces(owner, updated_at DESC, research_workspace_id DESC)
    """,
    """
    CREATE TABLE research_workspace_contracts (
        research_workspace_id TEXT NOT NULL
            REFERENCES research_workspaces(research_workspace_id) ON DELETE CASCADE,
        contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
        linked_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (research_workspace_id, contract_id)
    )
    """,
    """
    CREATE INDEX idx_research_workspace_contracts_contract
    ON research_workspace_contracts(contract_id, research_workspace_id)
    """,
    """
    CREATE TABLE research_workspace_runs (
        research_workspace_id TEXT NOT NULL
            REFERENCES research_workspaces(research_workspace_id) ON DELETE CASCADE,
        run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
        workspace_revision BIGINT NOT NULL,
        workspace_digest TEXT NOT NULL,
        linked_at TIMESTAMPTZ NOT NULL,
        CHECK (workspace_revision > 0)
    )
    """,
    """
    CREATE INDEX idx_research_workspace_runs_workspace
    ON research_workspace_runs(research_workspace_id, linked_at, run_id)
    """,
    """
    CREATE TABLE research_workspace_agent_projects (
        research_workspace_id TEXT NOT NULL
            REFERENCES research_workspaces(research_workspace_id) ON DELETE CASCADE,
        project_id TEXT PRIMARY KEY REFERENCES agent_experiment_projects(project_id),
        linked_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE INDEX idx_research_workspace_agent_projects_workspace
    ON research_workspace_agent_projects(research_workspace_id, linked_at, project_id)
    """,
    """
    CREATE TABLE research_workspace_assets (
        research_workspace_id TEXT NOT NULL
            REFERENCES research_workspaces(research_workspace_id) ON DELETE CASCADE,
        asset_ref TEXT NOT NULL,
        asset_kind TEXT NOT NULL,
        linked_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (research_workspace_id, asset_ref),
        CHECK (asset_kind IN ('file', 'directory', 'dataset', 'model', 'code', 'external'))
    )
    """,
    """
    CREATE INDEX idx_research_workspace_assets_ref
    ON research_workspace_assets(asset_ref, research_workspace_id)
    """,
)

_MIGRATION_003_ID = "006c.003.research_workspace_run_edge_normalization"
_MIGRATION_003_STATEMENTS = (
    "ALTER TABLE research_workspace_runs DROP COLUMN workspace_revision",
    "ALTER TABLE research_workspace_runs DROP COLUMN workspace_digest",
)

# Terminology authority: WorkArea is the research/experiment boundary;
# Workspace is exclusively Agent terminology from this migration onward.
_MIGRATION_004_ID = "006c.004.workarea_terminology"
_MIGRATION_004_STATEMENTS = (
    "ALTER TABLE research_workspaces RENAME TO workareas",
    "ALTER TABLE research_workspace_contracts RENAME TO workarea_contracts",
    "ALTER TABLE research_workspace_runs RENAME TO workarea_runs",
    "ALTER TABLE research_workspace_agent_projects RENAME TO workarea_agent_projects",
    "ALTER TABLE research_workspace_assets RENAME TO workarea_assets",
    "ALTER TABLE workareas RENAME COLUMN research_workspace_id TO workarea_id",
    "ALTER TABLE workarea_contracts RENAME COLUMN research_workspace_id TO workarea_id",
    "ALTER TABLE workarea_runs RENAME COLUMN research_workspace_id TO workarea_id",
    "ALTER TABLE workarea_agent_projects RENAME COLUMN research_workspace_id TO workarea_id",
    "ALTER TABLE workarea_assets RENAME COLUMN research_workspace_id TO workarea_id",
    "ALTER INDEX idx_research_workspaces_owner_updated RENAME TO idx_workareas_owner_updated",
    "ALTER INDEX idx_research_workspace_contracts_contract RENAME TO idx_workarea_contracts_contract",
    "ALTER INDEX idx_research_workspace_runs_workspace RENAME TO idx_workarea_runs_workarea",
    "ALTER INDEX idx_research_workspace_agent_projects_workspace RENAME TO idx_workarea_agent_projects_workarea",
    "ALTER INDEX idx_research_workspace_assets_ref RENAME TO idx_workarea_assets_ref",
)

_MIGRATIONS = (
    (_MIGRATION_002_ID, _MIGRATION_002_STATEMENTS),
    (_MIGRATION_003_ID, _MIGRATION_003_STATEMENTS),
    (_MIGRATION_004_ID, _MIGRATION_004_STATEMENTS),
)


class WorkAreaConflict(RuntimeError):
    """Raised when an edge would violate the WorkArea boundary contract."""


@dataclass(frozen=True)
class WorkAreaRecord:
    workarea_id: str
    owner: str
    request_key: str
    title: str
    description: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class WorkAreaAssetRef:
    asset_ref: str
    asset_kind: str
    linked_at: str


@dataclass(frozen=True)
class WorkAreaGraph:
    """Local provenance graph projection, never a lifecycle state object."""

    workarea: WorkAreaRecord
    contract_ids: tuple[str, ...]
    run_ids: tuple[str, ...]
    agent_project_ids: tuple[str, ...]
    assets: tuple[WorkAreaAssetRef, ...]


class PostgresWorkAreaStore:
    """PostgreSQL authority for WorkArea identity and membership edges."""

    def __init__(self, dsn: str) -> None:
        if not dsn or any(character in dsn for character in "\r\n\0"):
            raise ValueError("PostgreSQL DSN is invalid")
        self.dsn = dsn
        try:
            self._psycopg = importlib.import_module("psycopg")
            self._dict_row = importlib.import_module("psycopg.rows").dict_row
        except ModuleNotFoundError as exc:
            raise RuntimeError("install pilot107[postgres] to use WorkArea") from exc
        initialize_postgres_domain_schema(dsn)
        self._ensure_schema()

    def connect(self) -> Any:
        return self._psycopg.connect(self.dsn, row_factory=self._dict_row)

    def create(
        self,
        *,
        owner: str,
        request_key: str,
        title: str,
        description: str = "",
        workarea_id: str | None = None,
    ) -> WorkAreaRecord:
        _owner(owner)
        _key(request_key, "request_key")
        title = _text(title, "title", limit=256, required=True)
        description = _text(description, "description", limit=8_000, required=False)
        explicit_id = workarea_id is not None
        candidate_id = workarea_id or _workarea_id(owner, request_key)
        _key(candidate_id, "workarea_id")
        now = datetime.now(UTC)
        with self.connect() as connection, connection.transaction():
            row = connection.execute(
                """
                INSERT INTO workareas (
                    workarea_id, owner, request_key, title, description,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (owner, request_key) DO NOTHING
                RETURNING *
                """,
                (candidate_id, owner, request_key, title, description, now, now),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM workareas WHERE owner = %s AND request_key = %s",
                    (owner, request_key),
                ).fetchone()
            if row is None:
                raise RuntimeError("WorkArea idempotent create disappeared")
            record = _row_to_workarea(row)
            if (
                (explicit_id and record.workarea_id != candidate_id)
                or record.title != title
                or record.description != description
            ):
                raise WorkAreaConflict("WorkArea request_key refers to different content")
            return record

    def get(self, workarea_id: str, *, owner: str) -> WorkAreaRecord:
        _key(workarea_id, "workarea_id")
        _owner(owner)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM workareas WHERE workarea_id = %s AND owner = %s",
                (workarea_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(workarea_id)
        return _row_to_workarea(row)

    def list(self, *, owner: str, limit: int = 100) -> list[WorkAreaRecord]:
        _owner(owner)
        if limit <= 0 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workareas
                WHERE owner = %s
                ORDER BY updated_at DESC, workarea_id DESC
                LIMIT %s
                """,
                (owner, limit),
            ).fetchall()
        return [_row_to_workarea(row) for row in rows]

    def link_contract(self, workarea_id: str, *, owner: str, contract_id: str) -> None:
        self._assert_workarea(workarea_id, owner)
        _key(contract_id, "contract_id")
        now = datetime.now(UTC)
        with self.connect() as connection, connection.transaction():
            referenced = connection.execute(
                "SELECT owner FROM contracts WHERE contract_id = %s",
                (contract_id,),
            ).fetchone()
            if referenced is None:
                raise KeyError(contract_id)
            if str(referenced["owner"]) != owner:
                raise WorkAreaConflict("Contract owner does not match WorkArea")
            connection.execute(
                """
                INSERT INTO workarea_contracts (workarea_id, contract_id, linked_at)
                VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
                """,
                (workarea_id, contract_id, now),
            )
            self._touch(connection, workarea_id, owner, now)

    def link_run(self, workarea_id: str, *, owner: str, run_id: str) -> None:
        """Bind a Run to one primary WorkArea.

        This membership edge is independent of Agent Workspace provenance. A
        normal Contract Run, an Agent-produced Run, or a child whose parent is
        in another WorkArea can all be bound here.
        """

        self._assert_workarea(workarea_id, owner)
        _key(run_id, "run_id")
        now = datetime.now(UTC)
        with self.connect() as connection, connection.transaction():
            run = connection.execute(
                "SELECT owner FROM runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if str(run["owner"]) != owner:
                raise WorkAreaConflict("Run owner does not match WorkArea")
            existing = connection.execute(
                "SELECT workarea_id FROM workarea_runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["workarea_id"]) != workarea_id:
                    raise WorkAreaConflict("Run is already bound to another WorkArea")
                return
            connection.execute(
                "INSERT INTO workarea_runs (workarea_id, run_id, linked_at) VALUES (%s, %s, %s)",
                (workarea_id, run_id, now),
            )
            self._touch(connection, workarea_id, owner, now)

    def link_agent_project(self, workarea_id: str, *, owner: str, project_id: str) -> None:
        """Attach the optional Agent engineering subsystem to one WorkArea."""

        self._assert_workarea(workarea_id, owner)
        _key(project_id, "project_id")
        now = datetime.now(UTC)
        with self.connect() as connection, connection.transaction():
            project = connection.execute(
                "SELECT owner FROM agent_experiment_projects WHERE project_id = %s",
                (project_id,),
            ).fetchone()
            if project is None:
                raise KeyError(project_id)
            if str(project["owner"]) != owner:
                raise WorkAreaConflict("Agent Project owner does not match WorkArea")
            existing = connection.execute(
                "SELECT workarea_id FROM workarea_agent_projects WHERE project_id = %s",
                (project_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["workarea_id"]) != workarea_id:
                    raise WorkAreaConflict("Agent Project is already bound to another WorkArea")
                return
            connection.execute(
                """
                INSERT INTO workarea_agent_projects (workarea_id, project_id, linked_at)
                VALUES (%s, %s, %s)
                """,
                (workarea_id, project_id, now),
            )
            self._touch(connection, workarea_id, owner, now)

    def link_asset(
        self,
        workarea_id: str,
        *,
        owner: str,
        asset_ref: str,
        asset_kind: str,
    ) -> None:
        """Link a preparation/reference asset without creating metadata authority."""

        self._assert_workarea(workarea_id, owner)
        asset_ref = _text(asset_ref, "asset_ref", limit=4_096, required=True)
        if asset_kind not in _ASSET_KINDS:
            raise ValueError(f"unsupported asset_kind: {asset_kind}")
        now = datetime.now(UTC)
        with self.connect() as connection, connection.transaction():
            existing = connection.execute(
                """
                SELECT asset_kind FROM workarea_assets
                WHERE workarea_id = %s AND asset_ref = %s
                """,
                (workarea_id, asset_ref),
            ).fetchone()
            if existing is not None:
                if str(existing["asset_kind"]) != asset_kind:
                    raise WorkAreaConflict(
                        "Asset reference is already linked with a different kind"
                    )
                return
            connection.execute(
                """
                INSERT INTO workarea_assets (workarea_id, asset_ref, asset_kind, linked_at)
                VALUES (%s, %s, %s, %s)
                """,
                (workarea_id, asset_ref, asset_kind, now),
            )
            self._touch(connection, workarea_id, owner, now)

    def get_run_workarea(self, run_id: str, *, owner: str) -> WorkAreaRecord | None:
        _key(run_id, "run_id")
        _owner(owner)
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT area.*
                FROM workarea_runs AS edge
                JOIN workareas AS area ON area.workarea_id = edge.workarea_id
                JOIN runs AS run ON run.run_id = edge.run_id
                WHERE edge.run_id = %s AND area.owner = %s AND run.owner = %s
                """,
                (run_id, owner, owner),
            ).fetchone()
        return None if row is None else _row_to_workarea(row)

    def graph(self, workarea_id: str, *, owner: str) -> WorkAreaGraph:
        workarea = self.get(workarea_id, owner=owner)
        with self.connect() as connection:
            contracts = connection.execute(
                """
                SELECT contract_id FROM workarea_contracts
                WHERE workarea_id = %s ORDER BY linked_at, contract_id
                """,
                (workarea_id,),
            ).fetchall()
            runs = connection.execute(
                """
                SELECT run_id FROM workarea_runs
                WHERE workarea_id = %s ORDER BY linked_at, run_id
                """,
                (workarea_id,),
            ).fetchall()
            projects = connection.execute(
                """
                SELECT project_id FROM workarea_agent_projects
                WHERE workarea_id = %s ORDER BY linked_at, project_id
                """,
                (workarea_id,),
            ).fetchall()
            assets = connection.execute(
                """
                SELECT asset_ref, asset_kind, linked_at FROM workarea_assets
                WHERE workarea_id = %s ORDER BY linked_at, asset_ref
                """,
                (workarea_id,),
            ).fetchall()
        return WorkAreaGraph(
            workarea=workarea,
            contract_ids=tuple(str(row["contract_id"]) for row in contracts),
            run_ids=tuple(str(row["run_id"]) for row in runs),
            agent_project_ids=tuple(str(row["project_id"]) for row in projects),
            assets=tuple(
                WorkAreaAssetRef(
                    asset_ref=str(row["asset_ref"]),
                    asset_kind=str(row["asset_kind"]),
                    linked_at=_timestamp_text(row["linked_at"]),
                )
                for row in assets
            ),
        )

    def _assert_workarea(self, workarea_id: str, owner: str) -> None:
        self.get(workarea_id, owner=owner)

    @staticmethod
    def _touch(connection: Any, workarea_id: str, owner: str, now: datetime) -> None:
        updated = connection.execute(
            """
            UPDATE workareas SET updated_at = %s
            WHERE workarea_id = %s AND owner = %s
            """,
            (now, workarea_id, owner),
        )
        if updated.rowcount != 1:
            raise WorkAreaConflict("WorkArea boundary changed during link")

    def _ensure_schema(self) -> None:
        with self.connect() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("pilot107:migrations",),
            )
            for migration_id, statements in _MIGRATIONS:
                checksum = _migration_checksum(statements)
                existing = connection.execute(
                    "SELECT checksum FROM schema_migrations WHERE migration_id = %s",
                    (migration_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing["checksum"]) != checksum:
                        raise RuntimeError(f"migration checksum changed: {migration_id}")
                    continue
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO schema_migrations (migration_id, checksum, applied_at)
                    VALUES (%s, %s, %s)
                    """,
                    (migration_id, checksum, datetime.now(UTC)),
                )


def _workarea_id(owner: str, request_key: str) -> str:
    digest = hashlib.sha256(f"{owner}\0{request_key}".encode()).hexdigest()
    return f"workarea-{digest[:24]}"


def _migration_checksum(statements: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for statement in statements:
        digest.update(statement.strip().encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _row_to_workarea(row: Any) -> WorkAreaRecord:
    return WorkAreaRecord(
        workarea_id=str(row["workarea_id"]),
        owner=str(row["owner"]),
        request_key=str(row["request_key"]),
        title=str(row["title"]),
        description=str(row["description"]),
        created_at=_timestamp_text(row["created_at"]),
        updated_at=_timestamp_text(row["updated_at"]),
    )


def _timestamp_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)


def _owner(value: str) -> None:
    if not is_safe_username(value):
        raise ValueError("owner is invalid")


def _key(value: str, field: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} is invalid")


def _text(value: str, field: str, *, limit: int, required: bool) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    normalized = value.strip()
    if required and not normalized:
        raise ValueError(f"{field} is required")
    if "\0" in normalized or len(normalized) > limit:
        raise ValueError(f"{field} is invalid")
    return normalized


__all__ = [
    "PostgresWorkAreaStore",
    "WorkAreaAssetRef",
    "WorkAreaConflict",
    "WorkAreaGraph",
    "WorkAreaRecord",
]
