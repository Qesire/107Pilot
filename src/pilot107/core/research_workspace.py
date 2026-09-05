"""Research Workspace boundary aggregate for the scientific provenance graph.

A ResearchWorkspace is the user-level boundary of one research context.  It is
*not* the Agent's isolated filesystem ``AgentWorkspaceRecord`` and it is not a
linear Experiment lifecycle.  The aggregate owns only durable membership edges
between existing domain facts:

- Contracts may be referenced by multiple Research Workspaces.
- A Run has exactly one primary Research Workspace boundary once bound.
- An Agent Project has exactly one primary Research Workspace boundary once bound.
- File/data/model references may be reused by multiple Research Workspaces.
- Evidence, diagnoses and repair lineage are reached through Run/AgentProject
  provenance and are deliberately not duplicated here.

This module is PostgreSQL-only because PostgreSQL is the production authority.
SQLite remains a development/test implementation for the older domain stores;
we do not create a second production ResearchWorkspace authority.
"""

from __future__ import annotations

import hashlib
import importlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pilot107.core.postgres_domain_schema import initialize_postgres_domain_schema

_MIGRATION_ID = "006c.002.research_workspace_boundary"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_ASSET_KINDS = frozenset({"file", "directory", "dataset", "model", "code", "external"})

_MIGRATION_STATEMENTS = (
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


class ResearchWorkspaceConflict(RuntimeError):
    """Raised when an edge would violate the research boundary contract."""


@dataclass(frozen=True)
class ResearchWorkspaceRecord:
    research_workspace_id: str
    owner: str
    request_key: str
    title: str
    description: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ResearchWorkspaceAssetRef:
    asset_ref: str
    asset_kind: str
    linked_at: str


@dataclass(frozen=True)
class ResearchWorkspaceGraph:
    workspace: ResearchWorkspaceRecord
    contract_ids: tuple[str, ...]
    run_ids: tuple[str, ...]
    agent_project_ids: tuple[str, ...]
    assets: tuple[ResearchWorkspaceAssetRef, ...]


class PostgresResearchWorkspaceStore:
    """PostgreSQL authority for ResearchWorkspace identity and graph edges."""

    def __init__(self, dsn: str) -> None:
        if not dsn or any(character in dsn for character in "\r\n\0"):
            raise ValueError("PostgreSQL DSN is invalid")
        self.dsn = dsn
        try:
            self._psycopg = importlib.import_module("psycopg")
            self._dict_row = importlib.import_module("psycopg.rows").dict_row
        except ModuleNotFoundError as exc:
            raise RuntimeError("install pilot107[postgres] to use ResearchWorkspace") from exc
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
        research_workspace_id: str | None = None,
    ) -> ResearchWorkspaceRecord:
        _owner(owner)
        _key(request_key, "request_key")
        title = _text(title, "title", limit=256, required=True)
        description = _text(description, "description", limit=8_000, required=False)
        workspace_id = research_workspace_id or _workspace_id(owner, request_key)
        _key(workspace_id, "research_workspace_id")
        now = datetime.now(UTC)
        with self.connect() as connection, connection.transaction():
            row = connection.execute(
                """
                INSERT INTO research_workspaces (
                    research_workspace_id, owner, request_key, title, description,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (owner, request_key) DO NOTHING
                RETURNING *
                """,
                (workspace_id, owner, request_key, title, description, now, now),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """
                    SELECT * FROM research_workspaces
                    WHERE owner = %s AND request_key = %s
                    """,
                    (owner, request_key),
                ).fetchone()
            if row is None:
                raise RuntimeError("ResearchWorkspace idempotent create disappeared")
            record = _row_to_workspace(row)
            if (
                record.research_workspace_id != workspace_id
                or record.title != title
                or record.description != description
            ):
                raise ResearchWorkspaceConflict(
                    "ResearchWorkspace request_key refers to different content"
                )
            return record

    def get(self, research_workspace_id: str, *, owner: str) -> ResearchWorkspaceRecord:
        _key(research_workspace_id, "research_workspace_id")
        _owner(owner)
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM research_workspaces
                WHERE research_workspace_id = %s AND owner = %s
                """,
                (research_workspace_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(research_workspace_id)
        return _row_to_workspace(row)

    def list(self, *, owner: str, limit: int = 100) -> list[ResearchWorkspaceRecord]:
        _owner(owner)
        if limit <= 0 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM research_workspaces
                WHERE owner = %s
                ORDER BY updated_at DESC, research_workspace_id DESC
                LIMIT %s
                """,
                (owner, limit),
            ).fetchall()
        return [_row_to_workspace(row) for row in rows]

    def link_contract(
        self,
        research_workspace_id: str,
        *,
        owner: str,
        contract_id: str,
    ) -> None:
        self._assert_workspace(research_workspace_id, owner)
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
                raise ResearchWorkspaceConflict("Contract owner does not match ResearchWorkspace")
            connection.execute(
                """
                INSERT INTO research_workspace_contracts (
                    research_workspace_id, contract_id, linked_at
                ) VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (research_workspace_id, contract_id, now),
            )
            self._touch(connection, research_workspace_id, owner, now)

    def link_run(
        self,
        research_workspace_id: str,
        *,
        owner: str,
        run_id: str,
    ) -> None:
        """Bind one immutable execution attempt to exactly one primary boundary.

        Parent/child Run lineage may cross ResearchWorkspace boundaries.  The
        edge here only states where *this* Run was executed and therefore
        requires the Run's immutable workspace revision/digest provenance.
        """

        self._assert_workspace(research_workspace_id, owner)
        _key(run_id, "run_id")
        now = datetime.now(UTC)
        with self.connect() as connection, connection.transaction():
            run = connection.execute(
                """
                SELECT owner, workspace_revision, workspace_digest
                FROM runs WHERE run_id = %s
                """,
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if str(run["owner"]) != owner:
                raise ResearchWorkspaceConflict("Run owner does not match ResearchWorkspace")
            revision = run["workspace_revision"]
            digest = run["workspace_digest"]
            if revision is None or digest is None:
                raise ResearchWorkspaceConflict(
                    "Workspace-bound Run must carry immutable workspace revision/digest provenance"
                )
            existing = connection.execute(
                "SELECT research_workspace_id FROM research_workspace_runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["research_workspace_id"]) != research_workspace_id:
                    raise ResearchWorkspaceConflict(
                        "Run is already bound to another ResearchWorkspace"
                    )
                return
            connection.execute(
                """
                INSERT INTO research_workspace_runs (
                    research_workspace_id, run_id, workspace_revision,
                    workspace_digest, linked_at
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (research_workspace_id, run_id, int(revision), str(digest), now),
            )
            self._touch(connection, research_workspace_id, owner, now)

    def link_agent_project(
        self,
        research_workspace_id: str,
        *,
        owner: str,
        project_id: str,
    ) -> None:
        """Attach the optional Agent engineering subsystem to one boundary."""

        self._assert_workspace(research_workspace_id, owner)
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
                raise ResearchWorkspaceConflict(
                    "Agent Project owner does not match ResearchWorkspace"
                )
            existing = connection.execute(
                """
                SELECT research_workspace_id
                FROM research_workspace_agent_projects
                WHERE project_id = %s
                """,
                (project_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["research_workspace_id"]) != research_workspace_id:
                    raise ResearchWorkspaceConflict(
                        "Agent Project is already bound to another ResearchWorkspace"
                    )
                return
            connection.execute(
                """
                INSERT INTO research_workspace_agent_projects (
                    research_workspace_id, project_id, linked_at
                ) VALUES (%s, %s, %s)
                """,
                (research_workspace_id, project_id, now),
            )
            self._touch(connection, research_workspace_id, owner, now)

    def link_asset(
        self,
        research_workspace_id: str,
        *,
        owner: str,
        asset_ref: str,
        asset_kind: str,
    ) -> None:
        """Add a preparation/reference asset without creating metadata authority."""

        self._assert_workspace(research_workspace_id, owner)
        asset_ref = _text(asset_ref, "asset_ref", limit=4_096, required=True)
        if asset_kind not in _ASSET_KINDS:
            raise ValueError(f"unsupported asset_kind: {asset_kind}")
        now = datetime.now(UTC)
        with self.connect() as connection, connection.transaction():
            existing = connection.execute(
                """
                SELECT asset_kind FROM research_workspace_assets
                WHERE research_workspace_id = %s AND asset_ref = %s
                """,
                (research_workspace_id, asset_ref),
            ).fetchone()
            if existing is not None:
                if str(existing["asset_kind"]) != asset_kind:
                    raise ResearchWorkspaceConflict(
                        "Asset reference is already linked with a different kind"
                    )
                return
            connection.execute(
                """
                INSERT INTO research_workspace_assets (
                    research_workspace_id, asset_ref, asset_kind, linked_at
                ) VALUES (%s, %s, %s, %s)
                """,
                (research_workspace_id, asset_ref, asset_kind, now),
            )
            self._touch(connection, research_workspace_id, owner, now)

    def get_run_workspace(self, run_id: str, *, owner: str) -> ResearchWorkspaceRecord | None:
        _key(run_id, "run_id")
        _owner(owner)
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT workspace.*
                FROM research_workspace_runs AS edge
                JOIN research_workspaces AS workspace
                  ON workspace.research_workspace_id = edge.research_workspace_id
                JOIN runs AS run ON run.run_id = edge.run_id
                WHERE edge.run_id = %s AND workspace.owner = %s AND run.owner = %s
                """,
                (run_id, owner, owner),
            ).fetchone()
        return None if row is None else _row_to_workspace(row)

    def graph(self, research_workspace_id: str, *, owner: str) -> ResearchWorkspaceGraph:
        workspace = self.get(research_workspace_id, owner=owner)
        with self.connect() as connection:
            contracts = connection.execute(
                """
                SELECT contract_id FROM research_workspace_contracts
                WHERE research_workspace_id = %s ORDER BY linked_at, contract_id
                """,
                (research_workspace_id,),
            ).fetchall()
            runs = connection.execute(
                """
                SELECT run_id FROM research_workspace_runs
                WHERE research_workspace_id = %s ORDER BY linked_at, run_id
                """,
                (research_workspace_id,),
            ).fetchall()
            projects = connection.execute(
                """
                SELECT project_id FROM research_workspace_agent_projects
                WHERE research_workspace_id = %s ORDER BY linked_at, project_id
                """,
                (research_workspace_id,),
            ).fetchall()
            assets = connection.execute(
                """
                SELECT asset_ref, asset_kind, linked_at FROM research_workspace_assets
                WHERE research_workspace_id = %s ORDER BY linked_at, asset_ref
                """,
                (research_workspace_id,),
            ).fetchall()
        return ResearchWorkspaceGraph(
            workspace=workspace,
            contract_ids=tuple(str(row["contract_id"]) for row in contracts),
            run_ids=tuple(str(row["run_id"]) for row in runs),
            agent_project_ids=tuple(str(row["project_id"]) for row in projects),
            assets=tuple(
                ResearchWorkspaceAssetRef(
                    asset_ref=str(row["asset_ref"]),
                    asset_kind=str(row["asset_kind"]),
                    linked_at=_timestamp_text(row["linked_at"]),
                )
                for row in assets
            ),
        )

    def _assert_workspace(self, research_workspace_id: str, owner: str) -> None:
        self.get(research_workspace_id, owner=owner)

    @staticmethod
    def _touch(connection: Any, workspace_id: str, owner: str, now: datetime) -> None:
        updated = connection.execute(
            """
            UPDATE research_workspaces SET updated_at = %s
            WHERE research_workspace_id = %s AND owner = %s
            """,
            (now, workspace_id, owner),
        )
        if updated.rowcount != 1:
            raise ResearchWorkspaceConflict("ResearchWorkspace boundary changed during link")

    def _ensure_schema(self) -> None:
        checksum = _migration_checksum(_MIGRATION_STATEMENTS)
        with self.connect() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("pilot107:migrations",),
            )
            existing = connection.execute(
                "SELECT checksum FROM schema_migrations WHERE migration_id = %s",
                (_MIGRATION_ID,),
            ).fetchone()
            if existing is not None:
                if str(existing["checksum"]) != checksum:
                    raise RuntimeError(f"migration checksum changed: {_MIGRATION_ID}")
                return
            for statement in _MIGRATION_STATEMENTS:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO schema_migrations (migration_id, checksum, applied_at)
                VALUES (%s, %s, %s)
                """,
                (_MIGRATION_ID, checksum, datetime.now(UTC)),
            )


def _workspace_id(owner: str, request_key: str) -> str:
    digest = hashlib.sha256(f"{owner}\0{request_key}".encode()).hexdigest()
    return f"research-ws-{digest[:24]}"


def _migration_checksum(statements: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for statement in statements:
        digest.update(statement.strip().encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _row_to_workspace(row: Any) -> ResearchWorkspaceRecord:
    return ResearchWorkspaceRecord(
        research_workspace_id=str(row["research_workspace_id"]),
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
    _key(value, "owner")


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
    "PostgresResearchWorkspaceStore",
    "ResearchWorkspaceAssetRef",
    "ResearchWorkspaceConflict",
    "ResearchWorkspaceGraph",
    "ResearchWorkspaceRecord",
]
