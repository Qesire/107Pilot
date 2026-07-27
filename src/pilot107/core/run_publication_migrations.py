"""Schema migrations for owner-confirmed successful-run publications."""

from pilot107.core.platform_migrations import PLATFORM_MIGRATIONS
from pilot107.core.schema_migrations import SchemaMigration

RUN_PUBLICATION_MIGRATION = SchemaMigration(
    migration_id="003d.001.run_publications",
    statements=(
        """
        CREATE TABLE run_publications (
            publication_id TEXT PRIMARY KEY,
            source_run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
            source_contract_id TEXT,
            owner TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            visibility TEXT NOT NULL,
            scope_key TEXT,
            tags_json TEXT NOT NULL DEFAULT '[]',
            reproduction_note TEXT NOT NULL DEFAULT '',
            request_key TEXT NOT NULL,
            published_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            withdrawn_at TEXT,
            withdrawal_actor TEXT,
            withdrawal_reason TEXT,
            UNIQUE(owner, request_key),
            CHECK (visibility IN ('private', 'course', 'campus', 'public'))
        )
        """,
        """
        CREATE INDEX idx_run_publications_market
        ON run_publications(visibility, published_at DESC, publication_id DESC)
        """,
        """
        CREATE INDEX idx_run_publications_owner
        ON run_publications(owner, published_at DESC, publication_id DESC)
        """,
        """
        CREATE TABLE run_publication_adoptions (
            adoption_id TEXT PRIMARY KEY,
            publication_id TEXT NOT NULL REFERENCES run_publications(publication_id),
            adopter TEXT NOT NULL,
            request_key TEXT NOT NULL,
            target_contract_id TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(adopter, request_key)
        )
        """,
        """
        CREATE INDEX idx_run_publication_adoptions_adopter
        ON run_publication_adoptions(adopter, created_at DESC, adoption_id DESC)
        """,
        """
        CREATE UNIQUE INDEX idx_run_publication_adoptions_target_contract
        ON run_publication_adoptions(target_contract_id)
        WHERE target_contract_id IS NOT NULL
        """,
    ),
)


RUN_PUBLICATION_MIGRATIONS = (*PLATFORM_MIGRATIONS, RUN_PUBLICATION_MIGRATION)
