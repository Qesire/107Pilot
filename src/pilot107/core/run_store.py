"""SQLite-backed Run persistence for Phase 0A."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pilot107.adapters.slurm import JobSnapshot, SubmitReceipt
from pilot107.core.pagination import CursorPosition
from pilot107.core.redaction import redact_sensitive_structure
from pilot107.core.states import (
    ACTIVE_JOB_RUN_STATES,
    TERMINAL_RUN_STATES,
    CapsuleState,
    CollectionState,
    DiagnosisState,
    ResultStatus,
    RunState,
)


class RunStoreFenceConflict(RuntimeError):
    """Raised when a stale submission worker attempts to persist a result."""


class CollectionTaskFenceConflict(RuntimeError):
    """Raised when a stale collection worker attempts to persist a result."""


class AgentExecutionFenceConflict(RuntimeError):
    """Raised when a stale Agent execution worker attempts to persist a result."""


class WorkflowManifestFenceConflict(RuntimeError):
    """Raised when a stale workflow writer attempts to replace manifest truth."""


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    owner: str
    state: RunState
    collection_state: CollectionState
    diagnosis_state: DiagnosisState
    capsule_state: CapsuleState
    result_status: ResultStatus
    job_id: str | None
    workdir: str
    script: str
    exit_code: str | None
    terminal_state: str | None
    submit_strategy: str | None
    submit_response: dict[str, Any]
    created_at: str
    updated_at: str
    # Human-readable name supplied by the originating Contract or direct submit
    # request. Unlike the submitted Slurm marker it is not normalized, so the
    # UI can faithfully identify the user's original job.
    job_name: str | None = None
    resource_plan: dict[str, Any] = field(default_factory=dict)
    contract_id: str | None = None
    parent_run_id: str | None = None
    lineage_reason: str | None = None
    remediation_plan_id: str | None = None
    attempt: int = 0
    workflow: dict[str, Any] = field(default_factory=dict)
    retry_not_before: str | None = None


@dataclass(frozen=True)
class RunEvent:
    event_id: int
    run_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class CollectionTaskRecord:
    task_id: int
    run_id: str
    task_type: str
    state: str
    next_attempt_at: str | None
    lease_owner: str | None
    lease_expires_at: str | None
    fencing_token: int
    generation: int
    attempts: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class EvidenceObjectRecord:
    object_id: str
    run_id: str
    category: str
    logical_path: str
    store_path: str
    source_uri: str | None
    sha256: str | None
    size_bytes: int | None
    mime_type: str | None
    collection_status: str
    collection_note: str | None
    mutable_during_run: bool
    finalized_at: str | None
    created_at: str
    updated_at: str
    # Provenance fields are additive so legacy rows remain readable.  A null
    # value means the collector predates the terminal evidence gate.
    workspace_revision: int | None = None
    workspace_digest: str | None = None
    source_revision: str | None = None
    platform_snapshot_ref: str | None = None
    integrity_checked_at: str | None = None


@dataclass(frozen=True)
class DiagnosisRecord:
    diagnosis_id: str
    run_id: str
    rule_id: str
    severity: str
    summary: str
    evidence_refs: list[str]
    suggested_patch: dict[str, Any]
    retryable: bool
    confidence: str
    created_at: str
    category: str | None = None
    stage: str | None = None
    fix_guide: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentAdviceRecord:
    advice_id: str
    run_id: str
    owner: str
    request_key: str
    state: str
    version: int
    source_run_updated_at: str
    evidence_bundle_sha256: str
    provider: str
    model: str | None
    payload: dict[str, Any]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AgentDecisionRecord:
    decision_id: int
    advice_id: str
    decision: str
    actor: str
    action_ids: list[str]
    note: str | None
    advice_version: int
    created_at: str


@dataclass(frozen=True)
class AgentActionExecutionRecord:
    execution_id: str
    advice_id: str
    action_id: str
    owner: str
    state: str
    submit_requested: bool
    derived_contract_id: str | None
    run_id: str | None
    error_code: str | None
    error_message: str | None
    created_at: str
    updated_at: str
    execution_phase: str | None = None
    execution_owner: str | None = None
    execution_fencing_token: int = 0


class RunStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
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
                );

                CREATE INDEX IF NOT EXISTS idx_runs_owner_state ON runs(owner, state);
                CREATE INDEX IF NOT EXISTS idx_runs_job_id ON runs(job_id);
                CREATE INDEX IF NOT EXISTS idx_runs_owner_created
                    ON runs(owner, created_at DESC, run_id DESC);

                CREATE TABLE IF NOT EXISTS run_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_run_events_run_id ON run_events(run_id);
                CREATE INDEX IF NOT EXISTS idx_run_events_run_type_id
                    ON run_events(run_id, event_type, event_id);

                CREATE TABLE IF NOT EXISTS workflow_manifests (
                    workflow_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    manifest_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_workflow_manifests_owner
                    ON workflow_manifests(owner, updated_at DESC, workflow_id DESC);

                CREATE TABLE IF NOT EXISTS collection_tasks (
                    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
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
                );

                CREATE TABLE IF NOT EXISTS evidence_objects (
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
                    workspace_revision INTEGER,
                    workspace_digest TEXT,
                    source_revision TEXT,
                    platform_snapshot_ref TEXT,
                    integrity_checked_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, logical_path)
                );

                CREATE INDEX IF NOT EXISTS idx_evidence_objects_run_category
                    ON evidence_objects(run_id, category);
                CREATE INDEX IF NOT EXISTS idx_evidence_objects_sha256
                    ON evidence_objects(sha256);

                CREATE TABLE IF NOT EXISTS diagnoses (
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
                );

                CREATE INDEX IF NOT EXISTS idx_diagnoses_run_id
                    ON diagnoses(run_id, created_at);

                CREATE TABLE IF NOT EXISTS agent_advice (
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
                );

                CREATE INDEX IF NOT EXISTS idx_agent_advice_run
                    ON agent_advice(run_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_agent_advice_owner_created
                    ON agent_advice(owner, created_at DESC, advice_id DESC);
                CREATE INDEX IF NOT EXISTS idx_agent_advice_owner_state_created
                    ON agent_advice(owner, state, created_at DESC, advice_id DESC);

                CREATE TABLE IF NOT EXISTS agent_decisions (
                    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    advice_id TEXT NOT NULL REFERENCES agent_advice(advice_id) ON DELETE CASCADE,
                    decision TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action_ids_json TEXT NOT NULL DEFAULT '[]',
                    note TEXT,
                    advice_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_agent_decisions_advice
                    ON agent_decisions(advice_id, decision_id);

                CREATE TABLE IF NOT EXISTS agent_action_executions (
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
                );

                CREATE INDEX IF NOT EXISTS idx_agent_action_executions_advice
                    ON agent_action_executions(advice_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_agent_action_executions_owner_created
                    ON agent_action_executions(owner, created_at DESC, execution_id DESC);
                CREATE INDEX IF NOT EXISTS idx_agent_action_executions_owner_state_created
                    ON agent_action_executions(
                        owner, state, created_at DESC, execution_id DESC
                    );
                """
            )
            self._ensure_column(
                conn,
                table="runs",
                column="contract_id",
                definition="TEXT",
            )
            self._ensure_column(
                conn,
                table="runs",
                column="job_name",
                definition="TEXT",
            )
            self._ensure_column(
                conn,
                table="runs",
                column="submit_response_json",
                definition="TEXT NOT NULL DEFAULT '{}'",
            )
            self._ensure_column(
                conn,
                table="runs",
                column="resource_plan_json",
                definition="TEXT NOT NULL DEFAULT '{}'",
            )
            self._ensure_column(
                conn,
                table="runs",
                column="submission_owner",
                definition="TEXT",
            )
            self._ensure_column(
                conn,
                table="runs",
                column="submission_fencing_token",
                definition="INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                conn,
                table="collection_tasks",
                column="fencing_token",
                definition="INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                conn,
                table="collection_tasks",
                column="generation",
                definition="INTEGER NOT NULL DEFAULT 1",
            )
            self._ensure_column(
                conn,
                table="agent_action_executions",
                column="execution_phase",
                definition="TEXT",
            )
            self._ensure_column(
                conn,
                table="agent_action_executions",
                column="execution_owner",
                definition="TEXT",
            )
            self._ensure_column(
                conn,
                table="agent_action_executions",
                column="execution_fencing_token",
                definition="INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                conn,
                table="runs",
                column="parent_run_id",
                definition="TEXT",
            )
            self._ensure_column(
                conn,
                table="runs",
                column="lineage_reason",
                definition="TEXT",
            )
            self._ensure_column(
                conn,
                table="runs",
                column="remediation_plan_id",
                definition="TEXT",
            )
            self._ensure_column(
                conn,
                table="runs",
                column="attempt",
                definition="INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                conn,
                table="runs",
                column="workflow_json",
                definition="TEXT NOT NULL DEFAULT '{}'",
            )
            self._ensure_column(
                conn,
                table="runs",
                column="retry_not_before",
                definition="TEXT",
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_parent ON runs(parent_run_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_remediation "
                "ON runs(remediation_plan_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_retry_due "
                "ON runs(lineage_reason, state, retry_not_before)"
            )
            self._ensure_column(
                conn,
                table="diagnoses",
                column="category",
                definition="TEXT",
            )
            self._ensure_column(
                conn,
                table="diagnoses",
                column="stage",
                definition="TEXT",
            )
            self._ensure_column(
                conn,
                table="diagnoses",
                column="fix_guide_json",
                definition="TEXT NOT NULL DEFAULT '{}'",
            )
            self._ensure_column(
                conn,
                table="evidence_objects",
                column="workspace_revision",
                definition="INTEGER",
            )
            self._ensure_column(
                conn,
                table="evidence_objects",
                column="workspace_digest",
                definition="TEXT",
            )
            self._ensure_column(
                conn,
                table="evidence_objects",
                column="source_revision",
                definition="TEXT",
            )
            self._ensure_column(
                conn,
                table="evidence_objects",
                column="platform_snapshot_ref",
                definition="TEXT",
            )
            self._ensure_column(
                conn,
                table="evidence_objects",
                column="integrity_checked_at",
                definition="TEXT",
            )

    def create_run(
        self,
        *,
        run_id: str,
        owner: str,
        workdir: str,
        script: str,
        resource_plan: dict[str, Any] | None = None,
        job_name: str | None = None,
        contract_id: str | None = None,
        parent_run_id: str | None = None,
        lineage_reason: str | None = None,
        remediation_plan_id: str | None = None,
        workflow: dict[str, Any] | None = None,
        retry_not_before: str | None = None,
    ) -> RunRecord:
        now = utc_now_iso()
        with self.connect() as conn:
            attempt = 0
            if parent_run_id is not None:
                parent = conn.execute(
                    "SELECT owner, attempt FROM runs WHERE run_id = ?",
                    (parent_run_id,),
                ).fetchone()
                if parent is None:
                    raise KeyError(parent_run_id)
                if str(parent["owner"]) != owner:
                    raise ValueError("parent run owner must match child run owner")
                attempt = int(parent["attempt"]) + 1
            elif lineage_reason is not None or remediation_plan_id is not None:
                raise ValueError("lineage metadata requires parent_run_id")
            if lineage_reason == "agent_remediation":
                if remediation_plan_id is None:
                    raise ValueError("agent remediation requires remediation_plan_id")
                self._validate_remediation_reference(
                    conn,
                    parent_run_id=parent_run_id,
                    owner=owner,
                    remediation_plan_id=remediation_plan_id,
                )
            elif remediation_plan_id is not None:
                raise ValueError("remediation_plan_id requires lineage_reason=agent_remediation")
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, contract_id, parent_run_id, lineage_reason,
                    remediation_plan_id, attempt, workflow_json, retry_not_before,
                    owner, state, collection_state,
                    diagnosis_state, capsule_state,
                    result_status, job_id, job_name, workdir, script, exit_code, terminal_state,
                    submit_strategy, submit_response_json, resource_plan_json,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL,
                        NULL, '{}', ?, ?, ?)
                """,
                (
                    run_id,
                    contract_id,
                    parent_run_id,
                    lineage_reason,
                    remediation_plan_id,
                    attempt,
                    json.dumps(workflow or {}, sort_keys=True),
                    retry_not_before,
                    owner,
                    RunState.VALIDATED.value,
                    CollectionState.PENDING.value,
                    DiagnosisState.PENDING.value,
                    CapsuleState.PENDING.value,
                    ResultStatus.UNKNOWN.value,
                    job_name,
                    workdir,
                    script,
                    json.dumps(resource_plan or {}, sort_keys=True),
                    now,
                    now,
                ),
            )
            self._append_event(
                conn,
                run_id=run_id,
                event_type="run.created",
                payload={
                    "parent_run_id": parent_run_id,
                    "lineage_reason": lineage_reason,
                    "remediation_plan_id": remediation_plan_id,
                    "attempt": attempt,
                    "retry_not_before": retry_not_before,
                },
            )
        return self.get_run(run_id)

    def create_workflow_manifest(
        self,
        *,
        workflow_id: str,
        owner: str,
        manifest: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        """Persist the first immutable workflow decision document."""

        now = utc_now_iso()
        encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO workflow_manifests (
                    workflow_id, owner, version, manifest_json, created_at, updated_at
                ) VALUES (?, ?, 1, ?, ?, ?)
                """,
                (workflow_id, owner, encoded, now, now),
            )
        return self.get_workflow_manifest(workflow_id, owner=owner)

    def get_workflow_manifest(
        self,
        workflow_id: str,
        *,
        owner: str,
    ) -> tuple[int, dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT version, manifest_json FROM workflow_manifests "
                "WHERE workflow_id = ? AND owner = ?",
                (workflow_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(workflow_id)
        payload = json.loads(str(row["manifest_json"]))
        if not isinstance(payload, dict):
            raise RuntimeError("workflow manifest payload is not an object")
        return int(row["version"]), payload

    def update_workflow_manifest(
        self,
        *,
        workflow_id: str,
        owner: str,
        expected_version: int,
        manifest: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        """Compare-and-swap the complete manifest as one atomic decision."""

        if expected_version <= 0:
            raise ValueError("workflow manifest expected_version must be positive")
        encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.connect() as conn:
            result = conn.execute(
                """
                UPDATE workflow_manifests
                SET version = version + 1, manifest_json = ?, updated_at = ?
                WHERE workflow_id = ? AND owner = ? AND version = ?
                """,
                (encoded, utc_now_iso(), workflow_id, owner, expected_version),
            )
            if result.rowcount != 1:
                raise WorkflowManifestFenceConflict(
                    f"workflow manifest update is fenced: {workflow_id}"
                )
        return self.get_workflow_manifest(workflow_id, owner=owner)

    def list_active_workflow_manifests(
        self,
        *,
        limit: int = 100,
    ) -> list[tuple[str, str]]:
        if limit <= 0 or limit > 1000:
            raise ValueError("workflow manifest limit must be between 1 and 1000")
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT workflow_id, owner, manifest_json FROM workflow_manifests "
                "ORDER BY updated_at, workflow_id LIMIT 1000"
            ).fetchall()
        active: list[tuple[str, str]] = []
        for row in rows:
            payload = json.loads(str(row["manifest_json"]))
            if isinstance(payload, dict) and payload.get("state") not in {
                "cancelled",
                "completed",
            }:
                active.append((str(row["workflow_id"]), str(row["owner"])))
                if len(active) == limit:
                    break
        return active

    def mark_submitting(self, run_id: str) -> RunRecord:
        return self.update_state(run_id, RunState.SUBMITTING, event_type="run.submitting")

    def claim_submission(
        self,
        run_id: str,
        *,
        lease_owner: str | None = None,
        fencing_token: int | None = None,
    ) -> bool:
        """Atomically reserve one validated run for a single submitter."""

        if (lease_owner is None) != (fencing_token is None):
            raise ValueError("lease_owner and fencing_token must be provided together")
        if fencing_token is not None and fencing_token <= 0:
            raise ValueError("fencing_token must be positive")
        now = utc_now_iso()
        with self.connect() as conn:
            if lease_owner is None:
                cursor = conn.execute(
                    """
                    UPDATE runs
                    SET state = ?, updated_at = ?
                    WHERE run_id = ? AND state = ? AND job_id IS NULL
                    """,
                    (
                        RunState.SUBMITTING.value,
                        now,
                        run_id,
                        RunState.VALIDATED.value,
                    ),
                )
            else:
                assert fencing_token is not None
                cursor = conn.execute(
                    """
                    UPDATE runs
                    SET state = ?, submission_owner = ?, submission_fencing_token = ?,
                        updated_at = ?
                    WHERE run_id = ? AND job_id IS NULL
                      AND (
                        state = ?
                        OR (state = ? AND submission_fencing_token < ?)
                      )
                    """,
                    (
                        RunState.SUBMITTING.value,
                        lease_owner,
                        fencing_token,
                        now,
                        run_id,
                        RunState.VALIDATED.value,
                        RunState.SUBMITTING.value,
                        fencing_token,
                    ),
                )
            if cursor.rowcount == 1:
                self._append_event(
                    conn,
                    run_id=run_id,
                    event_type="run.submitting",
                    payload={
                        "state": RunState.SUBMITTING.value,
                        "lease_owner": lease_owner,
                        "fencing_token": fencing_token,
                    },
                )
        return cursor.rowcount == 1

    def apply_submit_receipt(
        self,
        run_id: str,
        receipt: SubmitReceipt,
        *,
        lease_owner: str | None = None,
        fencing_token: int | None = None,
    ) -> RunRecord:
        if (lease_owner is None) != (fencing_token is None):
            raise ValueError("lease_owner and fencing_token must be provided together")
        now = utc_now_iso()
        with self.connect() as conn:
            parameters: tuple[Any, ...] = (
                RunState.SUBMITTED.value,
                receipt.job_id,
                receipt.strategy.value,
                json.dumps(receipt.raw_response, sort_keys=True),
                now,
                run_id,
            )
            fence_clause = ""
            if lease_owner is not None:
                assert fencing_token is not None
                fence_clause = (
                    " AND state = ? AND submission_owner = ? "
                    "AND submission_fencing_token = ? AND job_id IS NULL"
                )
                parameters += (
                    RunState.SUBMITTING.value,
                    lease_owner,
                    fencing_token,
                )
            result = conn.execute(
                """
                UPDATE runs
                SET state = ?,
                    job_id = ?,
                    submit_strategy = ?,
                    submit_response_json = ?,
                    updated_at = ?
                WHERE run_id = ?
                """
                + fence_clause,
                parameters,
            )
            if result.rowcount != 1:
                raise RunStoreFenceConflict(f"submission result is fenced: {run_id}")
            self._append_event(
                conn,
                run_id=run_id,
                event_type="run.submitted",
                payload={
                    "job_id": receipt.job_id,
                    "strategy": receipt.strategy.value,
                    "raw_response": receipt.raw_response,
                    "lease_owner": lease_owner,
                    "fencing_token": fencing_token,
                },
            )
            self._ensure_task(conn, run_id=run_id, task_type="submission_snapshot")
            self._ensure_task(conn, run_id=run_id, task_type="runtime_status")
        return self.get_run(run_id)

    def fail_submission(
        self,
        run_id: str,
        *,
        state: RunState,
        event_type: str,
        lease_owner: str,
        fencing_token: int,
    ) -> RunRecord:
        if state not in {RunState.SUBMIT_FAILED, RunState.SUBMISSION_UNCERTAIN}:
            raise ValueError("submission failure state is invalid")
        if fencing_token <= 0:
            raise ValueError("fencing_token must be positive")
        now = utc_now_iso()
        with self.connect() as conn:
            result = conn.execute(
                """
                UPDATE runs SET state = ?, updated_at = ?
                WHERE run_id = ? AND state = ? AND submission_owner = ?
                  AND submission_fencing_token = ? AND job_id IS NULL
                """,
                (
                    state.value,
                    now,
                    run_id,
                    RunState.SUBMITTING.value,
                    lease_owner,
                    fencing_token,
                ),
            )
            if result.rowcount != 1:
                raise RunStoreFenceConflict(f"submission failure is fenced: {run_id}")
            self._append_event(
                conn,
                run_id=run_id,
                event_type=event_type,
                payload={
                    "state": state.value,
                    "lease_owner": lease_owner,
                    "fencing_token": fencing_token,
                },
            )
        return self.get_run(run_id)

    def apply_snapshot(self, run_id: str, snapshot: JobSnapshot) -> RunRecord:
        now = utc_now_iso()
        terminal_state = snapshot.raw_state_flags[0] if snapshot.raw_state_flags else None
        result_status = ResultStatus.UNKNOWN
        if snapshot.run_state == RunState.SUCCEEDED:
            result_status = ResultStatus.COMPLETE
        elif snapshot.run_state in {RunState.FAILED, RunState.CANCELLED}:
            result_status = ResultStatus.INVALID
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET state = ?, exit_code = ?, terminal_state = ?, result_status = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    snapshot.run_state.value,
                    snapshot.exit_code,
                    terminal_state,
                    result_status.value,
                    now,
                    run_id,
                ),
            )
            self._append_event(
                conn,
                run_id=run_id,
                event_type="run.snapshot",
                payload={
                    "job_id": snapshot.job_id,
                    "state": snapshot.run_state.value,
                    "raw_state_flags": snapshot.raw_state_flags,
                    "exit_code": snapshot.exit_code,
                    "reason": snapshot.reason,
                },
            )
            if snapshot.run_state in {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}:
                self._ensure_task(conn, run_id=run_id, task_type="terminal_accounting")
                self._ensure_task(conn, run_id=run_id, task_type="logs_finalize")
                self._ensure_task(conn, run_id=run_id, task_type="environment_finalize")
                self._ensure_task(conn, run_id=run_id, task_type="outputs_inventory")
                self._ensure_task(conn, run_id=run_id, task_type="result_summary")
            else:
                self._reactivate_succeeded_task(
                    conn,
                    run_id=run_id,
                    task_type="runtime_status",
                    now=now,
                )
            self._refresh_collection_state(conn, run_id)
        return self.get_run(run_id)

    def update_state(self, run_id: str, state: RunState, *, event_type: str) -> RunRecord:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                "UPDATE runs SET state = ?, updated_at = ? WHERE run_id = ?",
                (state.value, now, run_id),
            )
            self._append_event(
                conn,
                run_id=run_id,
                event_type=event_type,
                payload={"state": state.value},
            )
        return self.get_run(run_id)

    def mark_backend_orphaned(self, run_id: str, *, backend: str, job_id: str) -> RunRecord:
        """Quarantine a job whose persisted backend no longer owns it.

        The old job is deliberately not reported as failed or cancelled: its
        terminal outcome is unknown.  Marking it terminal in the control plane
        stops an infinite reconciliation loop while preserving an auditable
        record that an operator may import or reconcile manually.
        """

        now = utc_now_iso()
        with self.connect() as conn:
            result = conn.execute(
                """
                UPDATE runs
                SET state = ?, terminal_state = ?, result_status = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    RunState.ORPHANED.value,
                    "BACKEND_OWNERSHIP_LOST",
                    ResultStatus.UNKNOWN.value,
                    now,
                    run_id,
                ),
            )
            if result.rowcount != 1:
                raise KeyError(run_id)
            self._append_event(
                conn,
                run_id=run_id,
                event_type="run.backend_orphaned",
                payload={
                    "backend": backend,
                    "job_id": job_id,
                    "state": RunState.ORPHANED.value,
                    "reason": "backend_ownership_lost",
                },
            )
        return self.get_run(run_id)

    def update_capsule_state(
        self,
        run_id: str,
        state: CapsuleState,
        *,
        event_type: str = "capsule.state_changed",
        payload: dict[str, Any] | None = None,
    ) -> RunRecord:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                "UPDATE runs SET capsule_state = ?, updated_at = ? WHERE run_id = ?",
                (state.value, now, run_id),
            )
            self._append_event(
                conn,
                run_id=run_id,
                event_type=event_type,
                payload={"capsule_state": state.value, **(payload or {})},
            )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> RunRecord:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return _row_to_run(row)

    def list_runs_page(
        self,
        *,
        owner: str,
        states: tuple[str, ...] = (),
        contract_id: str | None = None,
        recipe_version_id: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        query: str | None = None,
        cursor: CursorPosition | None = None,
        limit: int = 50,
    ) -> tuple[list[RunRecord], CursorPosition | None]:
        if not owner:
            raise ValueError("owner is required")
        _require_page_limit(limit)
        conditions = ["runs.owner = ?"]
        values: list[Any] = [owner]
        if states:
            placeholders = ",".join("?" for _ in states)
            conditions.append(f"runs.state IN ({placeholders})")
            values.extend(states)
        if contract_id is not None:
            conditions.append("runs.contract_id = ?")
            values.append(contract_id)
        if recipe_version_id is not None:
            conditions.append(
                "EXISTS (SELECT 1 FROM contracts "
                "WHERE contracts.contract_id = runs.contract_id "
                "AND contracts.owner = runs.owner "
                "AND contracts.recipe_version_id = ?)"
            )
            values.append(recipe_version_id)
        if created_after is not None:
            conditions.append("runs.created_at >= ?")
            values.append(created_after)
        if created_before is not None:
            conditions.append("runs.created_at <= ?")
            values.append(created_before)
        if query is not None:
            pattern = f"%{_escape_like(query)}%"
            conditions.append(
                "(runs.run_id LIKE ? ESCAPE '\\' "
                "OR runs.job_id LIKE ? ESCAPE '\\' "
                "OR runs.job_name LIKE ? ESCAPE '\\' "
                "OR runs.contract_id LIKE ? ESCAPE '\\' "
                "OR runs.workdir LIKE ? ESCAPE '\\')"
            )
            values.extend([pattern, pattern, pattern, pattern, pattern])
        if cursor is not None:
            conditions.append("(runs.created_at < ? OR (runs.created_at = ? AND runs.run_id < ?))")
            values.extend([cursor.primary, cursor.primary, cursor.secondary])
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT runs.* FROM runs WHERE "
                + " AND ".join(conditions)
                + " ORDER BY runs.created_at DESC, runs.run_id DESC LIMIT ?",
                (*values, limit + 1),
            ).fetchall()
        selected = rows[:limit]
        items = [_row_to_run(row) for row in selected]
        next_position = None
        if len(rows) > limit and selected:
            last = selected[-1]
            next_position = CursorPosition(
                primary=str(last["created_at"]),
                secondary=str(last["run_id"]),
            )
        return items, next_position

    def list_child_runs(self, parent_run_id: str) -> list[RunRecord]:
        parent = self.get_run(parent_run_id)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE parent_run_id = ? ORDER BY created_at, run_id",
                (parent_run_id,),
            ).fetchall()
        children = [_row_to_run(row) for row in rows]
        if any(child.owner != parent.owner for child in children):
            raise RuntimeError("run children cross owner boundary")
        return children

    def list_run_lineage(self, run_id: str) -> list[RunRecord]:
        lineage: list[RunRecord] = []
        seen: set[str] = set()
        current = self.get_run(run_id)
        while True:
            if current.run_id in seen:
                raise RuntimeError("run lineage contains a cycle")
            seen.add(current.run_id)
            lineage.append(current)
            if current.parent_run_id is None:
                break
            current = self.get_run(current.parent_run_id)
            if current.owner != lineage[0].owner:
                raise RuntimeError("run lineage crosses owner boundary")
        lineage.reverse()
        return lineage

    def list_run_family(
        self,
        run_id: str,
        *,
        max_nodes: int = 500,
    ) -> tuple[list[RunRecord], list[RunRecord]]:
        if max_nodes <= 0 or max_nodes > 5000:
            raise ValueError("max_nodes must be between 1 and 5000")
        lineage = self.list_run_lineage(run_id)
        root = lineage[0]
        with self.connect() as conn:
            rows = conn.execute(
                """
                WITH RECURSIVE family(run_id) AS (
                    SELECT run_id FROM runs WHERE run_id = ? AND owner = ?
                    UNION ALL
                    SELECT child.run_id
                    FROM runs AS child
                    JOIN family AS parent ON child.parent_run_id = parent.run_id
                    WHERE child.owner = ?
                )
                SELECT runs.* FROM runs JOIN family USING (run_id)
                ORDER BY runs.attempt, runs.created_at, runs.run_id
                LIMIT ?
                """,
                (root.run_id, root.owner, root.owner, max_nodes + 1),
            ).fetchall()
            if len(rows) > max_nodes:
                raise RuntimeError("run family exceeds graph node limit")
            family = [_row_to_run(row) for row in rows]
            family_ids = {item.run_id for item in family}
            dependency_ids = {
                dependency_id
                for item in family
                for dependency_id in item.workflow.get("dependencies", [])
                if isinstance(dependency_id, str) and dependency_id not in family_ids
            }
            dependencies: list[RunRecord] = []
            if dependency_ids:
                placeholders = ",".join("?" for _ in dependency_ids)
                dependency_rows = conn.execute(
                    f"SELECT * FROM runs WHERE owner = ? AND run_id IN ({placeholders}) "
                    "ORDER BY created_at, run_id",
                    (root.owner, *sorted(dependency_ids)),
                ).fetchall()
                dependencies = [_row_to_run(row) for row in dependency_rows]
        return family, dependencies

    def list_due_workflow_retries(self, *, limit: int = 50) -> list[RunRecord]:
        if limit <= 0:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM runs
                WHERE lineage_reason = 'workflow_retry'
                  AND state = ?
                  AND job_id IS NULL
                  AND retry_not_before IS NOT NULL
                  AND retry_not_before <= ?
                ORDER BY retry_not_before, created_at
                LIMIT ?
                """,
                (RunState.VALIDATED.value, utc_now_iso(), limit),
            ).fetchall()
        return [_row_to_run(row) for row in rows]

    @staticmethod
    def _validate_remediation_reference(
        conn: sqlite3.Connection,
        *,
        parent_run_id: str | None,
        owner: str,
        remediation_plan_id: str,
    ) -> None:
        advice_id, separator, action_id = remediation_plan_id.partition(":")
        if not separator or not advice_id or not action_id:
            raise ValueError("remediation_plan_id must be '<advice_id>:<action_id>'")
        advice = conn.execute(
            "SELECT run_id, owner, state, payload_json FROM agent_advice WHERE advice_id = ?",
            (advice_id,),
        ).fetchone()
        if advice is None:
            raise ValueError("remediation advice does not exist")
        if str(advice["run_id"]) != parent_run_id or str(advice["owner"]) != owner:
            raise ValueError("remediation advice does not belong to the parent run")
        if str(advice["state"]) != "approved":
            raise ValueError("remediation advice is not approved")
        payload = json.loads(str(advice["payload_json"]))
        allowed = {
            str(action.get("action_id"))
            for action in payload.get("actions", [])
            if isinstance(action, dict) and action.get("policy_status") == "allowed_preview"
        }
        decision = conn.execute(
            """
            SELECT action_ids_json
            FROM agent_decisions
            WHERE advice_id = ? AND decision = 'approve'
            ORDER BY decision_id DESC
            LIMIT 1
            """,
            (advice_id,),
        ).fetchone()
        selected = (
            set(json.loads(str(decision["action_ids_json"]))) if decision is not None else set()
        )
        if action_id not in allowed or action_id not in selected:
            raise ValueError("remediation action was not approved")

    def list_active_job_runs(self, *, limit: int = 100) -> list[RunRecord]:
        if limit <= 0:
            return []
        state_values = sorted(state.value for state in ACTIVE_JOB_RUN_STATES)
        placeholders = ",".join("?" for _ in state_values)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM runs
                WHERE job_id IS NOT NULL AND state IN ({placeholders})
                ORDER BY updated_at ASC, created_at ASC
                LIMIT ?
                """,
                (*state_values, limit),
            ).fetchall()
        return [_row_to_run(row) for row in rows]

    def list_events(self, run_id: str) -> list[RunEvent]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM run_events WHERE run_id = ? ORDER BY event_id",
                (run_id,),
            ).fetchall()
        return [
            RunEvent(
                event_id=int(row["event_id"]),
                run_id=str(row["run_id"]),
                event_type=str(row["event_type"]),
                payload=json.loads(str(row["payload_json"])),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def list_events_page(
        self,
        run_id: str,
        *,
        event_types: tuple[str, ...] = (),
        after_event_id: int = 0,
        limit: int = 100,
    ) -> tuple[list[RunEvent], int | None]:
        _require_page_limit(limit)
        if after_event_id < 0:
            raise ValueError("after_event_id cannot be negative")
        conditions = ["run_id = ?", "event_id > ?"]
        values: list[Any] = [run_id, after_event_id]
        if event_types:
            placeholders = ",".join("?" for _ in event_types)
            conditions.append(f"event_type IN ({placeholders})")
            values.extend(event_types)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM run_events WHERE "
                + " AND ".join(conditions)
                + " ORDER BY event_id ASC LIMIT ?",
                (*values, limit + 1),
            ).fetchall()
        selected = rows[:limit]
        items = [_row_to_run_event(row) for row in selected]
        next_event_id = int(selected[-1]["event_id"]) if len(rows) > limit and selected else None
        return items, next_event_id

    def append_event(self, *, run_id: str, event_type: str, payload: dict[str, Any]) -> RunEvent:
        with self.connect() as conn:
            self._append_event(conn, run_id=run_id, event_type=event_type, payload=payload)
            row = conn.execute(
                "SELECT * FROM run_events WHERE event_id = last_insert_rowid()",
            ).fetchone()
        if row is None:
            raise RuntimeError("failed to read appended run event")
        return RunEvent(
            event_id=int(row["event_id"]),
            run_id=str(row["run_id"]),
            event_type=str(row["event_type"]),
            payload=json.loads(str(row["payload_json"])),
            created_at=str(row["created_at"]),
        )

    def create_agent_advice(
        self,
        *,
        advice_id: str,
        run_id: str,
        owner: str,
        request_key: str,
        state: str,
        source_run_updated_at: str,
        evidence_bundle_sha256: str,
        provider: str,
        model: str | None,
        payload: dict[str, Any],
    ) -> tuple[AgentAdviceRecord, bool]:
        now = utc_now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO agent_advice (
                    advice_id, run_id, owner, request_key, state, version,
                    source_run_updated_at, evidence_bundle_sha256, provider, model,
                    payload_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    advice_id,
                    run_id,
                    owner,
                    request_key,
                    state,
                    source_run_updated_at,
                    evidence_bundle_sha256,
                    provider,
                    model,
                    json.dumps(payload, sort_keys=True),
                    now,
                    now,
                ),
            )
            created = cursor.rowcount == 1
            if created:
                self._append_event(
                    conn,
                    run_id=run_id,
                    event_type="agent.advice_created",
                    payload={
                        "advice_id": advice_id,
                        "state": state,
                        "provider": provider,
                    },
                )
            row = conn.execute(
                "SELECT * FROM agent_advice WHERE run_id = ? AND request_key = ?",
                (run_id, request_key),
            ).fetchone()
        if row is None:
            raise RuntimeError("failed to read agent advice")
        return _row_to_agent_advice(row), created

    def get_agent_advice(self, advice_id: str) -> AgentAdviceRecord:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_advice WHERE advice_id = ?",
                (advice_id,),
            ).fetchone()
        if row is None:
            raise KeyError(advice_id)
        return _row_to_agent_advice(row)

    def list_agent_advice_page(
        self,
        *,
        owner: str,
        states: tuple[str, ...] = (),
        run_id: str | None = None,
        cursor: CursorPosition | None = None,
        limit: int = 50,
    ) -> tuple[list[AgentAdviceRecord], CursorPosition | None]:
        if not owner:
            raise ValueError("owner is required")
        _require_page_limit(limit)
        conditions = ["owner = ?"]
        values: list[Any] = [owner]
        if states:
            placeholders = ",".join("?" for _ in states)
            conditions.append(f"state IN ({placeholders})")
            values.extend(states)
        if run_id is not None:
            conditions.append("run_id = ?")
            values.append(run_id)
        if cursor is not None:
            conditions.append("(created_at < ? OR (created_at = ? AND advice_id < ?))")
            values.extend([cursor.primary, cursor.primary, cursor.secondary])
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_advice WHERE "
                + " AND ".join(conditions)
                + " ORDER BY created_at DESC, advice_id DESC LIMIT ?",
                (*values, limit + 1),
            ).fetchall()
        selected = rows[:limit]
        items = [_row_to_agent_advice(row) for row in selected]
        next_position = None
        if len(rows) > limit and selected:
            last = selected[-1]
            next_position = CursorPosition(
                primary=str(last["created_at"]), secondary=str(last["advice_id"])
            )
        return items, next_position

    def decide_agent_advice(
        self,
        *,
        advice_id: str,
        expected_version: int,
        expected_state: str,
        new_state: str,
        decision: str,
        actor: str,
        action_ids: list[str],
        note: str | None,
    ) -> AgentAdviceRecord:
        now = utc_now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_advice
                SET state = ?, version = version + 1, updated_at = ?
                WHERE advice_id = ? AND version = ? AND state = ?
                """,
                (new_state, now, advice_id, expected_version, expected_state),
            )
            if cursor.rowcount != 1:
                raise AgentAdviceConflict(advice_id)
            conn.execute(
                """
                INSERT INTO agent_decisions (
                    advice_id, decision, actor, action_ids_json, note,
                    advice_version, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    advice_id,
                    decision,
                    actor,
                    json.dumps(action_ids, sort_keys=True),
                    note,
                    expected_version,
                    now,
                ),
            )
            advice_run = conn.execute(
                "SELECT run_id FROM agent_advice WHERE advice_id = ?",
                (advice_id,),
            ).fetchone()
            if advice_run is None:
                raise RuntimeError("failed to read decided agent advice run")
            self._append_event(
                conn,
                run_id=str(advice_run["run_id"]),
                event_type="agent.advice_decided",
                payload={
                    "advice_id": advice_id,
                    "state": new_state,
                    "decision": decision,
                    "actor": actor,
                },
            )
            row = conn.execute(
                "SELECT * FROM agent_advice WHERE advice_id = ?",
                (advice_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("failed to read decided agent advice")
        return _row_to_agent_advice(row)

    def list_agent_decisions(self, advice_id: str) -> list[AgentDecisionRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_decisions WHERE advice_id = ? ORDER BY decision_id",
                (advice_id,),
            ).fetchall()
        return [_row_to_agent_decision(row) for row in rows]

    def claim_agent_action_execution(
        self,
        *,
        execution_id: str,
        advice_id: str,
        action_id: str,
        owner: str,
        submit_requested: bool,
    ) -> tuple[AgentActionExecutionRecord, bool]:
        now = utc_now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO agent_action_executions (
                    execution_id, advice_id, action_id, owner, state,
                    submit_requested, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'executing', ?, ?, ?)
                """,
                (
                    execution_id,
                    advice_id,
                    action_id,
                    owner,
                    1 if submit_requested else 0,
                    now,
                    now,
                ),
            )
            created = cursor.rowcount == 1
            if not created:
                stale_before = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
                reclaimed = conn.execute(
                    """
                    UPDATE agent_action_executions
                    SET submit_requested = ?, updated_at = ?
                    WHERE advice_id = ? AND action_id = ?
                      AND state = 'executing' AND updated_at <= ?
                    """,
                    (
                        1 if submit_requested else 0,
                        now,
                        advice_id,
                        action_id,
                        stale_before,
                    ),
                )
                created = reclaimed.rowcount == 1
            row = conn.execute(
                "SELECT * FROM agent_action_executions WHERE advice_id = ? AND action_id = ?",
                (advice_id, action_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("failed to claim agent action execution")
        record = _row_to_agent_action_execution(row)
        if record.owner != owner or record.execution_id != execution_id:
            raise ValueError("agent action execution idempotency conflict")
        return record, created

    def begin_agent_action_execution(
        self,
        execution_id: str,
        *,
        expected_state: str,
        submit_requested: bool,
    ) -> tuple[AgentActionExecutionRecord, bool]:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_action_executions
                SET state = 'executing', submit_requested = ?, updated_at = ?
                WHERE execution_id = ? AND state = ?
                """,
                (
                    1 if submit_requested else 0,
                    utc_now_iso(),
                    execution_id,
                    expected_state,
                ),
            )
        return self.get_agent_action_execution(execution_id), cursor.rowcount == 1

    def claim_agent_action_execution_fenced(
        self,
        *,
        execution_id: str,
        advice_id: str,
        action_id: str,
        owner: str,
        submit_requested: bool,
        execution_phase: str,
        execution_owner: str,
        fencing_token: int,
    ) -> tuple[AgentActionExecutionRecord, bool]:
        if execution_phase not in {"prepare", "submit"}:
            raise ValueError("agent execution phase is invalid")
        if fencing_token <= 0:
            raise ValueError("fencing_token must be positive")
        now = utc_now_iso()
        with self.connect() as conn:
            inserted = conn.execute(
                """
                INSERT OR IGNORE INTO agent_action_executions (
                    execution_id, advice_id, action_id, owner, state,
                    submit_requested, execution_phase, execution_owner,
                    execution_fencing_token, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'executing', ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    advice_id,
                    action_id,
                    owner,
                    1 if submit_requested else 0,
                    execution_phase,
                    execution_owner,
                    fencing_token,
                    now,
                    now,
                ),
            )
            claimed = inserted.rowcount == 1
            if not claimed:
                if execution_phase == "submit":
                    result = conn.execute(
                        """
                        UPDATE agent_action_executions
                        SET state = 'executing', submit_requested = 1,
                            execution_phase = ?, execution_owner = ?,
                            execution_fencing_token = ?, updated_at = ?
                        WHERE execution_id = ? AND state = 'prepared'
                        """,
                        (
                            execution_phase,
                            execution_owner,
                            fencing_token,
                            now,
                            execution_id,
                        ),
                    )
                    claimed = result.rowcount == 1
                if not claimed:
                    result = conn.execute(
                        """
                        UPDATE agent_action_executions
                        SET submit_requested = ?, execution_owner = ?,
                            execution_fencing_token = ?, updated_at = ?
                        WHERE execution_id = ? AND state = 'executing'
                          AND execution_phase = ?
                          AND execution_fencing_token < ?
                        """,
                        (
                            1 if submit_requested else 0,
                            execution_owner,
                            fencing_token,
                            now,
                            execution_id,
                            execution_phase,
                            fencing_token,
                        ),
                    )
                    claimed = result.rowcount == 1
            row = conn.execute(
                "SELECT * FROM agent_action_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("failed to claim fenced agent action execution")
        record = _row_to_agent_action_execution(row)
        if record.advice_id != advice_id or record.action_id != action_id or record.owner != owner:
            raise ValueError("agent action execution idempotency conflict")
        return record, claimed

    def update_agent_action_execution(
        self,
        execution_id: str,
        *,
        state: str,
        submit_requested: bool | None = None,
        derived_contract_id: str | None = None,
        run_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        execution_phase: str | None = None,
        execution_owner: str | None = None,
        fencing_token: int | None = None,
    ) -> AgentActionExecutionRecord:
        if fencing_token is not None and (execution_phase is None or execution_owner is None):
            raise ValueError("fenced update requires execution phase and owner")
        existing = self.get_agent_action_execution(execution_id)
        effective_submit_requested = (
            submit_requested if submit_requested is not None else existing.submit_requested
        )
        with self.connect() as conn:
            parameters: tuple[Any, ...] = (
                state,
                1 if effective_submit_requested else 0,
                derived_contract_id
                if derived_contract_id is not None
                else existing.derived_contract_id,
                run_id if run_id is not None else existing.run_id,
                error_code,
                error_message,
                utc_now_iso(),
                execution_id,
            )
            fence_clause = ""
            if fencing_token is not None:
                fence_clause = (
                    " AND state = 'executing' AND execution_phase = ? "
                    "AND execution_owner = ? AND execution_fencing_token = ?"
                )
                parameters += (execution_phase, execution_owner, fencing_token)
            result = conn.execute(
                """
                UPDATE agent_action_executions
                SET state = ?, submit_requested = ?, derived_contract_id = ?,
                    run_id = ?, error_code = ?, error_message = ?, updated_at = ?
                WHERE execution_id = ?
                """
                + fence_clause,
                parameters,
            )
            if result.rowcount != 1:
                raise AgentExecutionFenceConflict(
                    f"agent execution result is fenced: {execution_id}"
                )
        return self.get_agent_action_execution(execution_id)

    def get_agent_action_execution(self, execution_id: str) -> AgentActionExecutionRecord:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_action_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        if row is None:
            raise KeyError(execution_id)
        return _row_to_agent_action_execution(row)

    def list_agent_action_executions(
        self,
        advice_id: str,
    ) -> list[AgentActionExecutionRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_action_executions "
                "WHERE advice_id = ? ORDER BY created_at, execution_id",
                (advice_id,),
            ).fetchall()
        return [_row_to_agent_action_execution(row) for row in rows]

    def list_agent_action_executions_page(
        self,
        *,
        owner: str,
        states: tuple[str, ...] = (),
        advice_id: str | None = None,
        cursor: CursorPosition | None = None,
        limit: int = 50,
    ) -> tuple[list[AgentActionExecutionRecord], CursorPosition | None]:
        if not owner:
            raise ValueError("owner is required")
        _require_page_limit(limit)
        conditions = ["owner = ?"]
        values: list[Any] = [owner]
        if states:
            placeholders = ",".join("?" for _ in states)
            conditions.append(f"state IN ({placeholders})")
            values.extend(states)
        if advice_id is not None:
            conditions.append("advice_id = ?")
            values.append(advice_id)
        if cursor is not None:
            conditions.append("(created_at < ? OR (created_at = ? AND execution_id < ?))")
            values.extend([cursor.primary, cursor.primary, cursor.secondary])
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_action_executions WHERE "
                + " AND ".join(conditions)
                + " ORDER BY created_at DESC, execution_id DESC LIMIT ?",
                (*values, limit + 1),
            ).fetchall()
        selected = rows[:limit]
        items = [_row_to_agent_action_execution(row) for row in selected]
        next_position = None
        if len(rows) > limit and selected:
            last = selected[-1]
            next_position = CursorPosition(
                primary=str(last["created_at"]),
                secondary=str(last["execution_id"]),
            )
        return items, next_position

    def list_collection_tasks(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM collection_tasks WHERE run_id = ? ORDER BY task_id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def defer_logs_finalize_for_runtime_watch(self, run_id: str) -> bool:
        now = utc_now_iso()
        with self.connect() as conn:
            updated = conn.execute(
                "UPDATE collection_tasks SET state = 'waiting_runtime_watch', "
                "next_attempt_at = NULL, updated_at = ? "
                "WHERE run_id = ? AND task_type = 'logs_finalize' "
                "AND state IN ('pending', 'failed_retryable')",
                (now, run_id),
            )
            if updated.rowcount:
                self._append_event(
                    conn,
                    run_id=run_id,
                    event_type="runtime_watch.terminal_drain_started",
                    payload={},
                )
                self._refresh_collection_state(conn, run_id)
        return updated.rowcount == 1

    def release_logs_finalize_after_runtime_watch(self, run_id: str) -> bool:
        now = utc_now_iso()
        with self.connect() as conn:
            updated = conn.execute(
                "UPDATE collection_tasks SET state = 'pending', next_attempt_at = ?, "
                "updated_at = ? WHERE run_id = ? AND task_type = 'logs_finalize' "
                "AND state = 'waiting_runtime_watch'",
                (now, now, run_id),
            )
            if updated.rowcount:
                self._append_event(
                    conn,
                    run_id=run_id,
                    event_type="runtime_watch.terminal_drain_completed",
                    payload={},
                )
                self._refresh_collection_state(conn, run_id)
        return updated.rowcount == 1

    def upsert_evidence_objects(
        self,
        run_id: str,
        objects: list[dict[str, Any]],
    ) -> list[EvidenceObjectRecord]:
        now = utc_now_iso()
        with self.connect() as conn:
            for obj in objects:
                conn.execute(
                    """
                    INSERT INTO evidence_objects (
                        object_id, run_id, category, logical_path, store_path, source_uri,
                        sha256, size_bytes, mime_type, collection_status, collection_note,
                        mutable_during_run, finalized_at, workspace_revision, workspace_digest,
                        source_revision, platform_snapshot_ref, integrity_checked_at,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, logical_path) DO UPDATE SET
                        category = excluded.category,
                        store_path = excluded.store_path,
                        source_uri = excluded.source_uri,
                        sha256 = excluded.sha256,
                        size_bytes = excluded.size_bytes,
                        mime_type = excluded.mime_type,
                        collection_status = excluded.collection_status,
                        collection_note = excluded.collection_note,
                        mutable_during_run = excluded.mutable_during_run,
                        finalized_at = excluded.finalized_at,
                        workspace_revision = excluded.workspace_revision,
                        workspace_digest = excluded.workspace_digest,
                        source_revision = excluded.source_revision,
                        platform_snapshot_ref = excluded.platform_snapshot_ref,
                        integrity_checked_at = COALESCE(
                            evidence_objects.integrity_checked_at,
                            excluded.integrity_checked_at
                        ),
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(obj["object_id"]),
                        run_id,
                        str(obj["category"]),
                        str(obj["logical_path"]),
                        str(obj["store_path"]),
                        None if obj.get("source_uri") is None else str(obj["source_uri"]),
                        None if obj.get("sha256") is None else str(obj["sha256"]),
                        None if obj.get("size_bytes") is None else int(obj["size_bytes"]),
                        None if obj.get("mime_type") is None else str(obj["mime_type"]),
                        str(obj.get("collection_status") or "collected"),
                        None if obj.get("collection_note") is None else str(obj["collection_note"]),
                        1 if bool(obj.get("mutable_during_run", False)) else 0,
                        None if obj.get("finalized_at") is None else str(obj["finalized_at"]),
                        (
                            None
                            if obj.get("workspace_revision") is None
                            else int(obj["workspace_revision"])
                        ),
                        (
                            None
                            if obj.get("workspace_digest") is None
                            else str(obj["workspace_digest"])
                        ),
                        (
                            None
                            if obj.get("source_revision") is None
                            else str(obj["source_revision"])
                        ),
                        (
                            None
                            if obj.get("platform_snapshot_ref") is None
                            else str(obj["platform_snapshot_ref"])
                        ),
                        (
                            None
                            if obj.get("integrity_checked_at") is None
                            else str(obj["integrity_checked_at"])
                        ),
                        now,
                        now,
                    ),
                )
        return self.list_evidence_objects(run_id)

    def mark_evidence_integrity_checked(
        self,
        run_id: str,
        logical_paths: tuple[str, ...] | list[str],
        *,
        checked_at: str | None = None,
    ) -> None:
        """Persist the timestamp of a successful terminal integrity verification."""
        paths = tuple(dict.fromkeys(str(path) for path in logical_paths if str(path)))
        if not paths:
            return
        now = checked_at or utc_now_iso()
        placeholders = ",".join("?" for _ in paths)
        with self.connect() as conn:
            conn.execute(
                "UPDATE evidence_objects SET integrity_checked_at = COALESCE("
                "integrity_checked_at, ?) WHERE run_id = ? AND logical_path IN ("
                + placeholders
                + ")",
                (now, run_id, *paths),
            )

    def list_evidence_objects(
        self,
        run_id: str,
        *,
        category: str | None = None,
    ) -> list[EvidenceObjectRecord]:
        with self.connect() as conn:
            if category is None:
                rows = conn.execute(
                    """
                    SELECT * FROM evidence_objects
                    WHERE run_id = ?
                    ORDER BY category, logical_path
                    """,
                    (run_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM evidence_objects
                    WHERE run_id = ? AND category = ?
                    ORDER BY logical_path
                    """,
                    (run_id, category),
                ).fetchall()
        return [_row_to_evidence_object(row) for row in rows]

    def list_runs_ready_for_diagnosis(self, *, limit: int = 100) -> list[RunRecord]:
        if limit <= 0:
            return []
        terminal_values = sorted(state.value for state in TERMINAL_RUN_STATES)
        placeholders = ",".join("?" for _ in terminal_values)
        collection_values = (CollectionState.SUCCEEDED.value, CollectionState.DEGRADED.value)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM runs
                WHERE state IN ({placeholders})
                  AND collection_state IN (?, ?)
                  AND diagnosis_state = ?
                ORDER BY updated_at ASC, created_at ASC
                LIMIT ?
                """,
                (
                    *terminal_values,
                    *collection_values,
                    DiagnosisState.PENDING.value,
                    limit,
                ),
            ).fetchall()
        return [_row_to_run(row) for row in rows]

    def replace_diagnoses(
        self,
        run_id: str,
        diagnoses: list[dict[str, Any]],
    ) -> list[DiagnosisRecord]:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute("DELETE FROM diagnoses WHERE run_id = ?", (run_id,))
            for diagnosis in diagnoses:
                conn.execute(
                    """
                    INSERT INTO diagnoses (
                        diagnosis_id, run_id, rule_id, severity, summary,
                        evidence_refs_json, suggested_patch_json, retryable,
                        confidence, category, stage, fix_guide_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(diagnosis["diagnosis_id"]),
                        run_id,
                        str(diagnosis["rule_id"]),
                        str(diagnosis["severity"]),
                        str(diagnosis["summary"]),
                        json.dumps(diagnosis.get("evidence_refs", []), sort_keys=True),
                        json.dumps(diagnosis.get("suggested_patch", {}), sort_keys=True),
                        1 if bool(diagnosis.get("retryable", False)) else 0,
                        str(diagnosis.get("confidence") or "medium"),
                        None if diagnosis.get("category") is None else str(diagnosis["category"]),
                        None if diagnosis.get("stage") is None else str(diagnosis["stage"]),
                        json.dumps(diagnosis.get("fix_guide", {}), sort_keys=True),
                        now,
                    ),
                )
            diagnosis_state = DiagnosisState.SUCCEEDED if diagnoses else DiagnosisState.SKIPPED
            conn.execute(
                "UPDATE runs SET diagnosis_state = ?, updated_at = ? WHERE run_id = ?",
                (diagnosis_state.value, now, run_id),
            )
            self._append_event(
                conn,
                run_id=run_id,
                event_type="diagnosis.updated",
                payload={
                    "diagnosis_state": diagnosis_state.value,
                    "diagnosis_count": len(diagnoses),
                },
            )
        return self.list_diagnoses(run_id)

    def list_diagnoses(self, run_id: str) -> list[DiagnosisRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM diagnoses
                WHERE run_id = ?
                ORDER BY created_at, rule_id
                """,
                (run_id,),
            ).fetchall()
        return [_row_to_diagnosis(row) for row in rows]

    def list_due_collection_tasks(self, *, limit: int = 100) -> list[CollectionTaskRecord]:
        if limit <= 0:
            return []
        now = utc_now_iso()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM collection_tasks
                WHERE state IN ('pending', 'failed_retryable')
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY
                    CASE task_type
                        WHEN 'submission_snapshot' THEN 10
                        WHEN 'runtime_status' THEN 20
                        WHEN 'terminal_accounting' THEN 30
                        WHEN 'logs_finalize' THEN 40
                        WHEN 'environment_finalize' THEN 50
                        WHEN 'outputs_inventory' THEN 60
                        WHEN 'result_summary' THEN 90
                        ELSE 80
                    END ASC,
                    updated_at ASC,
                    created_at ASC
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
        return [_row_to_collection_task(row) for row in rows]

    def list_collection_tasks_for_dispatch(
        self,
        *,
        limit: int = 100,
    ) -> list[CollectionTaskRecord]:
        """Return due work plus expired legacy leases for durable outbox seeding."""

        if limit <= 0:
            return []
        now = utc_now_iso()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM collection_tasks
                WHERE (
                    state IN ('pending', 'failed_retryable')
                    AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ) OR (
                    state = 'running'
                    AND lease_expires_at IS NOT NULL
                    AND lease_expires_at <= ?
                )
                ORDER BY updated_at, created_at, task_id
                LIMIT ?
                """,
                (now, now, limit),
            ).fetchall()
        return [_row_to_collection_task(row) for row in rows]

    def claim_collection_task(
        self,
        task_id: int,
        *,
        lease_owner: str,
        fencing_token: int,
        generation: int,
        lease_expires_at: str,
    ) -> CollectionTaskRecord | None:
        if fencing_token <= 0:
            raise ValueError("fencing_token must be positive")
        if generation <= 0:
            raise ValueError("generation must be positive")
        now = utc_now_iso()
        with self.connect() as conn:
            result = conn.execute(
                """
                UPDATE collection_tasks
                SET state = 'running', lease_owner = ?, lease_expires_at = ?,
                    fencing_token = ?, attempts = attempts + 1, updated_at = ?
                WHERE task_id = ? AND generation = ?
                  AND (
                    (
                      state IN ('pending', 'failed_retryable')
                      AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                    ) OR (
                      state = 'running' AND fencing_token < ?
                    )
                  )
                """,
                (
                    lease_owner,
                    lease_expires_at,
                    fencing_token,
                    now,
                    task_id,
                    generation,
                    now,
                    fencing_token,
                ),
            )
            if result.rowcount != 1:
                return None
            task = self._get_task_in_conn(conn, task_id)
            self._append_event(
                conn,
                run_id=task.run_id,
                event_type="collection.task_acquired",
                payload={
                    "task_id": task.task_id,
                    "task_type": task.task_type,
                    "lease_owner": lease_owner,
                    "lease_expires_at": lease_expires_at,
                    "fencing_token": fencing_token,
                    "generation": generation,
                },
            )
            self._refresh_collection_state(conn, task.run_id)
        return self.get_collection_task(task_id)

    def acquire_due_collection_tasks(
        self,
        *,
        lease_owner: str,
        limit: int = 100,
        lease_seconds: int = 300,
    ) -> list[CollectionTaskRecord]:
        if limit <= 0:
            return []
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = utc_now_iso()
        lease_expires_at = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
        acquired: list[CollectionTaskRecord] = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT *
                FROM collection_tasks
                WHERE (
                    state IN ('pending', 'failed_retryable')
                    AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ) OR (
                    state = 'running'
                    AND lease_expires_at IS NOT NULL
                    AND lease_expires_at <= ?
                )
                ORDER BY updated_at ASC, created_at ASC
                LIMIT ?
                """,
                (now, now, limit),
            ).fetchall()
            for row in rows:
                task_id = int(row["task_id"])
                result = conn.execute(
                    """
                    UPDATE collection_tasks
                    SET state = 'running',
                        lease_owner = ?,
                        lease_expires_at = ?,
                        fencing_token = fencing_token + 1,
                        attempts = attempts + 1,
                        updated_at = ?
                    WHERE task_id = ?
                      AND (
                        (
                          state IN ('pending', 'failed_retryable')
                          AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                        ) OR (
                          state = 'running'
                          AND lease_expires_at IS NOT NULL
                          AND lease_expires_at <= ?
                        )
                      )
                    """,
                    (lease_owner, lease_expires_at, now, task_id, now, now),
                )
                if result.rowcount != 1:
                    continue
                task = self._get_task_in_conn(conn, task_id)
                self._append_event(
                    conn,
                    run_id=task.run_id,
                    event_type="collection.task_acquired",
                    payload={
                        "task_id": task.task_id,
                        "task_type": task.task_type,
                        "lease_owner": lease_owner,
                        "lease_expires_at": lease_expires_at,
                        "fencing_token": task.fencing_token,
                        "generation": task.generation,
                    },
                )
                self._refresh_collection_state(conn, task.run_id)
                acquired.append(task)
        return acquired

    def mark_collection_task_running(
        self,
        task_id: int,
        *,
        lease_owner: str,
        lease_expires_at: str | None = None,
    ) -> CollectionTaskRecord:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE collection_tasks
                SET state = 'running',
                    lease_owner = ?,
                    lease_expires_at = ?,
                    fencing_token = fencing_token + 1,
                    attempts = attempts + 1,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (lease_owner, lease_expires_at, now, task_id),
            )
            task = self._get_task_in_conn(conn, task_id)
            self._append_event(
                conn,
                run_id=task.run_id,
                event_type="collection.task_running",
                payload={"task_id": task.task_id, "task_type": task.task_type},
            )
            self._refresh_collection_state(conn, task.run_id)
        return self.get_collection_task(task_id)

    def mark_collection_task_succeeded(
        self,
        task_id: int,
        *,
        payload: dict[str, Any] | None = None,
        lease_owner: str | None = None,
        fencing_token: int | None = None,
    ) -> CollectionTaskRecord:
        if fencing_token is not None and lease_owner is None:
            raise ValueError("fencing_token requires lease_owner")
        now = utc_now_iso()
        with self.connect() as conn:
            if lease_owner is None:
                result = conn.execute(
                    """
                    UPDATE collection_tasks
                    SET state = 'succeeded',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        updated_at = ?
                    WHERE task_id = ?
                    """,
                    (now, task_id),
                )
            elif fencing_token is None:
                result = conn.execute(
                    """
                    UPDATE collection_tasks
                    SET state = 'succeeded',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        updated_at = ?
                    WHERE task_id = ?
                      AND state = 'running'
                      AND lease_owner = ?
                    """,
                    (now, task_id, lease_owner),
                )
            else:
                result = conn.execute(
                    """
                    UPDATE collection_tasks
                    SET state = 'succeeded',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        updated_at = ?
                    WHERE task_id = ?
                      AND state = 'running'
                      AND lease_owner = ?
                      AND fencing_token = ?
                    """,
                    (now, task_id, lease_owner, fencing_token),
                )
            if result.rowcount != 1:
                raise CollectionTaskFenceConflict(f"collection task lease is fenced: {task_id}")
            task = self._get_task_in_conn(conn, task_id)
            self._append_event(
                conn,
                run_id=task.run_id,
                event_type="collection.task_succeeded",
                payload={
                    "task_id": task.task_id,
                    "task_type": task.task_type,
                    "fencing_token": fencing_token,
                    "generation": task.generation,
                    **(payload or {}),
                },
            )
            self._refresh_collection_state(conn, task.run_id)
        return self.get_collection_task(task_id)

    def mark_collection_task_failed(
        self,
        task_id: int,
        *,
        message: str,
        retryable: bool,
        lease_owner: str | None = None,
        error_code: str | None = None,
        auth_required: bool = False,
        retry_delay_seconds: int | None = None,
        fencing_token: int | None = None,
    ) -> CollectionTaskRecord:
        if fencing_token is not None and lease_owner is None:
            raise ValueError("fencing_token requires lease_owner")
        now = utc_now_iso()
        state = "failed_retryable" if retryable else "failed_permanent"
        next_attempt_at = None
        if retryable and retry_delay_seconds is not None and retry_delay_seconds > 0:
            next_attempt_at = (
                datetime.now(UTC) + timedelta(seconds=retry_delay_seconds)
            ).isoformat()
        with self.connect() as conn:
            if lease_owner is None:
                result = conn.execute(
                    """
                    UPDATE collection_tasks
                    SET state = ?,
                        next_attempt_at = ?,
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        updated_at = ?
                    WHERE task_id = ?
                    """,
                    (state, next_attempt_at, now, task_id),
                )
            elif fencing_token is None:
                result = conn.execute(
                    """
                    UPDATE collection_tasks
                    SET state = ?,
                        next_attempt_at = ?,
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        updated_at = ?
                    WHERE task_id = ?
                      AND state = 'running'
                      AND lease_owner = ?
                    """,
                    (state, next_attempt_at, now, task_id, lease_owner),
                )
            else:
                result = conn.execute(
                    """
                    UPDATE collection_tasks
                    SET state = ?,
                        next_attempt_at = ?,
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        updated_at = ?
                    WHERE task_id = ?
                      AND state = 'running'
                      AND lease_owner = ?
                      AND fencing_token = ?
                    """,
                    (
                        state,
                        next_attempt_at,
                        now,
                        task_id,
                        lease_owner,
                        fencing_token,
                    ),
                )
            if result.rowcount != 1:
                raise CollectionTaskFenceConflict(f"collection task lease is fenced: {task_id}")
            task = self._get_task_in_conn(conn, task_id)
            self._append_event(
                conn,
                run_id=task.run_id,
                event_type="collection.task_failed",
                payload={
                    "task_id": task.task_id,
                    "task_type": task.task_type,
                    "message": message,
                    "retryable": retryable,
                    "error_code": error_code,
                    "auth_required": auth_required,
                    "retry_delay_seconds": retry_delay_seconds,
                    "fencing_token": fencing_token,
                    "generation": task.generation,
                },
            )
            self._refresh_collection_state(conn, task.run_id)
        return self.get_collection_task(task_id)

    def get_collection_task(self, task_id: int) -> CollectionTaskRecord:
        with self.connect() as conn:
            return self._get_task_in_conn(conn, task_id)

    def _get_task_in_conn(self, conn: sqlite3.Connection, task_id: int) -> CollectionTaskRecord:
        row = conn.execute(
            "SELECT * FROM collection_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return _row_to_collection_task(row)

    def _refresh_collection_state(self, conn: sqlite3.Connection, run_id: str) -> None:
        rows = conn.execute(
            "SELECT state FROM collection_tasks WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        states = {str(row["state"]) for row in rows}
        if not states:
            collection_state = CollectionState.PENDING
        elif "failed_permanent" in states:
            collection_state = CollectionState.FAILED
        elif "failed_retryable" in states:
            collection_state = CollectionState.DEGRADED
        elif "running" in states:
            collection_state = CollectionState.RUNNING
        elif states == {"succeeded"}:
            collection_state = CollectionState.SUCCEEDED
        else:
            collection_state = CollectionState.PENDING
        conn.execute(
            "UPDATE runs SET collection_state = ?, updated_at = ? WHERE run_id = ?",
            (collection_state.value, utc_now_iso(), run_id),
        )

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        *,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {str(row["name"]) for row in rows}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _append_event(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO run_events (run_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                run_id,
                event_type,
                json.dumps(redact_sensitive_structure(payload), sort_keys=True),
                utc_now_iso(),
            ),
        )

    def _ensure_task(self, conn: sqlite3.Connection, *, run_id: str, task_type: str) -> None:
        now = utc_now_iso()
        conn.execute(
            """
            INSERT OR IGNORE INTO collection_tasks (
                run_id, task_type, state, next_attempt_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, task_type, "pending", now, now, now),
        )

    def _reactivate_succeeded_task(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        task_type: str,
        now: str,
    ) -> None:
        self._ensure_task(conn, run_id=run_id, task_type=task_type)
        conn.execute(
            """
            UPDATE collection_tasks
            SET state = 'pending', next_attempt_at = ?, generation = generation + 1,
                updated_at = ?
            WHERE run_id = ? AND task_type = ? AND state = 'succeeded'
            """,
            (now, now, run_id, task_type),
        )


def _row_to_run(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=str(row["run_id"]),
        contract_id=None if row["contract_id"] is None else str(row["contract_id"]),
        parent_run_id=(None if row["parent_run_id"] is None else str(row["parent_run_id"])),
        lineage_reason=(None if row["lineage_reason"] is None else str(row["lineage_reason"])),
        remediation_plan_id=(
            None if row["remediation_plan_id"] is None else str(row["remediation_plan_id"])
        ),
        attempt=int(row["attempt"]),
        workflow=json.loads(str(row["workflow_json"] or "{}")),
        retry_not_before=(
            None if row["retry_not_before"] is None else str(row["retry_not_before"])
        ),
        owner=str(row["owner"]),
        state=RunState(str(row["state"])),
        collection_state=CollectionState(str(row["collection_state"])),
        diagnosis_state=DiagnosisState(str(row["diagnosis_state"])),
        capsule_state=CapsuleState(str(row["capsule_state"])),
        result_status=ResultStatus(str(row["result_status"])),
        job_id=None if row["job_id"] is None else str(row["job_id"]),
        workdir=str(row["workdir"]),
        script=str(row["script"]),
        exit_code=None if row["exit_code"] is None else str(row["exit_code"]),
        terminal_state=None if row["terminal_state"] is None else str(row["terminal_state"]),
        submit_strategy=None if row["submit_strategy"] is None else str(row["submit_strategy"]),
        submit_response=json.loads(str(row["submit_response_json"] or "{}")),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        job_name=None if row["job_name"] is None else str(row["job_name"]),
        resource_plan=json.loads(str(row["resource_plan_json"] or "{}")),
    )


def _row_to_run_event(row: sqlite3.Row) -> RunEvent:
    return RunEvent(
        event_id=int(row["event_id"]),
        run_id=str(row["run_id"]),
        event_type=str(row["event_type"]),
        payload=json.loads(str(row["payload_json"])),
        created_at=str(row["created_at"]),
    )


def _require_page_limit(limit: int) -> None:
    if limit <= 0 or limit > 100:
        raise ValueError("page limit must be between 1 and 100")


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class AgentAdviceConflict(RuntimeError):
    pass


def _row_to_agent_advice(row: sqlite3.Row) -> AgentAdviceRecord:
    return AgentAdviceRecord(
        advice_id=str(row["advice_id"]),
        run_id=str(row["run_id"]),
        owner=str(row["owner"]),
        request_key=str(row["request_key"]),
        state=str(row["state"]),
        version=int(row["version"]),
        source_run_updated_at=str(row["source_run_updated_at"]),
        evidence_bundle_sha256=str(row["evidence_bundle_sha256"]),
        provider=str(row["provider"]),
        model=None if row["model"] is None else str(row["model"]),
        payload=json.loads(str(row["payload_json"])),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_agent_decision(row: sqlite3.Row) -> AgentDecisionRecord:
    return AgentDecisionRecord(
        decision_id=int(row["decision_id"]),
        advice_id=str(row["advice_id"]),
        decision=str(row["decision"]),
        actor=str(row["actor"]),
        action_ids=json.loads(str(row["action_ids_json"])),
        note=None if row["note"] is None else str(row["note"]),
        advice_version=int(row["advice_version"]),
        created_at=str(row["created_at"]),
    )


def _row_to_agent_action_execution(row: sqlite3.Row) -> AgentActionExecutionRecord:
    return AgentActionExecutionRecord(
        execution_id=str(row["execution_id"]),
        advice_id=str(row["advice_id"]),
        action_id=str(row["action_id"]),
        owner=str(row["owner"]),
        state=str(row["state"]),
        submit_requested=bool(row["submit_requested"]),
        derived_contract_id=(
            None if row["derived_contract_id"] is None else str(row["derived_contract_id"])
        ),
        run_id=None if row["run_id"] is None else str(row["run_id"]),
        error_code=None if row["error_code"] is None else str(row["error_code"]),
        error_message=(None if row["error_message"] is None else str(row["error_message"])),
        execution_phase=(None if row["execution_phase"] is None else str(row["execution_phase"])),
        execution_owner=(None if row["execution_owner"] is None else str(row["execution_owner"])),
        execution_fencing_token=int(row["execution_fencing_token"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_collection_task(row: sqlite3.Row) -> CollectionTaskRecord:
    return CollectionTaskRecord(
        task_id=int(row["task_id"]),
        run_id=str(row["run_id"]),
        task_type=str(row["task_type"]),
        state=str(row["state"]),
        next_attempt_at=None if row["next_attempt_at"] is None else str(row["next_attempt_at"]),
        lease_owner=None if row["lease_owner"] is None else str(row["lease_owner"]),
        lease_expires_at=None if row["lease_expires_at"] is None else str(row["lease_expires_at"]),
        fencing_token=int(row["fencing_token"]),
        generation=int(row["generation"]),
        attempts=int(row["attempts"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_evidence_object(row: sqlite3.Row) -> EvidenceObjectRecord:
    return EvidenceObjectRecord(
        object_id=str(row["object_id"]),
        run_id=str(row["run_id"]),
        category=str(row["category"]),
        logical_path=str(row["logical_path"]),
        store_path=str(row["store_path"]),
        source_uri=None if row["source_uri"] is None else str(row["source_uri"]),
        sha256=None if row["sha256"] is None else str(row["sha256"]),
        size_bytes=None if row["size_bytes"] is None else int(row["size_bytes"]),
        mime_type=None if row["mime_type"] is None else str(row["mime_type"]),
        collection_status=str(row["collection_status"]),
        collection_note=None if row["collection_note"] is None else str(row["collection_note"]),
        mutable_during_run=bool(row["mutable_during_run"]),
        finalized_at=None if row["finalized_at"] is None else str(row["finalized_at"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        workspace_revision=(
            None if row["workspace_revision"] is None else int(row["workspace_revision"])
        ),
        workspace_digest=(
            None if row["workspace_digest"] is None else str(row["workspace_digest"])
        ),
        source_revision=(
            None if row["source_revision"] is None else str(row["source_revision"])
        ),
        platform_snapshot_ref=(
            None
            if row["platform_snapshot_ref"] is None
            else str(row["platform_snapshot_ref"])
        ),
        integrity_checked_at=(
            None
            if row["integrity_checked_at"] is None
            else str(row["integrity_checked_at"])
        ),
    )


def _row_to_diagnosis(row: sqlite3.Row) -> DiagnosisRecord:
    return DiagnosisRecord(
        diagnosis_id=str(row["diagnosis_id"]),
        run_id=str(row["run_id"]),
        rule_id=str(row["rule_id"]),
        severity=str(row["severity"]),
        summary=str(row["summary"]),
        evidence_refs=list(json.loads(str(row["evidence_refs_json"]))),
        suggested_patch=dict(json.loads(str(row["suggested_patch_json"]))),
        retryable=bool(row["retryable"]),
        confidence=str(row["confidence"]),
        created_at=str(row["created_at"]),
        category=None if row["category"] is None else str(row["category"]),
        stage=None if row["stage"] is None else str(row["stage"]),
        fix_guide=dict(json.loads(str(row["fix_guide_json"] or "{}"))),
    )
