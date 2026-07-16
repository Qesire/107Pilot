"""Ordered SQLite schema for persistent remediation sessions."""

from pilot107.core.schema_migrations import SchemaMigration

REMEDIATION_SESSION_MIGRATION = SchemaMigration(
    migration_id="003e.001.remediation_sessions",
    statements=(
        """
        CREATE TABLE remediation_sessions (
            session_id TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            request_key TEXT NOT NULL,
            state TEXT NOT NULL,
            version INTEGER NOT NULL,
            source_run_id TEXT NOT NULL REFERENCES runs(run_id),
            source_contract_id TEXT,
            source_diagnosis_digest TEXT NOT NULL,
            source_evidence_digest TEXT NOT NULL,
            automation_policy TEXT NOT NULL,
            budget_json TEXT NOT NULL,
            usage_json TEXT NOT NULL,
            stop_reason TEXT,
            takeover_reason TEXT,
            lease_owner TEXT,
            lease_expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(owner, request_key),
            CHECK (version > 0),
            CHECK (state IN (
                'waiting_evidence', 'diagnosing', 'planning', 'awaiting_input',
                'awaiting_approval', 'ready', 'preparing', 'executing', 'evaluating',
                'succeeded', 'exhausted', 'blocked', 'failed', 'cancelled'
            ))
        )
        """,
        """
        CREATE INDEX idx_remediation_sessions_owner_updated
        ON remediation_sessions(owner, updated_at DESC, session_id DESC)
        """,
        """
        CREATE INDEX idx_remediation_sessions_state_lease
        ON remediation_sessions(state, lease_expires_at, updated_at)
        """,
        """
        CREATE INDEX idx_remediation_sessions_source_run
        ON remediation_sessions(source_run_id, created_at)
        """,
        """
        CREATE TABLE remediation_turns (
            turn_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES remediation_sessions(session_id)
                ON DELETE CASCADE,
            turn_index INTEGER NOT NULL,
            state TEXT NOT NULL,
            source_run_id TEXT NOT NULL REFERENCES runs(run_id),
            advice_id TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(session_id, turn_index),
            CHECK (turn_index >= 0)
        )
        """,
        """
        CREATE TABLE remediation_action_proposals (
            proposal_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES remediation_sessions(session_id)
                ON DELETE CASCADE,
            turn_id TEXT NOT NULL REFERENCES remediation_turns(turn_id) ON DELETE CASCADE,
            action_id TEXT NOT NULL,
            action_type TEXT NOT NULL,
            source TEXT NOT NULL,
            risk TEXT NOT NULL,
            approval_required INTEGER NOT NULL,
            policy_status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(turn_id, action_id),
            CHECK (approval_required IN (0, 1))
        )
        """,
        """
        CREATE INDEX idx_remediation_proposals_session_created
        ON remediation_action_proposals(session_id, created_at, proposal_id)
        """,
        """
        CREATE TABLE remediation_action_decisions (
            decision_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES remediation_sessions(session_id)
                ON DELETE CASCADE,
            proposal_id TEXT NOT NULL REFERENCES remediation_action_proposals(proposal_id)
                ON DELETE CASCADE,
            actor TEXT NOT NULL,
            decision TEXT NOT NULL,
            expected_session_version INTEGER NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL,
            CHECK (decision IN ('approve', 'reject', 'cancel')),
            CHECK (expected_session_version > 0)
        )
        """,
        """
        CREATE INDEX idx_remediation_decisions_session_created
        ON remediation_action_decisions(session_id, created_at, decision_id)
        """,
        """
        CREATE TABLE remediation_action_executions (
            execution_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES remediation_sessions(session_id)
                ON DELETE CASCADE,
            proposal_id TEXT NOT NULL REFERENCES remediation_action_proposals(proposal_id),
            state TEXT NOT NULL,
            derived_contract_id TEXT,
            derived_run_id TEXT REFERENCES runs(run_id),
            error_code TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(session_id, proposal_id)
        )
        """,
        """
        CREATE INDEX idx_remediation_executions_session_created
        ON remediation_action_executions(session_id, created_at, execution_id)
        """,
        """
        CREATE TABLE remediation_evaluations (
            evaluation_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES remediation_sessions(session_id)
                ON DELETE CASCADE,
            execution_id TEXT NOT NULL REFERENCES remediation_action_executions(execution_id),
            source_run_id TEXT NOT NULL REFERENCES runs(run_id),
            derived_run_id TEXT NOT NULL REFERENCES runs(run_id),
            outcome TEXT NOT NULL,
            checks_json TEXT NOT NULL,
            comparison_json TEXT NOT NULL,
            evidence_refs_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(session_id, execution_id),
            CHECK (outcome IN (
                'verified_success', 'execution_success_unverified', 'failed', 'inconclusive'
            ))
        )
        """,
    ),
)


REMEDIATION_MIGRATIONS = (REMEDIATION_SESSION_MIGRATION,)
