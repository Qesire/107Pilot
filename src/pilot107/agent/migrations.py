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
