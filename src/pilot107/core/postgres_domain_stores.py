"""PostgreSQL-backed implementations of the persisted 107Pilot domains.

The public domain-store methods deliberately remain identical to their SQLite
counterparts.  This keeps SQLite as a useful local/offline implementation while
making API and Worker configuration select one *complete* database backend.

The adapter is intentionally narrow: it translates only the small SQLite SQL
dialect used by these stores (qmark parameters, ``INSERT OR IGNORE``, and two
JSON predicates).  Schema creation itself is native PostgreSQL and guarded by
the checksum migration runner in :mod:`postgres_domain_schema`.
"""

from __future__ import annotations

import importlib
import re
import sqlite3
from pathlib import Path
from typing import Any, cast

from pilot107.core.contracts import ContractService, ContractStore
from pilot107.core.platform_snapshot_store import PlatformSnapshotStore
from pilot107.core.postgres_control_repository import PostgresDriverUnavailable
from pilot107.core.postgres_domain_schema import initialize_postgres_domain_schema
from pilot107.core.remediation_store import RemediationStore
from pilot107.core.run_publications import RunPublicationStore
from pilot107.core.run_store import RunStore
from pilot107.core.template_market import TemplateMarketStore
from pilot107.core.template_policy import TemplatePublicationGate
from pilot107.core.user_entitlement_store import UserEntitlementStore

_INSERT_OR_IGNORE = re.compile(r"^(?P<prefix>\s*)INSERT\s+OR\s+IGNORE\s+INTO\b", re.IGNORECASE)
_JSON_PARTITIONS = re.compile(
    r"json_each\(releases\.compatibility_json,\s*'\$\.partitions'\)", re.IGNORECASE
)
_JSON_GPU = re.compile(r"json_extract\(releases\.compatibility_json,\s*'\$\.gpu'\)", re.IGNORECASE)


class PostgresDomainStoreError(RuntimeError):
    """Raised when a PostgreSQL business-store connection cannot be used."""


class _PostgresDomainConnection:
    """A minimal sqlite3-shaped connection over psycopg.

    Domain behavior lives in the existing, heavily tested store methods.  This
    adapter makes their portable subset execute in PostgreSQL transactions
    without pretending that PostgreSQL is SQLite at the schema layer.
    """

    def __init__(self, dsn: str, *, psycopg: Any, dict_row: Any) -> None:
        self._psycopg = psycopg
        self._connection = psycopg.connect(dsn, row_factory=dict_row)

    def __enter__(self) -> _PostgresDomainConnection:
        self._connection.__enter__()
        return self

    def __exit__(self, *args: object) -> bool | None:
        return cast(bool | None, self._connection.__exit__(*args))

    def execute(self, query: str, parameters: object = ()) -> Any:
        try:
            return self._connection.execute(_translate_sql(query), parameters)
        except self._psycopg.IntegrityError as exc:
            # Existing stores intentionally catch sqlite3.IntegrityError to
            # implement idempotent creates.  Preserve that public contract.
            raise sqlite3.IntegrityError(str(exc)) from exc

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


class _PostgresDomainStore:
    """Shared lifecycle and connection configuration for a domain store."""

    def _configure_postgres(self, *, dsn: str, compatibility_path: Path) -> None:
        if not dsn or any(character in dsn for character in "\r\n\0"):
            raise ValueError("PostgreSQL DSN is invalid")
        self.db_path = compatibility_path
        self.dsn = dsn
        try:
            self._psycopg = importlib.import_module("psycopg")
            self._dict_row = importlib.import_module("psycopg.rows").dict_row
        except ModuleNotFoundError as exc:
            raise PostgresDriverUnavailable(
                "install pilot107[postgres] to use PostgreSQL business repositories"
            ) from exc
        initialize_postgres_domain_schema(dsn)

    def _postgres_connect(self) -> _PostgresDomainConnection:
        return _PostgresDomainConnection(
            self.dsn,
            psycopg=self._psycopg,
            dict_row=self._dict_row,
        )


class PostgresRunStore(_PostgresDomainStore, RunStore):
    def __init__(self, dsn: str, *, compatibility_path: Path) -> None:
        self._configure_postgres(dsn=dsn, compatibility_path=compatibility_path)

    def _initialize(self) -> None:
        # _configure_postgres has already applied native PostgreSQL migrations.
        return None

    def connect(self) -> sqlite3.Connection:
        return cast(sqlite3.Connection, self._postgres_connect())


class PostgresContractStore(_PostgresDomainStore, ContractStore):
    def __init__(self, dsn: str, *, compatibility_path: Path) -> None:
        self._configure_postgres(dsn=dsn, compatibility_path=compatibility_path)

    def _initialize(self) -> None:
        return None

    def connect(self) -> sqlite3.Connection:
        return cast(sqlite3.Connection, self._postgres_connect())


class PostgresPlatformSnapshotStore(_PostgresDomainStore, PlatformSnapshotStore):
    def __init__(self, dsn: str, *, compatibility_path: Path) -> None:
        self._configure_postgres(dsn=dsn, compatibility_path=compatibility_path)

    def connect(self) -> sqlite3.Connection:
        return cast(sqlite3.Connection, self._postgres_connect())


class PostgresUserEntitlementStore(_PostgresDomainStore, UserEntitlementStore):
    def __init__(self, dsn: str, *, compatibility_path: Path) -> None:
        self._configure_postgres(dsn=dsn, compatibility_path=compatibility_path)

    def connect(self) -> sqlite3.Connection:
        return cast(sqlite3.Connection, self._postgres_connect())


class PostgresTemplateMarketStore(_PostgresDomainStore, TemplateMarketStore):
    def __init__(
        self,
        dsn: str,
        *,
        compatibility_path: Path,
        publication_gate: TemplatePublicationGate | None = None,
        contract_service: ContractService | None = None,
    ) -> None:
        self.publication_gate = publication_gate
        self.contract_service = contract_service
        self._configure_postgres(dsn=dsn, compatibility_path=compatibility_path)

    def connect(self) -> sqlite3.Connection:
        return cast(sqlite3.Connection, self._postgres_connect())


class PostgresRunPublicationStore(_PostgresDomainStore, RunPublicationStore):
    def __init__(
        self,
        dsn: str,
        *,
        compatibility_path: Path,
        run_store: RunStore,
        contract_service: ContractService | None = None,
    ) -> None:
        self.run_store = run_store
        self.contract_service = contract_service
        self._configure_postgres(dsn=dsn, compatibility_path=compatibility_path)

    def connect(self) -> sqlite3.Connection:
        return cast(sqlite3.Connection, self._postgres_connect())


class PostgresRemediationStore(_PostgresDomainStore, RemediationStore):
    def __init__(self, dsn: str, *, compatibility_path: Path) -> None:
        self._configure_postgres(dsn=dsn, compatibility_path=compatibility_path)

    def connect(self) -> sqlite3.Connection:
        return cast(sqlite3.Connection, self._postgres_connect())


def _translate_sql(query: str) -> str:
    """Translate the bounded SQLite query subset used by domain stores."""

    normalized = query.strip()
    if normalized.upper() == "BEGIN IMMEDIATE":
        return "BEGIN"
    if normalized.upper().startswith("PRAGMA "):
        # PostgreSQL schema initialization is native; these appear only in
        # SQLite connection setup, never in a PostgreSQL domain operation.
        raise PostgresDomainStoreError("SQLite PRAGMA is not valid for PostgreSQL")

    ignore_match = _INSERT_OR_IGNORE.match(normalized)
    if ignore_match is not None:
        normalized = _INSERT_OR_IGNORE.sub(r"\g<prefix>INSERT INTO", normalized, count=1)
        normalized = normalized.rstrip(";") + " ON CONFLICT DO NOTHING"

    normalized = normalized.replace("last_insert_rowid()", "LASTVAL()")
    normalized = _JSON_PARTITIONS.sub(
        "jsonb_array_elements_text("
        "COALESCE(releases.compatibility_json::jsonb -> 'partitions', '[]'::jsonb)"
        ") AS partitions(value)",
        normalized,
    )
    # SQLite's json_extract returns 0/1 for booleans, and the market store
    # deliberately binds an integer.  Keep that parameter contract in
    # PostgreSQL instead of comparing JSON text ("true"/"false") to a
    # smallint-bound placeholder.
    normalized = _JSON_GPU.sub(
        "CASE WHEN releases.compatibility_json::jsonb ->> 'gpu' = 'true' "
        "THEN 1 ELSE 0 END",
        normalized,
    )
    return normalized.replace("?", "%s")
