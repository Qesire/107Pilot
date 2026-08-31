"""PostgreSQL schema and migration runner for every persisted business domain.

SQLite remains the deliberately small, offline development backend.  Production
uses this schema for runs, contracts, platform facts, templates and remediation
sessions together with the control-plane tables in ``PostgresControlRepository``.
The domain migration IDs share the PostgreSQL ``schema_migrations`` history with
the control-plane migrations, but have their own non-overlapping ID range.
"""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import importlib
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from pilot107.core.postgres_control_repository import (
    PostgresConfigurationError,
    PostgresDriverUnavailable,
)


class PostgresDomainMigrationError(RuntimeError):
    """Raised when the PostgreSQL business-domain schema is not trustworthy."""


def _statements(sql: str) -> tuple[str, ...]:
    """Keep a large migration readable without weakening its checksum contract."""

    return tuple(
        statement.strip() for statement in sql.split("\n-- statement\n") if statement.strip()
    )


_DOMAIN_SCHEMA = _statements(
    """
    CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        contract_id TEXT,
        parent_run_id TEXT,
        lineage_reason TEXT,
        remediation_plan_id TEXT,
        attempt INTEGER NOT NULL DEFAULT 0,
        workflow_json TEXT NOT NULL DEFAULT '{}',
        retry_not_before TEXT,
        owner TEXT NOT NULL,
        state TEXT NOT NULL,
        collection_state TEXT NOT NULL,
        diagnosis_state TEXT NOT NULL,
        capsule_state TEXT NOT NULL,
        result_status TEXT NOT NULL,
        job_id TEXT,
        job_name TEXT,
        workdir TEXT NOT NULL,
        script TEXT NOT NULL,
        exit_code TEXT,
        terminal_state TEXT,
        submit_strategy TEXT,
        submit_response_json TEXT NOT NULL DEFAULT '{}',
        submission_owner TEXT,
        submission_fencing_token INTEGER NOT NULL DEFAULT 0,
        resource_plan_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_runs_owner_state ON runs(owner, state)"
    "\n-- statement\n"
    "CREATE INDEX idx_runs_job_id ON runs(job_id)"
    "\n-- statement\n"
    "CREATE INDEX idx_runs_owner_created ON runs(owner, created_at DESC, run_id DESC)"
    "\n-- statement\n"
    "CREATE INDEX idx_runs_parent ON runs(parent_run_id, created_at)"
    "\n-- statement\n"
    "CREATE INDEX idx_runs_remediation ON runs(remediation_plan_id, created_at)"
    "\n-- statement\n"
    "CREATE INDEX idx_runs_retry_due ON runs(lineage_reason, state, retry_not_before)"
    "\n-- statement\n"
    """
    CREATE TABLE run_events (
        event_id BIGSERIAL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        event_type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_run_events_run_id ON run_events(run_id)"
    "\n-- statement\n"
    "CREATE INDEX idx_run_events_run_type_id ON run_events(run_id, event_type, event_id)"
    "\n-- statement\n"
    """
    CREATE TABLE collection_tasks (
        task_id BIGSERIAL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        task_type TEXT NOT NULL,
        state TEXT NOT NULL,
        next_attempt_at TEXT,
        lease_owner TEXT,
        lease_expires_at TEXT,
        fencing_token INTEGER NOT NULL DEFAULT 0,
        generation INTEGER NOT NULL DEFAULT 1,
        attempts INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(run_id, task_type)
    )
    """
    "\n-- statement\n"
    """
    CREATE TABLE evidence_objects (
        object_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        category TEXT NOT NULL,
        logical_path TEXT NOT NULL,
        store_path TEXT NOT NULL,
        source_uri TEXT,
        sha256 TEXT,
        size_bytes INTEGER,
        mime_type TEXT,
        collection_status TEXT NOT NULL,
        collection_note TEXT,
        mutable_during_run INTEGER NOT NULL DEFAULT 0,
        finalized_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(run_id, logical_path)
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_evidence_objects_run_category ON evidence_objects(run_id, category)"
    "\n-- statement\n"
    "CREATE INDEX idx_evidence_objects_sha256 ON evidence_objects(sha256)"
    "\n-- statement\n"
    """
    CREATE TABLE diagnoses (
        diagnosis_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        rule_id TEXT NOT NULL,
        severity TEXT NOT NULL,
        summary TEXT NOT NULL,
        evidence_refs_json TEXT NOT NULL DEFAULT '[]',
        suggested_patch_json TEXT NOT NULL DEFAULT '{}',
        retryable INTEGER NOT NULL DEFAULT 0,
        confidence TEXT NOT NULL,
        category TEXT,
        stage TEXT,
        fix_guide_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        UNIQUE(run_id, rule_id)
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_diagnoses_run_id ON diagnoses(run_id, created_at)"
    "\n-- statement\n"
    """
    CREATE TABLE agent_advice (
        advice_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        owner TEXT NOT NULL,
        request_key TEXT NOT NULL,
        state TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        source_run_updated_at TEXT NOT NULL,
        evidence_bundle_sha256 TEXT NOT NULL,
        provider TEXT NOT NULL,
        model TEXT,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(run_id, request_key)
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_agent_advice_run ON agent_advice(run_id, created_at)"
    "\n-- statement\n"
    "CREATE INDEX idx_agent_advice_owner_created ON agent_advice(owner, created_at DESC, advice_id DESC)"
    "\n-- statement\n"
    "CREATE INDEX idx_agent_advice_owner_state_created ON agent_advice(owner, state, created_at DESC, advice_id DESC)"
    "\n-- statement\n"
    """
    CREATE TABLE agent_decisions (
        decision_id BIGSERIAL PRIMARY KEY,
        advice_id TEXT NOT NULL REFERENCES agent_advice(advice_id) ON DELETE CASCADE,
        decision TEXT NOT NULL,
        actor TEXT NOT NULL,
        action_ids_json TEXT NOT NULL DEFAULT '[]',
        note TEXT,
        advice_version INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_agent_decisions_advice ON agent_decisions(advice_id, decision_id)"
    "\n-- statement\n"
    """
    CREATE TABLE agent_action_executions (
        execution_id TEXT PRIMARY KEY,
        advice_id TEXT NOT NULL REFERENCES agent_advice(advice_id) ON DELETE CASCADE,
        action_id TEXT NOT NULL,
        owner TEXT NOT NULL,
        state TEXT NOT NULL,
        submit_requested INTEGER NOT NULL DEFAULT 0,
        derived_contract_id TEXT,
        run_id TEXT,
        error_code TEXT,
        error_message TEXT,
        execution_phase TEXT,
        execution_owner TEXT,
        execution_fencing_token INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(advice_id, action_id)
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_agent_action_executions_advice ON agent_action_executions(advice_id, created_at)"
    "\n-- statement\n"
    "CREATE INDEX idx_agent_action_executions_owner_created ON agent_action_executions(owner, created_at DESC, execution_id DESC)"
    "\n-- statement\n"
    "CREATE INDEX idx_agent_action_executions_owner_state_created ON agent_action_executions(owner, state, created_at DESC, execution_id DESC)"
    "\n-- statement\n"
    """
    CREATE TABLE contracts (
        contract_id TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        recipe_version_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        field_sources_json TEXT NOT NULL DEFAULT '[]',
        schema_version TEXT NOT NULL DEFAULT 'pilot107.contract/v1',
        digest TEXT NOT NULL DEFAULT '',
        parent_contract_id TEXT,
        derivation_reason TEXT,
        source_advice_id TEXT,
        source_action_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_contracts_owner ON contracts(owner, created_at)"
    "\n-- statement\n"
    "CREATE INDEX idx_contracts_owner_created ON contracts(owner, created_at DESC, contract_id DESC)"
    "\n-- statement\n"
    "CREATE INDEX idx_contracts_owner_recipe_created ON contracts(owner, recipe_version_id, created_at DESC, contract_id DESC)"
    "\n-- statement\n"
    "CREATE INDEX idx_contracts_parent ON contracts(parent_contract_id, created_at)"
    "\n-- statement\n"
    """
    CREATE TABLE recipe_versions (
        recipe_version_id TEXT PRIMARY KEY,
        recipe_id TEXT NOT NULL,
        version TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        trust_level TEXT NOT NULL,
        parameter_schema_json TEXT NOT NULL,
        compatibility_json TEXT NOT NULL,
        risk_declaration_json TEXT NOT NULL,
        sbatch_template TEXT,
        preflight_checks_json TEXT NOT NULL DEFAULT '[]',
        recovery_json TEXT,
        success_protocol_json TEXT,
        source TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        materializer TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(recipe_id, version)
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_recipe_versions_recipe ON recipe_versions(recipe_id, version)"
    "\n-- statement\n"
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
    """
    "\n-- statement\n"
    "CREATE INDEX idx_platform_snapshots_owner_captured ON platform_snapshots(owner, captured_at DESC, snapshot_id DESC)"
    "\n-- statement\n"
    "CREATE INDEX idx_platform_snapshots_owner_scope_captured ON platform_snapshots(owner, scope, captured_at DESC, snapshot_id DESC)"
    "\n-- statement\n"
    "CREATE INDEX idx_platform_snapshots_expiry ON platform_snapshots(expires_at)"
    "\n-- statement\n"
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
    """
    "\n-- statement\n"
    "CREATE INDEX idx_user_entitlements_owner_captured ON user_entitlement_snapshots(owner, captured_at DESC, snapshot_id DESC)"
    "\n-- statement\n"
    "CREATE INDEX idx_user_entitlements_expiry ON user_entitlement_snapshots(expires_at)"
    "\n-- statement\n"
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
        publication_json TEXT NOT NULL DEFAULT '{}',
        CHECK (visibility IN ('private', 'course', 'campus', 'public')),
        CHECK (state IN ('editable', 'submitted', 'approved', 'rejected', 'published', 'archived')),
        CHECK (version > 0)
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_template_drafts_owner_updated ON template_drafts(owner, updated_at DESC, draft_id DESC)"
    "\n-- statement\n"
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
        reviewer_role TEXT,
        reviewer_scope_key TEXT,
        gate_report_json TEXT NOT NULL DEFAULT '{}',
        validated_at TEXT,
        CHECK (state IN ('pending', 'approved', 'rejected', 'withdrawn')),
        CHECK (version > 0),
        CHECK (draft_version > 0)
    )
    """
    "\n-- statement\n"
    "CREATE UNIQUE INDEX idx_template_reviews_one_pending ON template_reviews(draft_id) WHERE state = 'pending'"
    "\n-- statement\n"
    "CREATE INDEX idx_template_reviews_state_created ON template_reviews(state, created_at, review_id)"
    "\n-- statement\n"
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
        publication_json TEXT NOT NULL DEFAULT '{}',
        gate_report_json TEXT NOT NULL DEFAULT '{}',
        request_key TEXT,
        UNIQUE(template_id, release_version),
        CHECK (visibility IN ('private', 'course', 'campus', 'public')),
        CHECK (source_draft_version > 0)
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_template_releases_market ON template_releases(visibility, published_at DESC, release_id DESC)"
    "\n-- statement\n"
    "CREATE UNIQUE INDEX idx_template_releases_publisher_request_key ON template_releases(publisher, request_key) WHERE request_key IS NOT NULL"
    "\n-- statement\n"
    """
    CREATE TABLE template_release_withdrawals (
        release_id TEXT PRIMARY KEY REFERENCES template_releases(release_id),
        actor TEXT NOT NULL,
        reason TEXT NOT NULL,
        withdrawn_at TEXT NOT NULL
    )
    """
    "\n-- statement\n"
    """
    CREATE TABLE template_adoptions (
        adoption_id TEXT PRIMARY KEY,
        release_id TEXT NOT NULL REFERENCES template_releases(release_id),
        adopter TEXT NOT NULL,
        request_key TEXT NOT NULL,
        target_template_id TEXT NOT NULL,
        target_draft_id TEXT NOT NULL REFERENCES template_drafts(draft_id),
        created_at TEXT NOT NULL,
        target_contract_id TEXT,
        UNIQUE(adopter, request_key)
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_template_adoptions_adopter_created ON template_adoptions(adopter, created_at DESC, adoption_id DESC)"
    "\n-- statement\n"
    "CREATE UNIQUE INDEX idx_template_adoptions_target_contract ON template_adoptions(target_contract_id) WHERE target_contract_id IS NOT NULL"
    "\n-- statement\n"
    """
    CREATE TABLE template_verifications (
        verification_id TEXT PRIMARY KEY,
        release_id TEXT NOT NULL REFERENCES template_releases(release_id),
        run_id TEXT,
        environment TEXT NOT NULL,
        status TEXT NOT NULL,
        evidence_ref TEXT,
        verified_at TEXT NOT NULL,
        verified_by TEXT,
        request_key TEXT,
        evidence_sha256 TEXT,
        detail_json TEXT NOT NULL DEFAULT '{}',
        CHECK (status IN ('passed', 'failed', 'expired'))
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_template_verifications_release_verified ON template_verifications(release_id, verified_at DESC, verification_id DESC)"
    "\n-- statement\n"
    "CREATE UNIQUE INDEX idx_template_verifications_actor_request ON template_verifications(verified_by, request_key) WHERE verified_by IS NOT NULL AND request_key IS NOT NULL"
    "\n-- statement\n"
    "CREATE UNIQUE INDEX idx_template_verifications_release_run_environment ON template_verifications(release_id, run_id, environment) WHERE run_id IS NOT NULL"
    "\n-- statement\n"
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
        provider TEXT NOT NULL DEFAULT 'none',
        UNIQUE(owner, request_key),
        CHECK (version > 0),
        CHECK (state IN ('waiting_evidence', 'diagnosing', 'planning', 'awaiting_input', 'awaiting_approval', 'ready', 'preparing', 'executing', 'evaluating', 'succeeded', 'exhausted', 'blocked', 'failed', 'cancelled'))
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_remediation_sessions_owner_updated ON remediation_sessions(owner, updated_at DESC, session_id DESC)"
    "\n-- statement\n"
    "CREATE INDEX idx_remediation_sessions_state_lease ON remediation_sessions(state, lease_expires_at, updated_at)"
    "\n-- statement\n"
    "CREATE INDEX idx_remediation_sessions_source_run ON remediation_sessions(source_run_id, created_at)"
    "\n-- statement\n"
    """
    CREATE TABLE remediation_turns (
        turn_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES remediation_sessions(session_id) ON DELETE CASCADE,
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
    """
    "\n-- statement\n"
    """
    CREATE TABLE remediation_action_proposals (
        proposal_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES remediation_sessions(session_id) ON DELETE CASCADE,
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
    """
    "\n-- statement\n"
    "CREATE INDEX idx_remediation_proposals_session_created ON remediation_action_proposals(session_id, created_at, proposal_id)"
    "\n-- statement\n"
    """
    CREATE TABLE remediation_action_decisions (
        decision_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES remediation_sessions(session_id) ON DELETE CASCADE,
        proposal_id TEXT NOT NULL REFERENCES remediation_action_proposals(proposal_id) ON DELETE CASCADE,
        actor TEXT NOT NULL,
        decision TEXT NOT NULL,
        expected_session_version INTEGER NOT NULL,
        note TEXT,
        created_at TEXT NOT NULL,
        CHECK (decision IN ('approve', 'reject', 'cancel')),
        CHECK (expected_session_version > 0)
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_remediation_decisions_session_created ON remediation_action_decisions(session_id, created_at, decision_id)"
    "\n-- statement\n"
    """
    CREATE TABLE remediation_action_executions (
        execution_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES remediation_sessions(session_id) ON DELETE CASCADE,
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
    """
    "\n-- statement\n"
    "CREATE INDEX idx_remediation_executions_session_created ON remediation_action_executions(session_id, created_at, execution_id)"
    "\n-- statement\n"
    """
    CREATE TABLE remediation_evaluations (
        evaluation_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES remediation_sessions(session_id) ON DELETE CASCADE,
        execution_id TEXT NOT NULL REFERENCES remediation_action_executions(execution_id),
        source_run_id TEXT NOT NULL REFERENCES runs(run_id),
        derived_run_id TEXT NOT NULL REFERENCES runs(run_id),
        outcome TEXT NOT NULL,
        checks_json TEXT NOT NULL,
        comparison_json TEXT NOT NULL,
        evidence_refs_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(session_id, execution_id),
        CHECK (outcome IN ('verified_success', 'execution_success_unverified', 'failed', 'inconclusive'))
    )
    """
    "\n-- statement\n"
    """
    CREATE TABLE remediation_session_events (
        event_id BIGSERIAL PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES remediation_sessions(session_id) ON DELETE CASCADE,
        event_type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_remediation_events_session_id ON remediation_session_events(session_id, event_id)"
    "\n-- statement\n"
    """
    CREATE OR REPLACE FUNCTION pilot107_template_release_immutable()
    RETURNS TRIGGER
    LANGUAGE plpgsql
    AS $$
    BEGIN
        RAISE EXCEPTION 'template releases are immutable';
    END;
    $$
    """
    "\n-- statement\n"
    """
    CREATE TRIGGER template_releases_immutable_update
    BEFORE UPDATE ON template_releases
    FOR EACH ROW EXECUTE FUNCTION pilot107_template_release_immutable()
    """
    "\n-- statement\n"
    """
    CREATE TRIGGER template_releases_immutable_delete
    BEFORE DELETE ON template_releases
    FOR EACH ROW EXECUTE FUNCTION pilot107_template_release_immutable()
    """
)

_RUN_PUBLICATION_SCHEMA = _statements(
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
    """
    "\n-- statement\n"
    "CREATE INDEX idx_run_publications_market ON run_publications(visibility, published_at DESC, publication_id DESC)"
    "\n-- statement\n"
    "CREATE INDEX idx_run_publications_owner ON run_publications(owner, published_at DESC, publication_id DESC)"
    "\n-- statement\n"
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
    """
    "\n-- statement\n"
    "CREATE INDEX idx_run_publication_adoptions_adopter ON run_publication_adoptions(adopter, created_at DESC, adoption_id DESC)"
    "\n-- statement\n"
    "CREATE UNIQUE INDEX idx_run_publication_adoptions_target_contract ON run_publication_adoptions(target_contract_id) WHERE target_contract_id IS NOT NULL"
)

_UPLOAD_SESSION_SCHEMA = _statements(
    """
    CREATE TABLE upload_sessions (
        upload_id TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        target_path TEXT NOT NULL,
        filename TEXT NOT NULL,
        total_size BIGINT NOT NULL,
        chunk_size INTEGER NOT NULL,
        total_chunks INTEGER NOT NULL,
        sha256_expected TEXT,
        auto_extract SMALLINT NOT NULL DEFAULT 0,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        received_chunks_json TEXT NOT NULL DEFAULT '{}',
        sha256_actual TEXT,
        written_path TEXT,
        extracted_members INTEGER,
        error TEXT
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_upload_sessions_owner ON upload_sessions(owner, created_at DESC)"
)

# tus resumable uploads replace the chunk-indexed staging model with a single
# contiguous offset.  Upload sessions are transient, so the upgrade discards
# any in-flight rows rather than translating the per-chunk map.
_UPLOAD_SESSION_TUS_SCHEMA = _statements(
    """
    ALTER TABLE upload_sessions DROP COLUMN chunk_size
    """
    "\n-- statement\n"
    "ALTER TABLE upload_sessions DROP COLUMN total_chunks"
    "\n-- statement\n"
    "ALTER TABLE upload_sessions DROP COLUMN received_chunks_json"
    "\n-- statement\n"
    "ALTER TABLE upload_sessions ADD COLUMN received_bytes BIGINT NOT NULL DEFAULT 0"
    "\n-- statement\n"
    "ALTER TABLE upload_sessions ADD COLUMN is_partial SMALLINT NOT NULL DEFAULT 0"
    "\n-- statement\n"
    "DELETE FROM upload_sessions"
)

_AGENT_SESSION_SCHEMA = _statements(
    """
    CREATE TABLE agent_sessions (
        session_id TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        request_key TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        model_profile_id TEXT NOT NULL,
        source_json JSONB NOT NULL,
        state TEXT NOT NULL,
        state_version INTEGER NOT NULL,
        context_checkpoint_json JSONB,
        resource_usage_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        outcome_json JSONB,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        UNIQUE (owner, request_key),
        CHECK (state IN ('idle', 'queued', 'running')),
        CHECK (state_version > 0)
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_agent_sessions_owner_updated ON agent_sessions(owner, updated_at DESC, session_id DESC)"
    "\n-- statement\n"
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
        cancel_requested SMALLINT NOT NULL DEFAULT 0,
        lease_owner TEXT,
        lease_expires_at TIMESTAMPTZ,
        fencing_token INTEGER NOT NULL DEFAULT 0,
        event_sequence INTEGER NOT NULL DEFAULT 0,
        final_checkpoint_json JSONB,
        error_json JSONB,
        created_at TIMESTAMPTZ NOT NULL,
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        UNIQUE (session_id, request_key),
        CHECK (state IN (
            'queued', 'running', 'interrupted', 'completed', 'cancelled', 'failed'
        )),
        CHECK (state_version > 0),
        CHECK (cancel_requested IN (0, 1)),
        CHECK (fencing_token >= 0),
        CHECK (event_sequence >= 0)
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_agent_turns_recoverable ON agent_turns(state, lease_expires_at, created_at, turn_id)"
    "\n-- statement\n"
    "CREATE INDEX idx_agent_turns_owner_session ON agent_turns(owner, session_id, created_at, turn_id)"
    "\n-- statement\n"
    "CREATE UNIQUE INDEX uq_agent_turns_running_owner ON agent_turns(owner) WHERE state = 'running'"
    "\n-- statement\n"
    """
    CREATE TABLE agent_turn_events (
        event_id BIGSERIAL PRIMARY KEY,
        turn_id TEXT NOT NULL REFERENCES agent_turns(turn_id),
        session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
        owner TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        payload_json JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        UNIQUE (turn_id, sequence),
        CHECK (sequence > 0)
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_agent_turn_events_owner_session ON agent_turn_events(owner, session_id, event_id)"
    "\n-- statement\n"
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
        result_json JSONB,
        error_json JSONB,
        bytes_returned BIGINT NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        UNIQUE (turn_id, idempotency_key),
        CHECK (state IN ('running', 'completed', 'failed')),
        CHECK (bytes_returned >= 0)
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_agent_tool_invocations_owner_turn ON agent_tool_invocations(owner, turn_id, created_at, invocation_id)"
)

_AGENT_EXPERIMENT_PROJECT_SCHEMA = _statements(
    """
    CREATE TABLE agent_experiment_projects (
        project_id TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        request_key TEXT NOT NULL,
        origin TEXT NOT NULL,
        state TEXT NOT NULL,
        version INTEGER NOT NULL,
        goal TEXT NOT NULL,
        source_json JSONB,
        blueprint_json JSONB,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        UNIQUE (owner, request_key),
        CHECK (origin IN ('blank', 'template', 'existing', 'failed_run')),
        CHECK (state IN (
            'drafting', 'editing', 'validating', 'awaiting_approval',
            'publishing', 'ready', 'blocked', 'cancelled'
        )),
        CHECK (version > 0)
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_agent_experiment_projects_owner_updated "
    "ON agent_experiment_projects(owner, updated_at DESC, project_id DESC)"
)

_AGENT_WORKSPACE_SCHEMA = _statements(
    """
    CREATE TABLE agent_workspaces (
        workspace_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES agent_experiment_projects(project_id),
        owner TEXT NOT NULL,
        snapshot_digest TEXT NOT NULL,
        payload_json JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        UNIQUE (project_id, snapshot_digest)
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_agent_workspaces_owner_updated "
    "ON agent_workspaces(owner, updated_at DESC, workspace_id DESC)"
)

_AGENT_WORKSPACE_CHANGESET_SCHEMA = _statements(
    """
    CREATE TABLE agent_workspace_changesets (
        change_set_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES agent_experiment_projects(project_id),
        workspace_id TEXT NOT NULL REFERENCES agent_workspaces(workspace_id),
        owner TEXT NOT NULL,
        digest TEXT NOT NULL,
        state TEXT NOT NULL,
        version INTEGER NOT NULL,
        payload_json JSONB NOT NULL,
        diff_text TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        CHECK (version > 0)
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_agent_workspace_changesets_owner_updated "
    "ON agent_workspace_changesets(owner, updated_at DESC, change_set_id DESC)"
)

_AGENT_WORKSPACE_PUBLICATION_SCHEMA = _statements(
    """
    CREATE TABLE agent_workspace_publications (
        publication_id TEXT PRIMARY KEY,
        change_set_id TEXT NOT NULL UNIQUE REFERENCES agent_workspace_changesets(change_set_id),
        project_id TEXT NOT NULL REFERENCES agent_experiment_projects(project_id),
        workspace_id TEXT NOT NULL REFERENCES agent_workspaces(workspace_id),
        owner TEXT NOT NULL,
        target_root TEXT NOT NULL,
        approved_digest TEXT NOT NULL,
        state TEXT NOT NULL,
        version INTEGER NOT NULL,
        payload_json JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        CHECK (version > 0)
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_agent_workspace_publications_owner_updated "
    "ON agent_workspace_publications(owner, updated_at DESC, publication_id DESC)"
)

_AGENT_BUILDER_SUBMISSION_SCHEMA = _statements(
    """
    CREATE TABLE agent_builder_submissions (
        submission_id TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        session_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        project_id TEXT NOT NULL REFERENCES agent_experiment_projects(project_id),
        workspace_id TEXT NOT NULL REFERENCES agent_workspaces(workspace_id),
        request_key TEXT NOT NULL,
        input_digest TEXT NOT NULL,
        phase TEXT NOT NULL,
        state TEXT NOT NULL,
        version INTEGER NOT NULL,
        base_change_set_id TEXT,
        change_set_id TEXT,
        sandbox_result_id TEXT,
        task_id TEXT,
        receipt_json JSONB,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        UNIQUE (owner, request_key),
        CHECK (phase IN ('drafting', 'sandbox_failed', 'validation_scheduled')),
        CHECK (state IN ('running', 'sandbox_failed', 'scheduled')),
        CHECK ((state = 'running' AND phase = 'drafting') OR
               (state = 'sandbox_failed' AND phase = 'sandbox_failed') OR
               (state = 'scheduled' AND phase = 'validation_scheduled')),
        CHECK (version > 0)
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_agent_builder_submissions_owner_updated "
    "ON agent_builder_submissions(owner, updated_at DESC, submission_id DESC)"
)

_AGENT_TASK_SCHEMA = _statements(
    """
    CREATE TABLE agent_tasks (
        task_id TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        session_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        task_kind TEXT NOT NULL,
        state TEXT NOT NULL,
        version BIGINT NOT NULL,
        request_key TEXT NOT NULL,
        request_json JSONB NOT NULL,
        resource_envelope_json JSONB NOT NULL,
        envelope_expires_at TIMESTAMPTZ NOT NULL,
        linked_run_id TEXT,
        result_json JSONB,
        cancel_requested SMALLINT NOT NULL,
        lease_owner TEXT,
        lease_expires_at TIMESTAMPTZ,
        fencing_token BIGINT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        UNIQUE (owner, request_key),
        CHECK (task_kind = 'slurm_validation'),
        CHECK (state IN (
            'pending', 'running', 'succeeded', 'failed', 'cancelled',
            'auth_required'
        )),
        CHECK (version >= 0),
        CHECK (cancel_requested IN (0, 1)),
        CHECK (fencing_token >= 0),
        CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_agent_tasks_recoverable "
    "ON agent_tasks(state, lease_expires_at, created_at, task_id)"
    "\n-- statement\n"
    "CREATE INDEX idx_agent_tasks_owner_session "
    "ON agent_tasks(owner, session_id, created_at, task_id)"
    "\n-- statement\n"
    "CREATE UNIQUE INDEX uq_agent_tasks_linked_run "
    "ON agent_tasks(linked_run_id) WHERE linked_run_id IS NOT NULL"
)

_AGENT_TASK_EVIDENCE_GATE_SCHEMA = _statements(
    "ALTER TABLE agent_tasks ADD COLUMN completion_policy TEXT NOT NULL DEFAULT 'evidence_required'"
    "\n-- statement\n"
    "ALTER TABLE agent_tasks ADD COLUMN gate_state TEXT NOT NULL DEFAULT 'created'"
    "\n-- statement\n"
    "ALTER TABLE agent_tasks ADD COLUMN schedule_receipt_ref TEXT"
    "\n-- statement\n"
    "ALTER TABLE agent_tasks ADD COLUMN schedule_receipt TEXT"
    "\n-- statement\n"
    "ALTER TABLE agent_tasks ADD COLUMN evidence_refs_json TEXT NOT NULL DEFAULT '[]'"
    "\n-- statement\n"
    "ALTER TABLE agent_tasks ADD COLUMN evidence_digest TEXT"
    "\n-- statement\n"
    "ALTER TABLE agent_tasks ADD COLUMN integrity_checked_at TIMESTAMPTZ"
    "\n-- statement\n"
    "ALTER TABLE agent_tasks ADD COLUMN capsule_ref TEXT"
    "\n-- statement\n"
    "ALTER TABLE agent_tasks ADD COLUMN capsule_state TEXT NOT NULL DEFAULT 'not_required'"
    "\n-- statement\n"
    "ALTER TABLE agent_tasks ADD COLUMN gate_receipt TEXT"
    "\n-- statement\n"
    "ALTER TABLE agent_tasks ADD COLUMN causation_root_key TEXT"
    "\n-- statement\n"
    "ALTER TABLE agent_tasks ADD COLUMN durable_operation_key TEXT"
    "\n-- statement\n"
    "ALTER TABLE agent_tasks ADD COLUMN reconciliation_attempt INTEGER NOT NULL DEFAULT 0"
    "\n-- statement\n"
    "ALTER TABLE agent_tasks ADD COLUMN heartbeat_at TIMESTAMPTZ"
    "\n-- statement\n"
    "ALTER TABLE agent_tasks ADD COLUMN legacy_gate_unverified SMALLINT NOT NULL DEFAULT 1"
    "\n-- statement\n"
    "CREATE INDEX idx_agent_tasks_gate_reconciliation ON agent_tasks(gate_state, reconciliation_attempt, updated_at)"
)

_AGENT_TASK_STAGE_IDENTITY_SCHEMA = _statements(
    "ALTER TABLE agent_tasks ADD COLUMN schedule_operation_key TEXT"
    "\n-- statement\n"
    "ALTER TABLE agent_tasks ADD COLUMN gate_operation_key TEXT"
)

_AGENT_TASK_READY_RECOVERY_SCHEMA = _statements(
    "ALTER TABLE agent_tasks ADD COLUMN ready_outbox_pending SMALLINT NOT NULL DEFAULT 0"
    "\n-- statement\n"
    "CREATE INDEX idx_agent_tasks_ready_recovery "
    "ON agent_tasks(ready_outbox_pending, updated_at, task_id)"
)

_EVIDENCE_OBJECT_GATE_SCHEMA = _statements(
    "ALTER TABLE evidence_objects ADD COLUMN IF NOT EXISTS workspace_revision INTEGER"
    "\n-- statement\n"
    "ALTER TABLE evidence_objects ADD COLUMN IF NOT EXISTS workspace_digest TEXT"
    "\n-- statement\n"
    "ALTER TABLE evidence_objects ADD COLUMN IF NOT EXISTS source_revision TEXT"
    "\n-- statement\n"
    "ALTER TABLE evidence_objects ADD COLUMN IF NOT EXISTS platform_snapshot_ref TEXT"
    "\n-- statement\n"
    "ALTER TABLE evidence_objects ADD COLUMN IF NOT EXISTS integrity_checked_at TIMESTAMPTZ"
    "\n-- statement\n"
    "ALTER TABLE evidence_objects ADD COLUMN IF NOT EXISTS integrity_object_set_digest TEXT"
)

_RUN_PROVENANCE_SCHEMA = _statements(
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS workspace_revision INTEGER"
    "\n-- statement\n"
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS workspace_digest TEXT"
    "\n-- statement\n"
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS source_revision TEXT"
    "\n-- statement\n"
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS platform_snapshot_ref TEXT"
)

_EVIDENCE_OBJECT_IMMUTABLE_GUARD_SCHEMA = _statements(
    "ALTER TABLE evidence_objects ADD COLUMN IF NOT EXISTS integrity_invalidated_at TIMESTAMPTZ"
    "\n-- statement\n"
    """
    CREATE OR REPLACE FUNCTION pilot107_evidence_object_integrity_guard()
    RETURNS TRIGGER
    LANGUAGE plpgsql
    AS $$
    DECLARE
        protected_changed BOOLEAN;
        state_changed BOOLEAN;
        explicit_invalidation BOOLEAN;
        freeze_completion BOOLEAN;
    BEGIN
        IF OLD.integrity_checked_at IS NULL
           AND OLD.integrity_invalidated_at IS NULL THEN
            RETURN NEW;
        END IF;
        protected_changed :=
            NEW.object_id IS DISTINCT FROM OLD.object_id OR
            NEW.run_id IS DISTINCT FROM OLD.run_id OR
            NEW.category IS DISTINCT FROM OLD.category OR
            NEW.logical_path IS DISTINCT FROM OLD.logical_path OR
            NEW.store_path IS DISTINCT FROM OLD.store_path OR
            NEW.source_uri IS DISTINCT FROM OLD.source_uri OR
            NEW.sha256 IS DISTINCT FROM OLD.sha256 OR
            NEW.size_bytes IS DISTINCT FROM OLD.size_bytes OR
            NEW.mime_type IS DISTINCT FROM OLD.mime_type OR
            NEW.collection_status IS DISTINCT FROM OLD.collection_status OR
            NEW.collection_note IS DISTINCT FROM OLD.collection_note OR
            NEW.mutable_during_run IS DISTINCT FROM OLD.mutable_during_run OR
            NEW.finalized_at IS DISTINCT FROM OLD.finalized_at OR
            NEW.workspace_revision IS DISTINCT FROM OLD.workspace_revision OR
            NEW.workspace_digest IS DISTINCT FROM OLD.workspace_digest OR
            NEW.source_revision IS DISTINCT FROM OLD.source_revision OR
            NEW.platform_snapshot_ref IS DISTINCT FROM OLD.platform_snapshot_ref;
        state_changed :=
            NEW.integrity_checked_at IS DISTINCT FROM OLD.integrity_checked_at OR
            NEW.integrity_object_set_digest IS DISTINCT FROM OLD.integrity_object_set_digest OR
            NEW.integrity_invalidated_at IS DISTINCT FROM OLD.integrity_invalidated_at;
        explicit_invalidation :=
            OLD.integrity_checked_at IS NOT NULL AND
            OLD.integrity_invalidated_at IS NULL AND
            NEW.integrity_checked_at IS NULL AND
            NEW.integrity_object_set_digest IS NULL AND
            NEW.integrity_invalidated_at IS NOT NULL AND
            NOT protected_changed;
        freeze_completion :=
            OLD.integrity_checked_at IS NOT NULL AND
            OLD.integrity_object_set_digest IS NULL AND
            OLD.integrity_invalidated_at IS NULL AND
            NEW.integrity_checked_at IS NOT DISTINCT FROM OLD.integrity_checked_at AND
            NEW.integrity_object_set_digest IS NOT NULL AND
            NEW.integrity_invalidated_at IS NULL AND
            NOT protected_changed;
        IF (protected_changed OR state_changed)
           AND NOT explicit_invalidation
           AND NOT freeze_completion THEN
            RAISE EXCEPTION 'integrity-frozen evidence object is immutable';
        END IF;
        RETURN NEW;
    END;
    $$
    """
    "\n-- statement\n"
    "DROP TRIGGER IF EXISTS evidence_objects_integrity_guard ON evidence_objects"
    "\n-- statement\n"
    """
    CREATE TRIGGER evidence_objects_integrity_guard
    BEFORE UPDATE ON evidence_objects
    FOR EACH ROW EXECUTE FUNCTION pilot107_evidence_object_integrity_guard()
    """
)

_WORKFLOW_MANIFEST_SCHEMA = _statements(
    """
    CREATE TABLE workflow_manifests (
        workflow_id TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        version BIGINT NOT NULL,
        manifest_json TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        CHECK (version > 0)
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_workflow_manifests_owner "
    "ON workflow_manifests(owner, updated_at DESC, workflow_id DESC)"
)

_RUNTIME_WATCH_SCHEMA = _statements(
    """
    CREATE TABLE runtime_watches (
        watch_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        owner TEXT NOT NULL,
        connection_id TEXT NOT NULL,
        state TEXT NOT NULL,
        version BIGINT NOT NULL,
        next_poll_at TIMESTAMPTZ,
        lease_owner TEXT,
        lease_expires_at TIMESTAMPTZ,
        fencing_token BIGINT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        stopped_at TIMESTAMPTZ,
        last_error_code TEXT,
        last_error_at TIMESTAMPTZ,
        UNIQUE (owner, run_id),
        CHECK (state IN (
            'watching', 'waiting_for_log', 'active', 'quiet_backoff',
            'degraded', 'finalizing', 'stopped'
        )),
        CHECK (version >= 0),
        CHECK (fencing_token >= 0),
        CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_runtime_watches_due "
    "ON runtime_watches(state, next_poll_at, lease_expires_at, watch_id)"
)

_RUNTIME_LOG_CURSOR_SCHEMA = _statements(
    """
    CREATE TABLE runtime_log_cursors (
        watch_id TEXT NOT NULL REFERENCES runtime_watches(watch_id) ON DELETE CASCADE,
        run_id TEXT NOT NULL,
        owner TEXT NOT NULL,
        stream TEXT NOT NULL,
        generation BIGINT NOT NULL,
        offset_value BIGINT NOT NULL,
        source_size BIGINT NOT NULL,
        source_mtime DOUBLE PRECISION,
        source_file_identity TEXT,
        source_prefix_fingerprint TEXT,
        decoder_remainder_base64 TEXT NOT NULL,
        last_data_at TIMESTAMPTZ,
        last_checked_at TIMESTAMPTZ,
        quiet_polls INTEGER NOT NULL,
        version BIGINT NOT NULL,
        PRIMARY KEY (watch_id, stream),
        UNIQUE (owner, run_id, stream),
        CHECK (stream IN ('stdout', 'stderr')),
        CHECK (generation >= 0 AND offset_value >= 0 AND source_size >= 0),
        CHECK (quiet_polls >= 0 AND version >= 0)
    )
    """
)

_RUNTIME_WATCH_HANDOFF_SCHEMA = _statements(
    "ALTER TABLE runtime_watches ADD COLUMN terminal_handoff_at TIMESTAMPTZ"
    "\n-- statement\n"
    "CREATE INDEX idx_runtime_watches_terminal_handoff "
    "ON runtime_watches(state, terminal_handoff_at, stopped_at)",
)

_RESOURCE_OBSERVATION_SCHEMA = _statements(
    """
    CREATE TABLE resource_observations (
        observation_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        resolution TEXT NOT NULL,
        connection_id TEXT NOT NULL,
        owner TEXT,
        run_id TEXT,
        attempt INTEGER,
        captured_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ,
        fencing_token BIGINT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        CHECK (resolution IN ('raw', 'minute', 'terminal')),
        CHECK (fencing_token >= 0)
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_resource_observations_run "
    "ON resource_observations(owner, run_id, resolution, captured_at)"
    "\n-- statement\n"
    "CREATE INDEX idx_resource_observations_expiry "
    "ON resource_observations(expires_at)"
    "\n-- statement\n"
    "CREATE UNIQUE INDEX idx_resource_terminal_summary_unique "
    "ON resource_observations(owner, run_id, attempt, kind) "
    "WHERE kind = 'run_resource_summary'"
)

_OBSERVATION_COLLECTION_SCHEMA = _statements(
    """
    CREATE TABLE observation_cycles (
        cycle_id TEXT PRIMARY KEY,
        connection_id TEXT NOT NULL,
        lane TEXT NOT NULL,
        fencing_token BIGINT NOT NULL,
        scheduled_at TIMESTAMPTZ NOT NULL,
        started_at TIMESTAMPTZ NOT NULL,
        completed_at TIMESTAMPTZ NOT NULL,
        command_count INTEGER NOT NULL,
        status TEXT NOT NULL,
        warnings_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        CHECK (fencing_token > 0),
        CHECK (command_count >= 0),
        CHECK (status IN ('complete', 'partial', 'failed', 'skipped_budget'))
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_observation_cycles_due "
    "ON observation_cycles(connection_id, lane, completed_at)"
    "\n-- statement\n"
    """
    CREATE TABLE observation_run_targets (
        connection_id TEXT NOT NULL,
        owner TEXT NOT NULL,
        run_id TEXT NOT NULL,
        job_id TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        run_state TEXT NOT NULL,
        finalized INTEGER NOT NULL DEFAULT 0,
        last_observed_at TIMESTAMPTZ,
        terminal_digest TEXT,
        terminal_stable_observations INTEGER NOT NULL DEFAULT 0,
        updated_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (owner, run_id),
        CHECK (attempt >= 0),
        CHECK (finalized IN (0, 1)),
        CHECK (terminal_stable_observations >= 0)
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_observation_targets_connection "
    "ON observation_run_targets(connection_id, finalized, last_observed_at, updated_at)"
)

_RUNTIME_LOG_SEGMENT_SCHEMA = _statements(
    """
    CREATE TABLE runtime_log_segments (
        segment_id TEXT PRIMARY KEY,
        watch_id TEXT NOT NULL REFERENCES runtime_watches(watch_id) ON DELETE CASCADE,
        run_id TEXT NOT NULL,
        owner TEXT NOT NULL,
        stream TEXT NOT NULL,
        generation BIGINT NOT NULL,
        start_offset BIGINT NOT NULL,
        end_offset BIGINT NOT NULL,
        content_sha256 TEXT NOT NULL,
        content_size BIGINT NOT NULL,
        content_ref TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        UNIQUE (owner, run_id, stream, generation, start_offset),
        CHECK (stream IN ('stdout', 'stderr')),
        CHECK (generation >= 0 AND start_offset >= 0),
        CHECK (end_offset > start_offset),
        CHECK (content_size = end_offset - start_offset)
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_runtime_log_segments_run_stream "
    "ON runtime_log_segments(owner, run_id, stream, generation, start_offset)"
)

_RUNTIME_ALERT_SCHEMA = _statements(
    """
    CREATE TABLE runtime_alerts (
        alert_id TEXT PRIMARY KEY,
        watch_id TEXT NOT NULL REFERENCES runtime_watches(watch_id) ON DELETE CASCADE,
        run_id TEXT NOT NULL,
        owner TEXT NOT NULL,
        code TEXT NOT NULL,
        severity TEXT NOT NULL,
        summary TEXT NOT NULL,
        segment_id TEXT REFERENCES runtime_log_segments(segment_id),
        generation BIGINT NOT NULL,
        offset_value BIGINT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        CHECK (severity IN ('info', 'warning', 'critical')),
        CHECK (generation >= 0 AND offset_value >= 0)
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_runtime_alerts_run_created "
    "ON runtime_alerts(owner, run_id, created_at, alert_id)"
)

_RUN_PUBLICATION_SHARE_MANIFEST_SCHEMA = _statements(
    "ALTER TABLE run_publications ADD COLUMN share_manifest_json TEXT NOT NULL DEFAULT '{}'"
    "\n-- statement\n"
    "ALTER TABLE run_publications ADD COLUMN share_manifest_digest TEXT NOT NULL DEFAULT ''"
    "\n-- statement\n"
    "ALTER TABLE run_publications ADD COLUMN shared_payload_json TEXT NOT NULL DEFAULT '{}'"
)

_MARKET_SESSION_SCHEMA = _statements(
    """
    CREATE TABLE template_publication_sessions (
        session_id TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        request_key TEXT NOT NULL,
        source_run_id TEXT NOT NULL,
        source_contract_id TEXT NOT NULL,
        source_digest TEXT NOT NULL,
        bundle_digest TEXT NOT NULL,
        draft_id TEXT,
        state TEXT NOT NULL,
        version INTEGER NOT NULL,
        reproduction_evidence_ref TEXT,
        reproduction_evidence_digest TEXT,
        reproduction_environment TEXT,
        confirmation_digest TEXT,
        review_id TEXT,
        release_id TEXT,
        release_version TEXT,
        verification_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(owner, source_run_id),
        UNIQUE(owner, request_key)
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_template_publication_sessions_owner_created "
    "ON template_publication_sessions(owner, created_at, session_id)"
    "\n-- statement\n"
    """
    CREATE TABLE market_application_sessions (
        session_id TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        request_key TEXT NOT NULL,
        source_kind TEXT NOT NULL,
        source_item_id TEXT NOT NULL,
        source_digest TEXT NOT NULL,
        assurance TEXT NOT NULL,
        user_intent TEXT NOT NULL,
        state TEXT NOT NULL,
        version INTEGER NOT NULL,
        project_id TEXT,
        workspace_id TEXT,
        change_set_id TEXT,
        target_contract_id TEXT,
        adoption_id TEXT,
        detail_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(owner, request_key),
        CHECK (source_kind IN ('curated_template', 'run_publication')),
        CHECK (assurance IN ('curated', 'reference_only'))
    )
    """
    "\n-- statement\n"
    "CREATE INDEX idx_market_application_sessions_owner_created "
    "ON market_application_sessions(owner, created_at, session_id)"
)

_REPAIR_TICKET_SCHEMA = _statements(
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
    """
    "\n-- statement\n"
    "CREATE INDEX idx_artifact_manifests_owner "
    "ON artifact_manifests(owner, created_at DESC)"
    "\n-- statement\n"
    "CREATE INDEX idx_artifact_manifests_run "
    "ON artifact_manifests(run_id, created_at DESC)"
    "\n-- statement\n"
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
    """
    "\n-- statement\n"
    "CREATE INDEX idx_repair_tickets_owner_state "
    "ON repair_tickets(owner, state, updated_at DESC, ticket_id DESC)"
    "\n-- statement\n"
    "CREATE INDEX idx_repair_tickets_source_run "
    "ON repair_tickets(source_run_id, created_at)"
    "\n-- statement\n"
    "CREATE INDEX idx_repair_tickets_session "
    "ON repair_tickets(session_id, created_at)"
)

_SSH_CONNECTION_SCHEMA = _statements(
    """
    CREATE TABLE ssh_connection_sessions (
        connection_id TEXT PRIMARY KEY,
        portal_owner TEXT NOT NULL,
        slurm_user TEXT NOT NULL,
        target_id TEXT NOT NULL,
        state TEXT NOT NULL,
        status_code TEXT NOT NULL,
        message TEXT NOT NULL,
        authenticated_at TEXT,
        expires_at TEXT,
        checked_at TEXT,
        revision INTEGER NOT NULL CHECK(revision > 0),
        CHECK(state IN (
            'active', 'auth_required', 'revoked', 'expired', 'unavailable'
        ))
    )
    """
)

_MIGRATIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("004a.001.postgres_domain_schema", _DOMAIN_SCHEMA),
    ("004a.002.run_publications", _RUN_PUBLICATION_SCHEMA),
    ("004a.003.upload_sessions", _UPLOAD_SESSION_SCHEMA),
    ("004a.004.upload_sessions_tus", _UPLOAD_SESSION_TUS_SCHEMA),
    ("004a.005.agent_sessions", _AGENT_SESSION_SCHEMA),
    ("004a.006.agent_experiment_projects", _AGENT_EXPERIMENT_PROJECT_SCHEMA),
    ("004a.007.agent_workspaces", _AGENT_WORKSPACE_SCHEMA),
    ("004a.008.agent_workspace_changesets", _AGENT_WORKSPACE_CHANGESET_SCHEMA),
    ("004a.009.runtime_watches", _RUNTIME_WATCH_SCHEMA),
    ("004a.010.runtime_log_cursors", _RUNTIME_LOG_CURSOR_SCHEMA),
    ("004a.011.runtime_log_segments", _RUNTIME_LOG_SEGMENT_SCHEMA),
    ("004a.012.runtime_alerts", _RUNTIME_ALERT_SCHEMA),
    ("004a.013.runtime_watch_terminal_handoff", _RUNTIME_WATCH_HANDOFF_SCHEMA),
    ("004a.014.resource_observations", _RESOURCE_OBSERVATION_SCHEMA),
    ("004a.015.observation_collection", _OBSERVATION_COLLECTION_SCHEMA),
    ("004a.016.agent_tasks", _AGENT_TASK_SCHEMA),
    ("004a.017.workflow_manifests", _WORKFLOW_MANIFEST_SCHEMA),
    ("004a.018.agent_workspace_publications", _AGENT_WORKSPACE_PUBLICATION_SCHEMA),
    ("004a.019.run_publication_share_manifest", _RUN_PUBLICATION_SHARE_MANIFEST_SCHEMA),
    ("004a.020.market_sessions", _MARKET_SESSION_SCHEMA),
    ("004a.021.repair_tickets", _REPAIR_TICKET_SCHEMA),
    ("004a.022.ssh_connection_sessions", _SSH_CONNECTION_SCHEMA),
    ("004a.023.agent_builder_submissions", _AGENT_BUILDER_SUBMISSION_SCHEMA),
    ("004a.024.agent_task_evidence_gates", _AGENT_TASK_EVIDENCE_GATE_SCHEMA),
    ("004a.025.agent_task_stage_identities", _AGENT_TASK_STAGE_IDENTITY_SCHEMA),
    ("004a.026.evidence_object_integrity_gates", _EVIDENCE_OBJECT_GATE_SCHEMA),
    ("004a.027.run_provenance", _RUN_PROVENANCE_SCHEMA),
    ("004a.028.evidence_object_immutable_guard", _EVIDENCE_OBJECT_IMMUTABLE_GUARD_SCHEMA),
    ("004a.029.agent_task_ready_recovery", _AGENT_TASK_READY_RECOVERY_SCHEMA),
)


def initialize_postgres_domain_schema(dsn: str) -> None:
    """Create or verify all PostgreSQL business-domain tables atomically."""

    psycopg, dict_row = _load_psycopg()
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.transaction():
        row = conn.execute("SHOW server_encoding").fetchone()
        if row is None or _text(row["server_encoding"]).upper() != "UTF8":
            raise PostgresConfigurationError("PostgreSQL server_encoding must be UTF8")
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("pilot107:migrations",))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                migration_id TEXT PRIMARY KEY,
                checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        for migration_id, statements in _MIGRATIONS:
            checksum = _migration_checksum(statements)
            existing = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE migration_id = %s",
                (migration_id,),
            ).fetchone()
            if existing is not None:
                if _text(existing["checksum"]) != checksum:
                    raise PostgresDomainMigrationError(
                        f"migration checksum changed: {migration_id}"
                    )
                continue
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (migration_id, checksum, applied_at) VALUES (%s, %s, %s)",
                (migration_id, checksum, datetime.now(UTC)),
            )


def domain_table_names() -> tuple[str, ...]:
    """Business-domain tables, in the foreign-key order used for transfer."""

    return (
        "runs",
        "run_events",
        "workflow_manifests",
        "collection_tasks",
        "evidence_objects",
        "diagnoses",
        "agent_advice",
        "agent_decisions",
        "agent_action_executions",
        "contracts",
        "recipe_versions",
        "platform_snapshots",
        "user_entitlement_snapshots",
        "template_drafts",
        "template_reviews",
        "template_releases",
        "template_release_withdrawals",
        "template_adoptions",
        "template_verifications",
        "run_publications",
        "run_publication_adoptions",
        "template_publication_sessions",
        "market_application_sessions",
        "remediation_sessions",
        "remediation_turns",
        "remediation_action_proposals",
        "remediation_action_decisions",
        "remediation_action_executions",
        "remediation_evaluations",
        "remediation_session_events",
        "upload_sessions",
        "agent_sessions",
        "agent_turns",
        "agent_turn_events",
        "agent_tool_invocations",
        "agent_experiment_projects",
        "agent_workspaces",
        "agent_workspace_changesets",
        "agent_workspace_publications",
        "agent_builder_submissions",
        "agent_tasks",
        "runtime_watches",
        "runtime_log_cursors",
        "runtime_log_segments",
        "runtime_alerts",
        "resource_observations",
        "observation_cycles",
        "observation_run_targets",
        "artifact_manifests",
        "repair_tickets",
        "ssh_connection_sessions",
    )


def control_table_names() -> tuple[str, ...]:
    """Persisted control-plane tables owned by ``PostgresControlRepository``."""

    return ("control_leases", "control_outbox", "control_traces")


def persisted_table_names() -> tuple[str, ...]:
    """Every persisted table migrated from a production SQLite runtime."""

    return (*domain_table_names(), *control_table_names())


def serial_primary_keys() -> tuple[tuple[str, str], ...]:
    return (
        ("run_events", "event_id"),
        ("collection_tasks", "task_id"),
        ("agent_decisions", "decision_id"),
        ("remediation_session_events", "event_id"),
        ("agent_turn_events", "event_id"),
    )


def domain_schema_checksum() -> str:
    return _migration_checksum(_DOMAIN_SCHEMA)


def _load_psycopg() -> tuple[Any, Any]:
    try:
        return (
            importlib.import_module("psycopg"),
            importlib.import_module("psycopg.rows").dict_row,
        )
    except ModuleNotFoundError as exc:
        raise PostgresDriverUnavailable(
            "install pilot107[postgres] to use PostgreSQL business repositories"
        ) from exc


def _migration_checksum(statements: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for statement in statements:
        digest.update(statement.strip().encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)
