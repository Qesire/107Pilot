"""Ordered SQLite schema for repair tickets and artifact manifests."""

from pilot107.core.schema_migrations import SchemaMigration

REPAIR_TICKET_MIGRATION = SchemaMigration(
    migration_id="005a.001.repair_tickets",
    statements=(
        """
        CREATE TABLE artifact_manifests (
            manifest_id TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            run_id TEXT,
            revision TEXT NOT NULL,
            dirty_diff_digest TEXT,
            bundle_digest TEXT,
            remote_workdir TEXT,
            local_test_summary TEXT,
            disclosure TEXT NOT NULL DEFAULT 'metadata_only',
            created_at TEXT NOT NULL,
            CHECK (disclosure IN ('metadata_only', 'summary'))
        )
        """,
        """
        CREATE INDEX idx_artifact_manifests_owner
        ON artifact_manifests(owner, created_at DESC)
        """,
        """
        CREATE INDEX idx_artifact_manifests_run
        ON artifact_manifests(run_id, created_at DESC)
        """,
        """
        CREATE TABLE repair_tickets (
            ticket_id TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'open',
            source_run_id TEXT NOT NULL,
            source_contract_id TEXT,
            session_id TEXT,
            diagnosis_ids_json TEXT NOT NULL DEFAULT '[]',
            cited_facts_json TEXT NOT NULL DEFAULT '[]',
            code_context_json TEXT,
            requested_change TEXT,
            no_go_constraints_json TEXT NOT NULL DEFAULT '[]',
            resolution_manifest_id TEXT REFERENCES artifact_manifests(manifest_id),
            resolution_run_id TEXT,
            resolution_comparison_json TEXT,
            abandon_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (state IN ('open', 'resolved', 'abandoned'))
        )
        """,
        """
        CREATE INDEX idx_repair_tickets_owner_state
        ON repair_tickets(owner, state, updated_at DESC, ticket_id DESC)
        """,
        """
        CREATE INDEX idx_repair_tickets_source_run
        ON repair_tickets(source_run_id, created_at)
        """,
        """
        CREATE INDEX idx_repair_tickets_session
        ON repair_tickets(session_id, created_at)
        """,
    ),
)

REPAIR_TICKET_MIGRATIONS = (REPAIR_TICKET_MIGRATION,)
