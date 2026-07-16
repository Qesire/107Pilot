"""Ordered schema registry for platform observation read models."""

from pilot107.core.schema_migrations import SchemaMigration

PLATFORM_SNAPSHOT_MIGRATION = SchemaMigration(
    migration_id="003b.001.platform_snapshots",
    statements=(
        """
            CREATE TABLE platform_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                scope TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_name TEXT NOT NULL,
                collector_version TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                expires_at TEXT,
                collection_status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """,
        """
            CREATE INDEX idx_platform_snapshots_owner_captured
            ON platform_snapshots(owner, captured_at DESC, snapshot_id DESC)
        """,
        """
            CREATE INDEX idx_platform_snapshots_owner_scope_captured
            ON platform_snapshots(owner, scope, captured_at DESC, snapshot_id DESC)
        """,
        """
            CREATE INDEX idx_platform_snapshots_expiry
            ON platform_snapshots(expires_at)
        """,
    ),
)

USER_ENTITLEMENT_MIGRATION = SchemaMigration(
    migration_id="003b.002.user_entitlement_snapshots",
    statements=(
        """
            CREATE TABLE user_entitlement_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_name TEXT NOT NULL,
                collector_version TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                expires_at TEXT,
                data_quality TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """,
        """
            CREATE INDEX idx_user_entitlements_owner_captured
            ON user_entitlement_snapshots(owner, captured_at DESC, snapshot_id DESC)
        """,
        """
            CREATE INDEX idx_user_entitlements_expiry
            ON user_entitlement_snapshots(expires_at)
        """,
    ),
)

PLATFORM_MIGRATIONS = (
    PLATFORM_SNAPSHOT_MIGRATION,
    USER_ENTITLEMENT_MIGRATION,
)
