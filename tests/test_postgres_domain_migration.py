import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

from pilot107.agent.market_sessions import SQLiteMarketSessionStore
from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.agent.task_store import SQLiteAgentTaskStore
from pilot107.core.contracts import ContractStore
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.core.file_uploads import UploadSessionStore
from pilot107.core.platform_snapshot_store import PlatformSnapshotStore
from pilot107.core.postgres_domain_migration import (
    DomainDataMigrationError,
    _canonical_row,
    _copy_table,
    migrate_sqlite_domain_to_postgres,
    verify_sqlite_domain_matches_postgres,
)
from pilot107.core.postgres_domain_schema import domain_table_names, persisted_table_names
from pilot107.core.postgres_domain_stores import _translate_sql
from pilot107.core.remediation_store import RemediationStore
from pilot107.core.repair_ticket_store import RepairTicketStore
from pilot107.core.run_publications import RunPublicationStore
from pilot107.core.run_store import RunStore
from pilot107.core.ssh_connections import SshConnectionStore
from pilot107.core.template_market import TemplateMarketStore
from pilot107.core.user_entitlement_store import UserEntitlementStore
from pilot107.observability.store import SQLiteObservabilityStore
from pilot107.runtime_watch.store import SQLiteRuntimeWatchStore


class PostgresDomainMigrationSafetyTests(unittest.TestCase):
    def test_transfer_requires_explicit_quiesce_before_any_database_access(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            self.assertRaises(DomainDataMigrationError) as raised,
        ):
            migrate_sqlite_domain_to_postgres(
                sqlite_path=Path(temporary) / "does-not-matter.db",
                postgres_dsn="postgresql://not-used",
                source_quiesced=False,
            )
        self.assertIn("source_quiesced", str(raised.exception))

    def test_verify_refuses_a_missing_sqlite_source(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            self.assertRaises(DomainDataMigrationError) as raised,
        ):
            verify_sqlite_domain_matches_postgres(
                sqlite_path=Path(temporary) / "missing.db",
                postgres_dsn="postgresql://not-used",
            )
        self.assertIn("does not exist", str(raised.exception))

    def test_domain_registry_covers_every_persisted_business_area(self) -> None:
        names = set(domain_table_names())
        self.assertTrue(
            {
                "runs",
                "workflow_manifests",
                "contracts",
                "platform_snapshots",
                "user_entitlement_snapshots",
                "template_releases",
                "run_publications",
                "run_publication_adoptions",
                "market_application_sessions",
                "template_publication_sessions",
                "remediation_sessions",
                "agent_sessions",
                "agent_turns",
                "agent_turn_events",
                "agent_tool_invocations",
                "agent_experiment_projects",
                "agent_workspaces",
                "agent_builder_submissions",
                "agent_tasks",
                "runtime_watches",
                "runtime_log_cursors",
                "runtime_log_segments",
                "runtime_alerts",
                "observation_cycles",
                "observation_run_targets",
                "artifact_manifests",
                "repair_tickets",
                "ssh_connection_sessions",
            }.issubset(names)
        )
        self.assertEqual(len(names), len(domain_table_names()))
        self.assertEqual(len(persisted_table_names()), len(domain_table_names()) + 3)

    def test_domain_registry_covers_the_complete_sqlite_runtime_schema(self) -> None:
        """A newly persisted SQLite domain must not silently miss PG cutover."""

        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "runtime.db"
            run_store = RunStore(database)
            ContractStore(database)
            PlatformSnapshotStore(database)
            UserEntitlementStore(database)
            TemplateMarketStore(database)
            RunPublicationStore(database, run_store=run_store)
            SQLiteMarketSessionStore(database)
            RemediationStore(database)
            RepairTicketStore(database)
            SshConnectionStore(database)
            UploadSessionStore(database)
            SQLiteAgentSessionStore(database)
            SQLiteProjectStore(database)
            SQLiteAgentTaskStore(database)
            SQLiteRuntimeWatchStore(
                database,
                segment_root=Path(temporary) / "runtime-segments",
            )
            SQLiteObservabilityStore(database)
            SQLiteControlRepository(database)
            with sqlite3.connect(database) as connection:
                sqlite_tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }

        # ``sqlite_sequence`` is SQLite bookkeeping for INTEGER PRIMARY KEY
        # columns. ``schema_migrations`` is backend-specific migration
        # metadata: PostgreSQL recreates and verifies it natively instead of
        # importing SQLite's history. Every application-owned table must have
        # a PG counterpart.
        sqlite_tables.discard("sqlite_sequence")
        sqlite_tables.discard("schema_migrations")
        self.assertEqual(sqlite_tables, set(persisted_table_names()))


class PostgresDomainSqlTranslationTests(unittest.TestCase):
    def test_sqlite_idempotency_and_event_queries_are_native_postgres(self) -> None:
        self.assertEqual(
            _translate_sql("INSERT OR IGNORE INTO runs (run_id) VALUES (?)"),
            "INSERT INTO runs (run_id) VALUES (%s) ON CONFLICT DO NOTHING",
        )
        self.assertEqual(
            _translate_sql("SELECT last_insert_rowid()"),
            "SELECT LASTVAL()",
        )
        self.assertEqual(_translate_sql("BEGIN IMMEDIATE"), "BEGIN")

    def test_template_json_filters_translate_without_sqlite_json_functions(self) -> None:
        translated = _translate_sql(
            "SELECT 1 FROM json_each(releases.compatibility_json, '$.partitions') "
            "WHERE value = ? AND json_extract(releases.compatibility_json, '$.gpu') = ?"
        )
        self.assertNotIn("json_each", translated)
        self.assertNotIn("json_extract", translated)
        self.assertIn("jsonb_array_elements_text", translated)
        self.assertIn("THEN 1 ELSE 0", translated)
        self.assertEqual(translated.count("%s"), 2)


class PostgresAgentSessionSchemaTests(unittest.TestCase):
    def test_evidence_integrity_gate_migration_is_additive_and_complete(self) -> None:
        from pilot107.core.postgres_domain_schema import _MIGRATIONS

        migration_id, statements = next(
            migration
            for migration in _MIGRATIONS
            if migration[0] == "004a.026.evidence_object_integrity_gates"
        )
        schema = "\n".join(statements)

        self.assertEqual(migration_id, "004a.026.evidence_object_integrity_gates")
        self.assertNotIn("CREATE TABLE", schema)
        for column in (
            "workspace_revision",
            "workspace_digest",
            "source_revision",
            "platform_snapshot_ref",
            "integrity_checked_at",
            "integrity_object_set_digest",
        ):
            self.assertIn(f"ADD COLUMN IF NOT EXISTS {column}", schema)

    def test_run_provenance_migration_is_additive_and_complete(self) -> None:
        from pilot107.core.postgres_domain_schema import _MIGRATIONS

        migration_id, statements = next(
            migration
            for migration in _MIGRATIONS
            if migration[0] == "004a.027.run_provenance"
        )
        schema = "\n".join(statements)

        self.assertEqual(migration_id, "004a.027.run_provenance")
        self.assertNotIn("CREATE TABLE", schema)
        for column in (
            "workspace_revision",
            "workspace_digest",
            "source_revision",
            "platform_snapshot_ref",
        ):
            self.assertIn(f"ADD COLUMN IF NOT EXISTS {column}", schema)

    def test_evidence_integrity_migration_installs_immutable_guard(self) -> None:
        from pilot107.core.postgres_domain_schema import _MIGRATIONS

        migration_id, statements = next(
            migration
            for migration in _MIGRATIONS
            if migration[0] == "004a.028.evidence_object_immutable_guard"
        )
        schema = "\n".join(statements)

        self.assertEqual(migration_id, "004a.028.evidence_object_immutable_guard")
        self.assertIn("ADD COLUMN IF NOT EXISTS integrity_invalidated_at", schema)
        self.assertIn("pilot107_evidence_object_integrity_guard", schema)
        self.assertIn("DROP TRIGGER IF EXISTS", schema)
        self.assertIn("CREATE TRIGGER evidence_objects_integrity_guard", schema)
        self.assertIn("IS DISTINCT FROM", schema)

    def test_evidence_seal_migration_adds_recoverable_run_state(self) -> None:
        from pilot107.core.postgres_domain_schema import _MIGRATIONS

        migration_id, statements = next(
            migration
            for migration in _MIGRATIONS
            if migration[0] == "004a.030.evidence_seal_state"
        )
        schema = "\n".join(statements)

        self.assertEqual(migration_id, "004a.030.evidence_seal_state")
        for column in (
            "evidence_seal_state",
            "evidence_seal_digest",
            "evidence_seal_marker_ref",
            "evidence_sealed_at",
            "evidence_seal_invalid_reason",
        ):
            self.assertIn(f"ADD COLUMN IF NOT EXISTS {column}", schema)
        self.assertIn("DEFAULT 'OPEN'", schema)

    def test_run_provenance_guard_migration_freezes_all_effective_sources(self) -> None:
        from pilot107.core.postgres_domain_schema import _MIGRATIONS

        migration_id, statements = next(
            migration
            for migration in _MIGRATIONS
            if migration[0] == "004a.031.run_provenance_immutable_guard"
        )
        schema = "\n".join(statements)

        self.assertEqual(migration_id, "004a.031.run_provenance_immutable_guard")
        self.assertIn("pilot107_run_provenance_immutable_guard", schema)
        for column in (
            "workspace_revision",
            "workspace_digest",
            "source_revision",
            "platform_snapshot_ref",
            "resource_plan_json",
        ):
            self.assertIn(f"NEW.{column} IS DISTINCT FROM OLD.{column}", schema)
        self.assertIn("CREATE TRIGGER runs_provenance_immutable_guard", schema)

    def test_agent_migration_uses_native_postgres_types_and_fencing_index(self) -> None:
        from pilot107.core.postgres_domain_schema import _MIGRATIONS

        migration_id, statements = next(
            migration for migration in _MIGRATIONS if migration[0] == "004a.005.agent_sessions"
        )
        schema = "\n".join(statements)

        self.assertEqual(migration_id, "004a.005.agent_sessions")
        self.assertIn("CREATE TABLE agent_sessions", schema)
        self.assertIn("CREATE TABLE agent_turns", schema)
        self.assertIn("CREATE TABLE agent_turn_events", schema)
        self.assertIn("CREATE TABLE agent_tool_invocations", schema)
        self.assertIn("JSONB", schema)
        self.assertIn("BIGSERIAL", schema)
        self.assertIn("TIMESTAMPTZ", schema)
        self.assertIn("WHERE state = 'running'", schema)

    def test_project_migration_uses_jsonb_and_optimistic_versioning(self) -> None:
        from pilot107.core.postgres_domain_schema import _MIGRATIONS

        migration_id, statements = next(
            migration
            for migration in _MIGRATIONS
            if migration[0] == "004a.006.agent_experiment_projects"
        )
        schema = "\n".join(statements)

        self.assertEqual(migration_id, "004a.006.agent_experiment_projects")
        self.assertIn("CREATE TABLE agent_experiment_projects", schema)
        self.assertIn("blueprint_json JSONB", schema)
        self.assertIn("version INTEGER NOT NULL", schema)
        self.assertIn("UNIQUE (owner, request_key)", schema)

    def test_workspace_migration_uses_jsonb_and_project_foreign_key(self) -> None:
        from pilot107.core.postgres_domain_schema import _MIGRATIONS

        migration_id, statements = next(
            migration
            for migration in _MIGRATIONS
            if migration[0] == "004a.007.agent_workspaces"
        )
        schema = "\n".join(statements)

        self.assertEqual(migration_id, "004a.007.agent_workspaces")
        self.assertIn("CREATE TABLE agent_workspaces", schema)
        self.assertIn("REFERENCES agent_experiment_projects", schema)
        self.assertIn("payload_json JSONB", schema)

    def test_builder_submission_migration_is_durable_and_idempotent(self) -> None:
        from pilot107.core.postgres_domain_schema import _MIGRATIONS

        migration_id, statements = next(
            migration
            for migration in _MIGRATIONS
            if migration[0] == "004a.023.agent_builder_submissions"
        )
        schema = "\n".join(statements)

        self.assertEqual(migration_id, "004a.023.agent_builder_submissions")
        self.assertIn("CREATE TABLE agent_builder_submissions", schema)
        self.assertIn("receipt_json JSONB", schema)
        self.assertIn("UNIQUE (owner, request_key)", schema)
        self.assertIn("REFERENCES agent_experiment_projects", schema)
        self.assertIn("REFERENCES agent_workspaces", schema)


class PostgresDomainCopyTests(unittest.TestCase):
    def test_control_timestamps_have_one_cross_backend_canonical_form(self) -> None:
        source = _canonical_row(
            "control_outbox",
            {
                "available_at": "2026-07-26T04:00:00+00:00",
                "lease_expires_at": None,
                "created_at": "2026-07-26T04:00:00+00:00",
                "updated_at": "2026-07-26T04:00:00+00:00",
            },
        )
        target = _canonical_row(
            "control_outbox",
            {
                "available_at": datetime.fromisoformat("2026-07-26T12:00:00+08:00"),
                "lease_expires_at": None,
                "created_at": datetime.fromisoformat("2026-07-26T04:00:00+00:00"),
                "updated_at": datetime.fromisoformat("2026-07-26T04:00:00+00:00"),
            },
        )

        self.assertEqual(source, target)

    def test_copy_uses_sqlite_row_column_names_not_row_values(self) -> None:
        class Cursor:
            def __init__(self, target: "Target") -> None:
                self.target = target

            def executemany(self, statement: str, values: list[tuple[Any, ...]]) -> None:
                self.target.statement = statement
                self.target.values = values

        class Target:
            statement = ""
            values: list[tuple[Any, ...]] = []

            def cursor(self) -> Cursor:
                return Cursor(self)

        with tempfile.TemporaryDirectory() as temporary:
            source = sqlite3.connect(Path(temporary) / "source.db")
            try:
                source.execute("CREATE TABLE rows_to_copy (id TEXT, payload TEXT)")
                source.execute("INSERT INTO rows_to_copy VALUES (?, ?)", ("run_source", "value"))
                source.row_factory = sqlite3.Row
                target = Target()

                _copy_table(source, target, "rows_to_copy")

                self.assertEqual(
                    target.statement,
                    "INSERT INTO rows_to_copy (id, payload) VALUES (%s, %s)",
                )
                self.assertEqual(target.values, [("run_source", "value")])
            finally:
                source.close()


if __name__ == "__main__":
    unittest.main()
