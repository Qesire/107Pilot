"""Fail-closed runtime selection for every durable lifecycle store."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from pilot107.agent.market_sessions import SQLiteMarketSessionStore
from pilot107.agent.postgres_project_store import PostgresProjectStore
from pilot107.agent.postgres_store import PostgresAgentSessionStore
from pilot107.agent.postgres_task_store import PostgresAgentTaskStore
from pilot107.agent.project_store import ProjectStore, SQLiteProjectStore
from pilot107.agent.store import AgentSessionStore, SQLiteAgentSessionStore
from pilot107.agent.task_store import AgentTaskStore, SQLiteAgentTaskStore
from pilot107.core.postgres_domain_stores import PostgresMarketSessionStore


class ConfigurationError(ValueError):
    """Durable repositories cannot be resolved to one database identity."""


class DatabaseMode(StrEnum):
    SQLITE = "sqlite"
    POSTGRES = "postgres"


@dataclass(frozen=True)
class DurableStoreSelection:
    mode: DatabaseMode
    sqlite_path: Path
    postgres_dsn: str | None = field(default=None, repr=False)
    control_postgres_dsn: str | None = field(default=None, repr=False)

    @property
    def is_postgres(self) -> bool:
        return self.mode is DatabaseMode.POSTGRES


def resolve_durable_store_selection(
    *,
    database_mode: DatabaseMode | str,
    sqlite_path: Path,
    postgres_dsn: str | None,
    control_postgres_dsn: str | None,
    runtime_watch_sqlite_path: Path | None = None,
    observation_sqlite_path: Path | None = None,
) -> DurableStoreSelection:
    """Resolve exactly one backend before any repository performs I/O."""

    try:
        mode = DatabaseMode(database_mode)
    except ValueError as exc:
        raise ConfigurationError("database mode must be sqlite or postgres") from exc
    primary_sqlite = sqlite_path.resolve()
    sqlite_overrides = tuple(
        item.resolve()
        for item in (runtime_watch_sqlite_path, observation_sqlite_path)
        if item is not None
    )
    if mode is DatabaseMode.SQLITE:
        if postgres_dsn is not None or control_postgres_dsn is not None:
            raise ConfigurationError(
                "mixed durable stores: sqlite mode cannot select PostgreSQL repositories"
            )
        if any(item != primary_sqlite for item in sqlite_overrides):
            raise ConfigurationError(
                "mixed durable stores: all SQLite lifecycle stores must use one database"
            )
        return DurableStoreSelection(mode=mode, sqlite_path=primary_sqlite)

    if sqlite_overrides:
        raise ConfigurationError(
            "mixed durable stores: postgres mode cannot select SQLite lifecycle stores"
        )
    if postgres_dsn is None or control_postgres_dsn is None:
        raise ConfigurationError(
            "mixed durable stores: postgres mode requires domain and control repositories"
        )
    if _postgres_database_identity(postgres_dsn) != _postgres_database_identity(
        control_postgres_dsn
    ):
        raise ConfigurationError(
            "domain and control repositories must use the same PostgreSQL database"
        )
    return DurableStoreSelection(
        mode=mode,
        sqlite_path=primary_sqlite,
        postgres_dsn=postgres_dsn,
        control_postgres_dsn=control_postgres_dsn,
    )


def build_market_session_store(
    *, selection: DurableStoreSelection
) -> SQLiteMarketSessionStore:
    if selection.is_postgres:
        assert selection.postgres_dsn is not None
        return PostgresMarketSessionStore(
            selection.postgres_dsn,
            compatibility_path=selection.sqlite_path,
        )
    return SQLiteMarketSessionStore(selection.sqlite_path)


def build_agent_session_store(
    *, sqlite_path: Path, postgres_dsn: str | None
) -> AgentSessionStore:
    if postgres_dsn:
        return PostgresAgentSessionStore(postgres_dsn)
    return SQLiteAgentSessionStore(sqlite_path)


def build_project_store(*, sqlite_path: Path, postgres_dsn: str | None) -> ProjectStore:
    if postgres_dsn:
        return PostgresProjectStore(postgres_dsn)
    return SQLiteProjectStore(sqlite_path)


def build_agent_task_store(
    *, sqlite_path: Path, postgres_dsn: str | None
) -> AgentTaskStore:
    if postgres_dsn:
        return PostgresAgentTaskStore(postgres_dsn)
    return SQLiteAgentTaskStore(sqlite_path)


def _postgres_database_identity(dsn: str) -> tuple[str, str, str]:
    """Compare server/database identity while deliberately ignoring credentials."""

    if not dsn or any(character in dsn for character in "\r\n\0"):
        raise ConfigurationError("PostgreSQL DSN is invalid")
    if dsn.startswith(("postgresql://", "postgres://")):
        parsed = urlsplit(dsn)
        query = parse_qs(parsed.query)
        host = query.get("host", [parsed.hostname or "localhost"])[-1]
        port = query.get("port", [str(parsed.port or 5432)])[-1]
        database = query.get("dbname", [unquote(parsed.path.lstrip("/"))])[-1]
    else:
        try:
            values = dict(
                item.split("=", 1)
                for item in shlex.split(dsn)
                if "=" in item
            )
        except ValueError as exc:
            raise ConfigurationError("PostgreSQL DSN is invalid") from exc
        host = values.get("host", "localhost")
        port = values.get("port", "5432")
        database = values.get("dbname") or values.get("database") or ""
    if not host or not port or not database:
        raise ConfigurationError("PostgreSQL DSN must identify a database")
    return (host.casefold(), str(port), database)
