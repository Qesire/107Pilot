"""Fail-closed PostgreSQL selection for every durable lifecycle store.

SQLite is retired as a runtime/production authority. ``DatabaseMode.SQLITE``
is retained only as a deprecation sentinel so stale configuration receives an
explicit migration error rather than silently opening a local database.

The ``sqlite_path`` argument is temporarily kept in builder signatures because
PostgreSQL compatibility adapters still use it as a non-authoritative local
path. This module never constructs a SQLite store.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from pilot107.agent.checkpoint_durability import ensure_postgres_checkpoint_pointer
from pilot107.agent.postgres_project_store import PostgresProjectStore
from pilot107.agent.postgres_store import PostgresAgentSessionStore
from pilot107.agent.postgres_task_store import PostgresAgentTaskStore
from pilot107.agent.project_store import ProjectStore
from pilot107.agent.store import AgentSessionStore
from pilot107.agent.task_store import AgentTaskStore
from pilot107.core.postgres_domain_stores import PostgresMarketSessionStore


class ConfigurationError(ValueError):
    """Durable repositories cannot be resolved to one PostgreSQL authority."""


class DatabaseMode(StrEnum):
    """Durable database modes.

    ``SQLITE`` is a rejected legacy value, not a supported runtime mode.
    """

    SQLITE = "sqlite"
    POSTGRES = "postgres"


@dataclass(frozen=True)
class DurableStoreSelection:
    mode: DatabaseMode
    sqlite_path: Path = field(repr=False)
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
    """Resolve exactly one PostgreSQL backend before any repository performs I/O."""

    try:
        mode = DatabaseMode(database_mode)
    except ValueError as exc:
        raise ConfigurationError("durable database mode must be postgres") from exc

    if mode is DatabaseMode.SQLITE:
        raise ConfigurationError("SQLite runtime authority has been retired; configure PostgreSQL")
    if runtime_watch_sqlite_path is not None or observation_sqlite_path is not None:
        raise ConfigurationError(
            "SQLite runtime authority has been retired; "
            "lifecycle store overrides must use PostgreSQL"
        )
    if postgres_dsn is None or control_postgres_dsn is None:
        raise ConfigurationError(
            "PostgreSQL mode requires domain and control repositories; "
            "SQLite runtime authority has been retired"
        )
    if _postgres_database_identity(postgres_dsn) != _postgres_database_identity(
        control_postgres_dsn
    ):
        raise ConfigurationError(
            "domain and control repositories must use the same PostgreSQL database"
        )
    return DurableStoreSelection(
        mode=DatabaseMode.POSTGRES,
        sqlite_path=sqlite_path.resolve(),
        postgres_dsn=postgres_dsn,
        control_postgres_dsn=control_postgres_dsn,
    )


def build_market_session_store(*, selection: DurableStoreSelection) -> PostgresMarketSessionStore:
    dsn = _required_postgres_dsn(selection, component="market session")
    return PostgresMarketSessionStore(
        dsn,
        compatibility_path=selection.sqlite_path,
    )


def build_agent_session_store(*, sqlite_path: Path, postgres_dsn: str | None) -> AgentSessionStore:
    del sqlite_path
    dsn = _require_builder_dsn(postgres_dsn, component="Agent session")
    store = PostgresAgentSessionStore(dsn)
    ensure_postgres_checkpoint_pointer(dsn)
    return store


def build_project_store(*, sqlite_path: Path, postgres_dsn: str | None) -> ProjectStore:
    del sqlite_path
    dsn = _require_builder_dsn(postgres_dsn, component="Agent project")
    return PostgresProjectStore(dsn)


def build_agent_task_store(*, sqlite_path: Path, postgres_dsn: str | None) -> AgentTaskStore:
    del sqlite_path
    dsn = _require_builder_dsn(postgres_dsn, component="Agent task")
    return PostgresAgentTaskStore(dsn)


def _required_postgres_dsn(
    selection: DurableStoreSelection,
    *,
    component: str,
) -> str:
    if selection.mode is not DatabaseMode.POSTGRES or not selection.postgres_dsn:
        raise ConfigurationError(
            f"{component} store requires PostgreSQL; SQLite runtime authority has been retired"
        )
    return selection.postgres_dsn


def _require_builder_dsn(value: str | None, *, component: str) -> str:
    if not value:
        raise ConfigurationError(
            f"{component} store requires PostgreSQL; SQLite runtime authority has been retired"
        )
    return value


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
            values = dict(item.split("=", 1) for item in shlex.split(dsn) if "=" in item)
        except ValueError as exc:
            raise ConfigurationError("PostgreSQL DSN is invalid") from exc
        host = values.get("host", "localhost")
        port = values.get("port", "5432")
        database = values.get("dbname") or values.get("database") or ""
    if not host or not port or not database:
        raise ConfigurationError("PostgreSQL DSN must identify a database")
    return (host.casefold(), str(port), database)
