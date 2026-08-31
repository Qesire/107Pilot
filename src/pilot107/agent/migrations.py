"""Checksum-verified SQLite migrations for durable Agent Sessions."""

from pilot107.core.schema_migrations import SchemaMigration

AGENT_SESSION_MIGRATIONS = (
    SchemaMigration(
        migration_id="006a.001.agent_sessions",
        statements=(
            """
            CREATE TABLE agent_sessions (
                session_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                request_key TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                model_profile_id TEXT NOT NULL,
                source_json TEXT NOT NULL,
                state TEXT NOT NULL,
                state_version INTEGER NOT NULL,
                context_checkpoint_json TEXT,
                resource_usage_json TEXT NOT NULL,
                outcome_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (owner, request_key),
                CHECK (state IN ('idle', 'queued', 'running')),
                CHECK (state_version > 0)
            )
            """,
            """
            CREATE INDEX idx_agent_sessions_owner_updated
            ON agent_sessions(owner, updated_at DESC, session_id DESC)
            """,
            """
            CREATE TABLE agent_turns (
                turn_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
                owner TEXT NOT NULL,
                request_key TEXT NOT NULL,
                input_digest TEXT NOT NULL,
                message TEXT NOT NULL,
                state_version INTEGER NOT NULL,
                state TEXT NOT NULL,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                lease_owner TEXT,
                lease_expires_at TEXT,
                fencing_token INTEGER NOT NULL DEFAULT 0,
                event_sequence INTEGER NOT NULL DEFAULT 0,
                final_checkpoint_json TEXT,
                error_json TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                UNIQUE (session_id, request_key),
                CHECK (state IN (
                    'queued', 'running', 'interrupted', 'completed', 'cancelled', 'failed'
                )),
                CHECK (state_version > 0),
                CHECK (cancel_requested IN (0, 1)),
                CHECK (fencing_token >= 0),
                CHECK (event_sequence >= 0)
            )
            """,
            """
            CREATE INDEX idx_agent_turns_recoverable
            ON agent_turns(state, lease_expires_at, created_at, turn_id)
            """,
            """
            CREATE INDEX idx_agent_turns_owner_session
            ON agent_turns(owner, session_id, created_at, turn_id)
            """,
            """
            CREATE UNIQUE INDEX uq_agent_turns_running_owner
            ON agent_turns(owner) WHERE state = 'running'
            """,
            """
            CREATE TABLE agent_turn_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                turn_id TEXT NOT NULL REFERENCES agent_turns(turn_id),
                session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
                owner TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (turn_id, sequence),
                CHECK (sequence > 0)
            )
            """,
            """
            CREATE INDEX idx_agent_turn_events_owner_session
            ON agent_turn_events(owner, session_id, event_id)
            """,
            """
            CREATE TABLE agent_tool_invocations (
                invocation_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL,
                turn_id TEXT NOT NULL REFERENCES agent_turns(turn_id),
                session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
                owner TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_digest TEXT NOT NULL,
                state TEXT NOT NULL,
                result_json TEXT,
                error_json TEXT,
                bytes_returned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (turn_id, idempotency_key),
                CHECK (state IN ('running', 'completed', 'failed')),
                CHECK (bytes_returned >= 0)
            )
            """,
            """
            CREATE INDEX idx_agent_tool_invocations_owner_turn
            ON agent_tool_invocations(owner, turn_id, created_at, invocation_id)
            """,
        ),
    ),
)

# Task persistence uses its own migration history because agent_tasks is an
# independent lifecycle table.  This migration is additive so old task rows
# remain readable and are explicitly marked for gate reconciliation.
AGENT_TASK_EVIDENCE_GATE_MIGRATION = SchemaMigration(
    migration_id="006c.002.agent_task_evidence_gates",
    statements=(
        "ALTER TABLE agent_tasks ADD COLUMN completion_policy TEXT NOT NULL "
        "DEFAULT 'evidence_required'",
        "ALTER TABLE agent_tasks ADD COLUMN gate_state TEXT NOT NULL DEFAULT 'created'",
        "ALTER TABLE agent_tasks ADD COLUMN schedule_receipt_ref TEXT",
        "ALTER TABLE agent_tasks ADD COLUMN schedule_receipt TEXT",
        "ALTER TABLE agent_tasks ADD COLUMN evidence_refs_json TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE agent_tasks ADD COLUMN evidence_digest TEXT",
        "ALTER TABLE agent_tasks ADD COLUMN integrity_checked_at TEXT",
        "ALTER TABLE agent_tasks ADD COLUMN capsule_ref TEXT",
        "ALTER TABLE agent_tasks ADD COLUMN capsule_state TEXT NOT NULL DEFAULT 'not_required'",
        "ALTER TABLE agent_tasks ADD COLUMN gate_receipt TEXT",
        "ALTER TABLE agent_tasks ADD COLUMN causation_root_key TEXT",
        "ALTER TABLE agent_tasks ADD COLUMN durable_operation_key TEXT",
        "ALTER TABLE agent_tasks ADD COLUMN reconciliation_attempt INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE agent_tasks ADD COLUMN heartbeat_at TEXT",
        "ALTER TABLE agent_tasks ADD COLUMN legacy_gate_unverified INTEGER NOT NULL DEFAULT 1",
        "CREATE INDEX idx_agent_tasks_gate_reconciliation ON agent_tasks("
        "gate_state, reconciliation_attempt, updated_at)",
    ),
)

AGENT_TASK_STAGE_IDENTITY_MIGRATION = SchemaMigration(
    migration_id="006c.003.agent_task_stage_identities",
    statements=(
        "ALTER TABLE agent_tasks ADD COLUMN schedule_operation_key TEXT",
        "ALTER TABLE agent_tasks ADD COLUMN gate_operation_key TEXT",
    ),
)

AGENT_TASK_READY_RECOVERY_MIGRATION = SchemaMigration(
    migration_id="006c.004.agent_task_ready_recovery",
    statements=(
        "ALTER TABLE agent_tasks ADD COLUMN ready_outbox_pending INTEGER NOT NULL DEFAULT 0",
        "CREATE INDEX idx_agent_tasks_ready_recovery ON agent_tasks("
        "ready_outbox_pending, updated_at, task_id)",
    ),
)
