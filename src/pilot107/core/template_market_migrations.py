"""Ordered schema registry for template-market persistence."""

from pilot107.core.platform_migrations import PLATFORM_MIGRATIONS
from pilot107.core.schema_migrations import SchemaMigration

TEMPLATE_MARKET_MIGRATION = SchemaMigration(
    migration_id="003c.001.template_market",
    statements=(
        """
        CREATE TABLE template_drafts (
            draft_id TEXT PRIMARY KEY,
            template_id TEXT NOT NULL UNIQUE,
            owner TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            visibility TEXT NOT NULL,
            scope_key TEXT,
            state TEXT NOT NULL,
            version INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            compatibility_json TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (visibility IN ('private', 'course', 'campus', 'public')),
            CHECK (state IN (
                'editable', 'submitted', 'approved', 'rejected', 'published', 'archived'
            )),
            CHECK (version > 0)
        )
        """,
        """
        CREATE INDEX idx_template_drafts_owner_updated
        ON template_drafts(owner, updated_at DESC, draft_id DESC)
        """,
        """
        CREATE TABLE template_reviews (
            review_id TEXT PRIMARY KEY,
            draft_id TEXT NOT NULL REFERENCES template_drafts(draft_id),
            requester TEXT NOT NULL,
            reviewer TEXT,
            state TEXT NOT NULL,
            version INTEGER NOT NULL,
            draft_version INTEGER NOT NULL,
            content_sha256 TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            decided_at TEXT,
            CHECK (state IN ('pending', 'approved', 'rejected', 'withdrawn')),
            CHECK (version > 0),
            CHECK (draft_version > 0)
        )
        """,
        """
        CREATE UNIQUE INDEX idx_template_reviews_one_pending
        ON template_reviews(draft_id) WHERE state = 'pending'
        """,
        """
        CREATE INDEX idx_template_reviews_state_created
        ON template_reviews(state, created_at, review_id)
        """,
        """
        CREATE TABLE template_releases (
            release_id TEXT PRIMARY KEY,
            template_id TEXT NOT NULL,
            release_version TEXT NOT NULL,
            source_draft_id TEXT NOT NULL REFERENCES template_drafts(draft_id),
            source_draft_version INTEGER NOT NULL,
            review_id TEXT NOT NULL UNIQUE REFERENCES template_reviews(review_id),
            publisher TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            visibility TEXT NOT NULL,
            scope_key TEXT,
            payload_json TEXT NOT NULL,
            compatibility_json TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            published_at TEXT NOT NULL,
            UNIQUE(template_id, release_version),
            CHECK (visibility IN ('private', 'course', 'campus', 'public')),
            CHECK (source_draft_version > 0)
        )
        """,
        """
        CREATE INDEX idx_template_releases_market
        ON template_releases(visibility, published_at DESC, release_id DESC)
        """,
        """
        CREATE TRIGGER template_releases_immutable_update
        BEFORE UPDATE ON template_releases
        BEGIN
            SELECT RAISE(ABORT, 'template releases are immutable');
        END
        """,
        """
        CREATE TRIGGER template_releases_immutable_delete
        BEFORE DELETE ON template_releases
        BEGIN
            SELECT RAISE(ABORT, 'template releases are immutable');
        END
        """,
        """
        CREATE TABLE template_release_withdrawals (
            release_id TEXT PRIMARY KEY REFERENCES template_releases(release_id),
            actor TEXT NOT NULL,
            reason TEXT NOT NULL,
            withdrawn_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE template_adoptions (
            adoption_id TEXT PRIMARY KEY,
            release_id TEXT NOT NULL REFERENCES template_releases(release_id),
            adopter TEXT NOT NULL,
            request_key TEXT NOT NULL,
            target_template_id TEXT NOT NULL,
            target_draft_id TEXT NOT NULL REFERENCES template_drafts(draft_id),
            created_at TEXT NOT NULL,
            UNIQUE(adopter, request_key)
        )
        """,
        """
        CREATE INDEX idx_template_adoptions_adopter_created
        ON template_adoptions(adopter, created_at DESC, adoption_id DESC)
        """,
        """
        CREATE TABLE template_verifications (
            verification_id TEXT PRIMARY KEY,
            release_id TEXT NOT NULL REFERENCES template_releases(release_id),
            run_id TEXT,
            environment TEXT NOT NULL,
            status TEXT NOT NULL,
            evidence_ref TEXT,
            verified_at TEXT NOT NULL,
            CHECK (status IN ('passed', 'failed', 'expired'))
        )
        """,
        """
        CREATE INDEX idx_template_verifications_release_verified
        ON template_verifications(release_id, verified_at DESC, verification_id DESC)
        """,
    ),
)

TEMPLATE_MARKET_POLICY_MIGRATION = SchemaMigration(
    migration_id="003c.002.template_publication_policy",
    statements=(
        "ALTER TABLE template_drafts ADD COLUMN publication_json TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE template_reviews ADD COLUMN reviewer_role TEXT",
        "ALTER TABLE template_reviews ADD COLUMN reviewer_scope_key TEXT",
        "ALTER TABLE template_reviews ADD COLUMN gate_report_json TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE template_reviews ADD COLUMN validated_at TEXT",
        "ALTER TABLE template_releases ADD COLUMN publication_json TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE template_releases ADD COLUMN gate_report_json TEXT NOT NULL DEFAULT '{}'",
    ),
)

TEMPLATE_MARKET_API_MIGRATION = SchemaMigration(
    migration_id="003c.003.template_api_idempotency",
    statements=(
        "ALTER TABLE template_releases ADD COLUMN request_key TEXT",
        """
        CREATE UNIQUE INDEX idx_template_releases_publisher_request_key
        ON template_releases(publisher, request_key) WHERE request_key IS NOT NULL
        """,
    ),
)

TEMPLATE_MARKET_VERTICAL_MIGRATION = SchemaMigration(
    migration_id="003c.004.template_market_vertical",
    statements=(
        "ALTER TABLE template_adoptions ADD COLUMN target_contract_id TEXT",
        """
        CREATE UNIQUE INDEX idx_template_adoptions_target_contract
        ON template_adoptions(target_contract_id) WHERE target_contract_id IS NOT NULL
        """,
        "ALTER TABLE template_verifications ADD COLUMN verified_by TEXT",
        "ALTER TABLE template_verifications ADD COLUMN request_key TEXT",
        "ALTER TABLE template_verifications ADD COLUMN evidence_sha256 TEXT",
        "ALTER TABLE template_verifications ADD COLUMN detail_json TEXT NOT NULL DEFAULT '{}'",
        """
        CREATE UNIQUE INDEX idx_template_verifications_actor_request
        ON template_verifications(verified_by, request_key)
        WHERE verified_by IS NOT NULL AND request_key IS NOT NULL
        """,
        """
        CREATE UNIQUE INDEX idx_template_verifications_release_run_environment
        ON template_verifications(release_id, run_id, environment)
        WHERE run_id IS NOT NULL
        """,
    ),
)

TEMPLATE_MARKET_MIGRATIONS = (
    *PLATFORM_MIGRATIONS,
    TEMPLATE_MARKET_MIGRATION,
    TEMPLATE_MARKET_POLICY_MIGRATION,
    TEMPLATE_MARKET_API_MIGRATION,
    TEMPLATE_MARKET_VERTICAL_MIGRATION,
)
