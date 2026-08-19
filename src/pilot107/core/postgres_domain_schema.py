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

_MIGRATIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("004a.001.postgres_domain_schema", _DOMAIN_SCHEMA),
    ("004a.002.run_publications", _RUN_PUBLICATION_SCHEMA),
    ("004a.003.upload_sessions", _UPLOAD_SESSION_SCHEMA),
    ("004a.004.upload_sessions_tus", _UPLOAD_SESSION_TUS_SCHEMA),
    ("004a.005.agent_sessions", _AGENT_SESSION_SCHEMA),
    ("004a.006.agent_experiment_projects", _AGENT_EXPERIMENT_PROJECT_SCHEMA),
    ("004a.007.agent_workspaces", _AGENT_WORKSPACE_SCHEMA),
    ("004a.008.agent_workspace_changesets", _AGENT_WORKSPACE_CHANGESET_SCHEMA),
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
