"""SQLite persistence for bounded, leased AgentTask execution."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

from pilot107.agent.migrations import (
    AGENT_TASK_EVIDENCE_GATE_MIGRATION,
    AGENT_TASK_READY_RECOVERY_MIGRATION,
    AGENT_TASK_STAGE_IDENTITY_MIGRATION,
)
from pilot107.agent.tasks import (
    TERMINAL_TASK_STATES,
    AgentResourceEnvelope,
    AgentTaskCompletionPolicy,
    AgentTaskConflict,
    AgentTaskGateReceipt,
    AgentTaskGateState,
    AgentTaskKind,
    AgentTaskLease,
    AgentTaskRecord,
    AgentTaskRequest,
    AgentTaskResult,
    AgentTaskScheduleReceipt,
    AgentTaskState,
    agent_task_gate_receipt_payload,
    agent_task_schedule_receipt_payload,
    timestamp,
)
from pilot107.core.schema_migrations import SchemaMigration, apply_schema_migrations

_GATE_STATE_RANK = {
    AgentTaskGateState.CREATED: 0,
    AgentTaskGateState.ADMITTED: 1,
    AgentTaskGateState.SUBMITTING: 2,
    AgentTaskGateState.PENDING: 3,
    AgentTaskGateState.RUNNING: 4,
    AgentTaskGateState.AWAITING_RUN_TERMINAL: 5,
    AgentTaskGateState.AWAITING_EVIDENCE: 6,
    AgentTaskGateState.AWAITING_INTEGRITY: 7,
    AgentTaskGateState.AWAITING_CAPSULE: 8,
    AgentTaskGateState.COMPLETED: 9,
    AgentTaskGateState.CANCELLING: 9,
    AgentTaskGateState.CANCELLED: 9,
    AgentTaskGateState.FAILED: 9,
    AgentTaskGateState.BLOCKED: 9,
    AgentTaskGateState.ORPHANED: 9,
}

AGENT_TASK_MIGRATIONS = (
    SchemaMigration(
        migration_id="006c.001.agent_tasks",
        statements=(
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
                version INTEGER NOT NULL,
                request_key TEXT NOT NULL,
                request_json TEXT NOT NULL,
                resource_envelope_json TEXT NOT NULL,
                envelope_expires_at TEXT NOT NULL,
                linked_run_id TEXT,
                result_json TEXT,
                cancel_requested INTEGER NOT NULL,
                lease_owner TEXT,
                lease_expires_at TEXT,
                fencing_token INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
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
            """,
            """
            CREATE INDEX idx_agent_tasks_recoverable
            ON agent_tasks(state, lease_expires_at, created_at, task_id)
            """,
            """
            CREATE INDEX idx_agent_tasks_owner_session
            ON agent_tasks(owner, session_id, created_at, task_id)
            """,
            """
            CREATE UNIQUE INDEX uq_agent_tasks_linked_run
            ON agent_tasks(linked_run_id) WHERE linked_run_id IS NOT NULL
            """,
        ),
    ),
    AGENT_TASK_EVIDENCE_GATE_MIGRATION,
    AGENT_TASK_STAGE_IDENTITY_MIGRATION,
    AGENT_TASK_READY_RECOVERY_MIGRATION,
)


class AgentTaskStore(Protocol):
    def create_task(
        self,
        *,
        owner: str,
        session_id: str,
        turn_id: str,
        project_id: str,
        workspace_id: str,
        task_kind: AgentTaskKind,
        request_key: str,
        request: AgentTaskRequest,
        envelope: AgentResourceEnvelope,
        completion_policy: AgentTaskCompletionPolicy = (
            AgentTaskCompletionPolicy.EVIDENCE_REQUIRED
        ),
    ) -> tuple[AgentTaskRecord, bool]: ...

    def get_task(self, task_id: str, *, owner: str) -> AgentTaskRecord: ...

    def list_tasks(
        self, *, owner: str, session_id: str, limit: int = 100
    ) -> list[AgentTaskRecord]: ...

    def list_recoverable_tasks(self, *, limit: int = 100) -> list[AgentTaskRecord]: ...

    def list_ready_outbox_pending(self, *, limit: int = 100) -> list[AgentTaskRecord]: ...

    def mark_ready_outbox_materialized(
        self,
        task_id: str,
        *,
        owner: str,
        expected_version: int,
    ) -> AgentTaskRecord: ...

    def claim_task(
        self,
        task_id: str,
        *,
        owner: str,
        worker_id: str,
        lease_seconds: int,
    ) -> AgentTaskLease | None: ...

    def renew_task(self, lease: AgentTaskLease, *, lease_seconds: int) -> AgentTaskLease: ...

    def release_task(self, lease: AgentTaskLease) -> AgentTaskRecord: ...

    def link_run(self, task_id: str, *, lease: AgentTaskLease, run_id: str) -> AgentTaskRecord: ...

    def complete_task(
        self, task_id: str, *, lease: AgentTaskLease, result: AgentTaskResult
    ) -> AgentTaskRecord: ...

    def advance_gate(
        self,
        task_id: str,
        *,
        lease: AgentTaskLease,
        gate_state: AgentTaskGateState,
        receipt: AgentTaskGateReceipt | AgentTaskScheduleReceipt | None = None,
        completion_policy: AgentTaskCompletionPolicy | None = None,
        causation_root_key: str | None = None,
        stage_operation_key: str | None = None,
    ) -> AgentTaskRecord: ...

    def finalize_task(
        self,
        task_id: str,
        *,
        lease: AgentTaskLease,
        gate_receipt: AgentTaskGateReceipt | None,
        result: AgentTaskResult,
        causation_root_key: str | None = None,
        stage_operation_key: str | None = None,
    ) -> AgentTaskRecord: ...

    def request_cancel(
        self, task_id: str, *, owner: str, expected_version: int
    ) -> AgentTaskRecord: ...

    def resume_after_auth(
        self, task_id: str, *, owner: str, expected_version: int
    ) -> AgentTaskRecord: ...


class SQLiteAgentTaskStore:
    _integrity_errors: tuple[type[BaseException], ...] = (sqlite3.IntegrityError,)

    def __init__(
        self,
        db_path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(UTC))
        with self.connect() as connection:
            apply_schema_migrations(connection, AGENT_TASK_MIGRATIONS)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def create_task(
        self,
        *,
        owner: str,
        session_id: str,
        turn_id: str,
        project_id: str,
        workspace_id: str,
        task_kind: AgentTaskKind,
        request_key: str,
        request: AgentTaskRequest,
        envelope: AgentResourceEnvelope,
        completion_policy: AgentTaskCompletionPolicy = (
            AgentTaskCompletionPolicy.EVIDENCE_REQUIRED
        ),
    ) -> tuple[AgentTaskRecord, bool]:
        _key(owner, "owner")
        _key(request_key, "request_key")
        with self.connect() as connection:
            replay_row = connection.execute(
                "SELECT * FROM agent_tasks WHERE owner = ? AND request_key = ?",
                (owner, request_key),
            ).fetchone()
        if replay_row is not None:
            replay = _task_from_row(replay_row)
            _assert_create_arguments(
                replay,
                session_id=session_id,
                turn_id=turn_id,
                project_id=project_id,
                workspace_id=workspace_id,
                task_kind=task_kind,
                request=request,
                envelope=envelope,
                completion_policy=completion_policy,
            )
            return replay, False
        candidate = _new_task(
            owner=owner,
            session_id=session_id,
            turn_id=turn_id,
            project_id=project_id,
            workspace_id=workspace_id,
            task_kind=task_kind,
            request_key=request_key,
            request=request,
            envelope=envelope,
            completion_policy=completion_policy,
            now=self._clock_value(),
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            inserted = connection.execute(
                """
                INSERT INTO agent_tasks (
                    task_id, owner, session_id, turn_id, project_id, workspace_id,
                    task_kind, state, version, request_key, request_json,
                    resource_envelope_json, envelope_expires_at,
                    linked_run_id, result_json,
                    cancel_requested, lease_owner, lease_expires_at, fencing_token,
                    created_at, updated_at, completion_policy, gate_state,
                    evidence_refs_json, capsule_state, legacy_gate_unverified
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, NULL, NULL,
                          0, NULL, NULL, 0, ?, ?, ?, 'created', '[]',
                          'not_required', 0)
                ON CONFLICT (owner, request_key) DO NOTHING
                """,
                _insert_values(candidate),
            )
            row = connection.execute(
                "SELECT * FROM agent_tasks WHERE owner = ? AND request_key = ?",
                (owner, request_key),
            ).fetchone()
        if row is None:
            raise RuntimeError("AgentTask insert did not produce a row")
        existing = _task_from_row(row)
        _assert_create_replay(existing, candidate)
        return existing, inserted.rowcount == 1

    def get_task(self, task_id: str, *, owner: str) -> AgentTaskRecord:
        _key(task_id, "task_id")
        _key(owner, "owner")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_tasks WHERE task_id = ? AND owner = ?",
                (task_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return _task_from_row(row)

    def list_tasks(self, *, owner: str, session_id: str, limit: int = 100) -> list[AgentTaskRecord]:
        _key(owner, "owner")
        _key(session_id, "session_id")
        _limit(limit)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_tasks WHERE owner = ? AND session_id = ? "
                "ORDER BY created_at, task_id LIMIT ?",
                (owner, session_id, limit),
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def list_recoverable_tasks(self, *, limit: int = 100) -> list[AgentTaskRecord]:
        _limit(limit)
        now = self._now()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_tasks WHERE "
                "(state = 'pending' AND envelope_expires_at > ?) OR "
                "(state = 'running' AND "
                "(lease_owner IS NULL OR lease_expires_at <= ?)) "
                "ORDER BY created_at, task_id LIMIT ?",
                (now, now, limit),
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def list_ready_outbox_pending(self, *, limit: int = 100) -> list[AgentTaskRecord]:
        _limit(limit)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_tasks WHERE ready_outbox_pending = 1 "
                "AND state IN ('succeeded', 'failed', 'cancelled', 'auth_required') "
                "ORDER BY updated_at, task_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def mark_ready_outbox_materialized(
        self,
        task_id: str,
        *,
        owner: str,
        expected_version: int,
    ) -> AgentTaskRecord:
        _key(task_id, "task_id")
        _key(owner, "owner")
        _version(expected_version)
        with self.connect() as connection:
            updated = connection.execute(
                "UPDATE agent_tasks SET ready_outbox_pending = 0 "
                "WHERE task_id = ? AND owner = ? AND version = ? "
                "AND ready_outbox_pending = 1 "
                "AND state IN ('succeeded', 'failed', 'cancelled', 'auth_required') "
                "RETURNING *",
                (task_id, owner, expected_version),
            ).fetchone()
            if updated is None:
                current = connection.execute(
                    "SELECT * FROM agent_tasks WHERE task_id = ? AND owner = ?",
                    (task_id, owner),
                ).fetchone()
                if current is None:
                    raise KeyError(task_id)
                if (
                    int(current["version"]) == expected_version
                    and int(current["ready_outbox_pending"]) == 0
                ):
                    return _task_from_row(current)
                raise AgentTaskConflict("AgentTask ready outbox materialization was fenced")
        return _task_from_row(updated)

    def claim_task(
        self,
        task_id: str,
        *,
        owner: str,
        worker_id: str,
        lease_seconds: int,
    ) -> AgentTaskLease | None:
        _key(task_id, "task_id")
        _key(owner, "owner")
        _key(worker_id, "worker_id")
        _lease_seconds(lease_seconds)
        now = self._clock_value()
        now_text = timestamp(now)
        expires_at = timestamp(now + timedelta(seconds=lease_seconds))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                UPDATE agent_tasks
                SET state = 'running', lease_owner = ?, lease_expires_at = ?,
                    fencing_token = fencing_token + 1, version = version + 1,
                    heartbeat_at = ?, updated_at = ?
                WHERE task_id = ? AND owner = ?
                  AND ((state = 'pending' AND cancel_requested = 0
                    AND envelope_expires_at > ?) OR (
                    state = 'running' AND (
                      lease_owner IS NULL OR lease_expires_at <= ?
                    )
                  ))
                RETURNING *
                """,
                (
                    worker_id,
                    expires_at,
                    now_text,
                    now_text,
                    task_id,
                    owner,
                    now_text,
                    now_text,
                ),
            ).fetchone()
            if row is None:
                exists = connection.execute(
                    "SELECT 1 FROM agent_tasks WHERE task_id = ? AND owner = ?",
                    (task_id, owner),
                ).fetchone()
                if exists is None:
                    raise KeyError(task_id)
                return None
        return AgentTaskLease(
            task_id=task_id,
            owner=owner,
            worker_id=worker_id,
            version=int(row["version"]),
            fencing_token=int(row["fencing_token"]),
            expires_at=expires_at,
        )

    def renew_task(self, lease: AgentTaskLease, *, lease_seconds: int) -> AgentTaskLease:
        _lease_seconds(lease_seconds)
        now = self._clock_value()
        expires_at = timestamp(now + timedelta(seconds=lease_seconds))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE agent_tasks SET lease_expires_at = ?, heartbeat_at = ?, updated_at = ? "
                "WHERE task_id = ? AND owner = ? AND state = 'running' "
                "AND lease_owner = ? AND lease_expires_at > ? "
                "AND fencing_token = ? AND version = ?",
                (
                    expires_at,
                    timestamp(now),
                    timestamp(now),
                    lease.task_id,
                    lease.owner,
                    lease.worker_id,
                    timestamp(now),
                    lease.fencing_token,
                    lease.version,
                ),
            )
            if updated.rowcount != 1:
                raise AgentTaskConflict("AgentTask renewal is stale or fenced")
        return replace(lease, expires_at=expires_at)

    def release_task(self, lease: AgentTaskLease) -> AgentTaskRecord:
        if not isinstance(lease, AgentTaskLease):
            raise TypeError("lease must be AgentTaskLease")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(connection, lease)
            updated = connection.execute(
                "UPDATE agent_tasks SET lease_owner = NULL, lease_expires_at = NULL, "
                "updated_at = ? WHERE task_id = ? AND owner = ? AND state = 'running' "
                "AND lease_owner = ? AND version = ? AND fencing_token = ? RETURNING *",
                (
                    self._now(),
                    lease.task_id,
                    lease.owner,
                    lease.worker_id,
                    lease.version,
                    lease.fencing_token,
                ),
            ).fetchone()
        if updated is None:
            raise AgentTaskConflict("AgentTask release was fenced")
        return _task_from_row(updated)

    def link_run(self, task_id: str, *, lease: AgentTaskLease, run_id: str) -> AgentTaskRecord:
        _key(task_id, "task_id")
        _key(run_id, "run_id")
        if task_id != lease.task_id:
            raise AgentTaskConflict("AgentTask lease binding is invalid")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._assert_lease(connection, lease)
            existing_run = row["linked_run_id"]
            if existing_run is not None:
                if str(existing_run) != run_id:
                    raise AgentTaskConflict("AgentTask already links another Run")
                return _task_from_row(row)
            try:
                updated = connection.execute(
                    "UPDATE agent_tasks SET linked_run_id = ?, updated_at = ? "
                    "WHERE task_id = ? AND owner = ? AND version = ? "
                    "AND fencing_token = ? AND linked_run_id IS NULL RETURNING *",
                    (
                        run_id,
                        self._now(),
                        task_id,
                        lease.owner,
                        lease.version,
                        lease.fencing_token,
                    ),
                ).fetchone()
            except self._integrity_errors as exc:
                raise AgentTaskConflict("Run is already linked to another AgentTask") from exc
        if updated is None:
            raise AgentTaskConflict("AgentTask Run link was fenced")
        return _task_from_row(updated)

    def complete_task(
        self, task_id: str, *, lease: AgentTaskLease, result: AgentTaskResult
    ) -> AgentTaskRecord:
        _key(task_id, "task_id")
        if task_id != lease.task_id:
            raise AgentTaskConflict("AgentTask lease binding is invalid")
        if not isinstance(result, AgentTaskResult):
            raise TypeError("result must be AgentTaskResult")
        target = AgentTaskState(result.status)
        if result.status == "succeeded":
            current = self.get_task(task_id, owner=lease.owner)
            if current.gate_receipt is None:
                raise AgentTaskConflict("successful AgentTask completion requires a gate receipt")
            return self.finalize_task(
                task_id,
                lease=lease,
                gate_receipt=current.gate_receipt,
                result=result,
            )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM agent_tasks WHERE task_id = ? AND owner = ?",
                (task_id, lease.owner),
            ).fetchone()
            if existing is None:
                raise KeyError(task_id)
            current = _task_from_row(existing)
            if (
                current.state in TERMINAL_TASK_STATES
                or current.state is AgentTaskState.AUTH_REQUIRED
            ):
                if current.result == result and current.fencing_token == lease.fencing_token:
                    return current
                raise AgentTaskConflict("AgentTask terminal result replay conflicts")
            self._assert_lease(connection, lease)
            updated = connection.execute(
                "UPDATE agent_tasks SET state = ?, result_json = ?, version = version + 1, "
                "ready_outbox_pending = 1, lease_owner = NULL, lease_expires_at = NULL, "
                "updated_at = ? "
                "WHERE task_id = ? AND owner = ? AND state = 'running' "
                "AND version = ? AND fencing_token = ? RETURNING *",
                (
                    target.value,
                    _json(_result_payload(result)),
                    self._now(),
                    task_id,
                    lease.owner,
                    lease.version,
                    lease.fencing_token,
                ),
            ).fetchone()
        if updated is None:
            raise AgentTaskConflict("AgentTask completion was fenced")
        return _task_from_row(updated)

    def advance_gate(
        self,
        task_id: str,
        *,
        lease: AgentTaskLease,
        gate_state: AgentTaskGateState,
        receipt: AgentTaskGateReceipt | AgentTaskScheduleReceipt | None = None,
        completion_policy: AgentTaskCompletionPolicy | None = None,
        causation_root_key: str | None = None,
        stage_operation_key: str | None = None,
    ) -> AgentTaskRecord:
        """Advance the durable gate projection under the current task lease."""

        _key(task_id, "task_id")
        if task_id != lease.task_id:
            raise AgentTaskConflict("AgentTask lease binding is invalid")
        if not isinstance(gate_state, AgentTaskGateState):
            try:
                gate_state = AgentTaskGateState(gate_state)
            except (TypeError, ValueError) as exc:
                raise ValueError("gate_state is invalid") from exc
        if gate_state is AgentTaskGateState.COMPLETED:
            raise AgentTaskConflict("completed gate state is written only by finalize_task")
        if gate_state is AgentTaskGateState.INPUT_REQUIRED:
            raise AgentTaskConflict("INPUT_REQUIRED is not a progress gate state")
        if receipt is not None and not isinstance(
            receipt, (AgentTaskGateReceipt, AgentTaskScheduleReceipt)
        ):
            raise TypeError("receipt must be an AgentTask gate receipt")
        if completion_policy is not None and not isinstance(
            completion_policy, AgentTaskCompletionPolicy
        ):
            try:
                completion_policy = AgentTaskCompletionPolicy(completion_policy)
            except (TypeError, ValueError) as exc:
                raise ValueError("completion_policy is invalid") from exc
        receipt_payload = None
        schedule_ref = None
        evidence_refs = None
        evidence_digest = None
        integrity_checked_at = None
        capsule_ref = None
        capsule_state = "not_required"
        if isinstance(receipt, AgentTaskScheduleReceipt):
            receipt_payload = _json(agent_task_schedule_receipt_payload(receipt))
            schedule_ref = receipt.receipt_id
        elif isinstance(receipt, AgentTaskGateReceipt):
            receipt_payload = _json(agent_task_gate_receipt_payload(receipt))
            evidence_refs = _json_array(receipt.evidence_refs)
            evidence_digest = receipt.evidence_digest
            integrity_checked_at = receipt.integrity_verified_at
            capsule_ref = receipt.capsule_ref
            capsule_state = receipt.capsule_state
        # Reject malformed or terminal candidates before touching the database.
        if receipt is not None and not receipt_payload:
            raise ValueError("gate receipt candidate is invalid")
        stage_operation_key = _stage_key(stage_operation_key, receipt_payload)
        causation_root_key = _root_key(causation_root_key, task_id)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_row = self._assert_lease(connection, lease)
            current = _task_from_row(current_row)
            target_policy = completion_policy or current.completion_policy
            if current.gate_state is AgentTaskGateState.INPUT_REQUIRED:
                raise AgentTaskConflict("INPUT_REQUIRED gate must resume before progress")
            stored_root = (
                current_row["causation_root_key"]
                if _row_has(current_row, "causation_root_key")
                else None
            )
            if stored_root is not None and stored_root != causation_root_key:
                raise AgentTaskConflict("receipt identity conflicts with causation root")
            if (
                current.schedule_receipt is not None
                and current.schedule_receipt.completion_policy is not target_policy
            ):
                raise AgentTaskConflict("schedule receipt policy is immutable")
            if current.gate_receipt is not None and current.completion_policy is not target_policy:
                raise AgentTaskConflict("gate receipt policy is immutable")
            if _GATE_STATE_RANK[gate_state] < _GATE_STATE_RANK[current.gate_state]:
                raise AgentTaskConflict("gate state transition is not monotonic")
            if receipt is not None:
                _validate_receipt_identity(current, receipt, target_policy, gate_state)
                if (
                    isinstance(receipt, AgentTaskScheduleReceipt)
                    and current.schedule_receipt is not None
                ):
                    if _stored_stage_key(current_row, "schedule") != stage_operation_key:
                        raise AgentTaskConflict("schedule stage identity conflicts")
                    if current.schedule_receipt != receipt:
                        raise AgentTaskConflict("immutable schedule receipt conflicts")
                    if gate_state is current.gate_state:
                        return current
                if isinstance(receipt, AgentTaskGateReceipt) and current.gate_receipt is not None:
                    if _stored_stage_key(current_row, "gate") != stage_operation_key:
                        raise AgentTaskConflict("gate stage identity conflicts")
                    if current.gate_receipt == receipt and gate_state is current.gate_state:
                        return current
                    if current.gate_receipt != receipt:
                        raise AgentTaskConflict("immutable gate receipt conflicts")
            candidate_schedule = (
                receipt
                if isinstance(receipt, AgentTaskScheduleReceipt)
                else current.schedule_receipt
            )
            candidate_gate = (
                receipt if isinstance(receipt, AgentTaskGateReceipt) else current.gate_receipt
            )
            replace(
                current,
                completion_policy=target_policy,
                gate_state=gate_state,
                schedule_receipt=candidate_schedule,
                gate_receipt=candidate_gate,
            )
            updated = connection.execute(
                "UPDATE agent_tasks SET completion_policy = ?, gate_state = ?, "
                "schedule_receipt_ref = COALESCE(?, schedule_receipt_ref), "
                "schedule_receipt = COALESCE(?, schedule_receipt), "
                "durable_operation_key = COALESCE(?, durable_operation_key), "
                "causation_root_key = COALESCE(?, causation_root_key), "
                "schedule_operation_key = CASE WHEN ? IS NOT NULL THEN ? "
                "ELSE schedule_operation_key END, "
                "gate_operation_key = CASE WHEN ? IS NOT NULL THEN ? "
                "ELSE gate_operation_key END, "
                "evidence_refs_json = COALESCE(?, evidence_refs_json), "
                "evidence_digest = COALESCE(?, evidence_digest), "
                "integrity_checked_at = COALESCE(?, integrity_checked_at), "
                "capsule_ref = COALESCE(?, capsule_ref), "
                "capsule_state = COALESCE(?, capsule_state), "
                "gate_receipt = CASE WHEN ? IS NOT NULL THEN ? ELSE gate_receipt END, "
                "legacy_gate_unverified = CASE WHEN ? IS NOT NULL THEN 0 "
                "ELSE legacy_gate_unverified END, "
                "heartbeat_at = ?, version = version + 1, updated_at = ? "
                "WHERE task_id = ? AND owner = ? AND state = 'running' "
                "AND version = ? AND fencing_token = ? RETURNING *",
                (
                    target_policy.value,
                    gate_state.value,
                    schedule_ref,
                    receipt_payload if isinstance(receipt, AgentTaskScheduleReceipt) else None,
                    stage_operation_key if receipt is not None else None,
                    causation_root_key,
                    stage_operation_key if isinstance(receipt, AgentTaskScheduleReceipt) else None,
                    stage_operation_key if isinstance(receipt, AgentTaskScheduleReceipt) else None,
                    stage_operation_key if isinstance(receipt, AgentTaskGateReceipt) else None,
                    stage_operation_key if isinstance(receipt, AgentTaskGateReceipt) else None,
                    evidence_refs,
                    evidence_digest,
                    integrity_checked_at,
                    capsule_ref,
                    capsule_state,
                    receipt_payload if isinstance(receipt, AgentTaskGateReceipt) else None,
                    receipt_payload if isinstance(receipt, AgentTaskGateReceipt) else None,
                    receipt_payload if isinstance(receipt, AgentTaskGateReceipt) else None,
                    self._now(),
                    self._now(),
                    task_id,
                    lease.owner,
                    lease.version,
                    lease.fencing_token,
                ),
            ).fetchone()
        if updated is None:
            raise AgentTaskConflict("AgentTask gate transition was fenced")
        return _task_from_row(updated)

    def finalize_task(
        self,
        task_id: str,
        *,
        lease: AgentTaskLease,
        gate_receipt: AgentTaskGateReceipt | None,
        result: AgentTaskResult,
        causation_root_key: str | None = None,
        stage_operation_key: str | None = None,
    ) -> AgentTaskRecord:
        """Finalize a task only after an authoritative, policy-compatible gate."""

        _key(task_id, "task_id")
        if task_id != lease.task_id:
            raise AgentTaskConflict("AgentTask lease binding is invalid")
        if not isinstance(result, AgentTaskResult):
            raise TypeError("result must be AgentTaskResult")
        if gate_receipt is not None and not isinstance(gate_receipt, AgentTaskGateReceipt):
            raise TypeError("gate_receipt must be AgentTaskGateReceipt")
        gate_payload = (
            _json(agent_task_gate_receipt_payload(gate_receipt))
            if gate_receipt is not None
            else None
        )
        stage_operation_key = _stage_key(stage_operation_key, gate_payload)
        causation_root_key = _root_key(causation_root_key, task_id)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM agent_tasks WHERE task_id = ? AND owner = ?",
                (task_id, lease.owner),
            ).fetchone()
            if existing is None:
                raise KeyError(task_id)
            current = _task_from_row(existing)
            if current.state in TERMINAL_TASK_STATES:
                if (
                    gate_receipt is not None
                    and current.result == result
                    and current.gate_receipt == gate_receipt
                    and (
                        not _row_has(existing, "causation_root_key")
                        or existing["causation_root_key"] == causation_root_key
                    )
                    and _stored_stage_key(existing, "gate") == stage_operation_key
                ):
                    return current
                raise AgentTaskConflict("terminal AgentTask replay has no matching identity")
            self._assert_lease(connection, lease)
            stored_root = (
                existing["causation_root_key"] if _row_has(existing, "causation_root_key") else None
            )
            if stored_root is not None and stored_root != causation_root_key:
                raise AgentTaskConflict("gate identity conflicts with causation root")
            stored_gate_key = _stored_stage_key(existing, "gate")
            if stored_gate_key is not None and stored_gate_key != stage_operation_key:
                raise AgentTaskConflict("gate stage identity conflicts")
            if result.status == "succeeded" and gate_receipt is None:
                raise AgentTaskConflict("successful AgentTask finalization requires a gate receipt")
            receipt_payload = None
            evidence_refs = _json_array(result.evidence_refs)
            evidence_digest = None
            integrity_checked_at = None
            capsule_ref = None
            capsule_state = "not_required"
            if gate_receipt is not None:
                if gate_receipt.task_id != current.task_id:
                    raise AgentTaskConflict("AgentTask gate receipt identity is invalid")
                if current.linked_run_id is None or gate_receipt.run_id != current.linked_run_id:
                    raise AgentTaskConflict("AgentTask gate receipt identity is invalid")
                if current.gate_receipt is not None and current.gate_receipt != gate_receipt:
                    raise AgentTaskConflict("immutable gate receipt conflicts")
                if tuple(result.evidence_refs) != tuple(gate_receipt.evidence_refs):
                    raise AgentTaskConflict("Evidence references do not match gate receipt")
                if result.status == "succeeded" and gate_receipt.run_terminal_state != "completed":
                    raise AgentTaskConflict("successful AgentTask requires a completed Run")
                if current.completion_policy.requires_capsule and (
                    gate_receipt.capsule_state != "READY" or gate_receipt.capsule_ref is None
                ):
                    raise AgentTaskConflict("Capsule-required AgentTask needs a READY Capsule")
                receipt_payload = gate_payload
                evidence_refs = _json_array(gate_receipt.evidence_refs)
                evidence_digest = gate_receipt.evidence_digest
                integrity_checked_at = gate_receipt.integrity_verified_at
                capsule_ref = gate_receipt.capsule_ref
                capsule_state = gate_receipt.capsule_state
            target = AgentTaskState(result.status)
            updated = connection.execute(
                "UPDATE agent_tasks SET state = ?, result_json = ?, gate_state = ?, "
                "gate_receipt = COALESCE(?, gate_receipt), evidence_refs_json = ?, "
                "durable_operation_key = COALESCE(?, durable_operation_key), "
                "causation_root_key = COALESCE(?, causation_root_key), "
                "gate_operation_key = COALESCE(?, gate_operation_key), "
                "evidence_digest = COALESCE(?, evidence_digest), "
                "integrity_checked_at = COALESCE(?, integrity_checked_at), "
                "capsule_ref = COALESCE(?, capsule_ref), capsule_state = ?, "
                "legacy_gate_unverified = CASE WHEN ? IS NOT NULL THEN 0 "
                "ELSE legacy_gate_unverified END, "
                "version = version + 1, ready_outbox_pending = 1, "
                "lease_owner = NULL, lease_expires_at = NULL, "
                "heartbeat_at = ?, updated_at = ? WHERE task_id = ? AND owner = ? "
                "AND state = 'running' AND version = ? AND fencing_token = ? RETURNING *",
                (
                    target.value,
                    _json(_result_payload(result)),
                    (
                        AgentTaskGateState.COMPLETED.value
                        if result.status == "succeeded"
                        else gate_state_for_result(target).value
                    ),
                    receipt_payload,
                    evidence_refs,
                    stage_operation_key if gate_receipt is not None else None,
                    causation_root_key,
                    stage_operation_key if gate_receipt is not None else None,
                    evidence_digest,
                    integrity_checked_at,
                    capsule_ref,
                    capsule_state,
                    receipt_payload,
                    self._now(),
                    self._now(),
                    task_id,
                    lease.owner,
                    lease.version,
                    lease.fencing_token,
                ),
            ).fetchone()
        if updated is None:
            raise AgentTaskConflict("AgentTask finalization was fenced")
        return _task_from_row(updated)

    def request_cancel(self, task_id: str, *, owner: str, expected_version: int) -> AgentTaskRecord:
        _key(task_id, "task_id")
        _key(owner, "owner")
        _version(expected_version)
        now = self._now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM agent_tasks WHERE task_id = ? AND owner = ?",
                (task_id, owner),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            current = _task_from_row(row)
            if current.state in TERMINAL_TASK_STATES:
                if current.state is AgentTaskState.CANCELLED:
                    return current
                raise AgentTaskConflict("terminal AgentTask cannot be cancelled")
            if current.cancel_requested:
                return current
            if current.version != expected_version:
                raise AgentTaskConflict("AgentTask cancellation version is stale")
            if current.state in {
                AgentTaskState.PENDING,
                AgentTaskState.AUTH_REQUIRED,
            }:
                result = AgentTaskResult.cancelled("cancelled before execution")
                updated = connection.execute(
                    "UPDATE agent_tasks SET state = 'cancelled', cancel_requested = 1, "
                    "result_json = ?, version = version + 1, ready_outbox_pending = 1, "
                    "lease_owner = NULL, "
                    "lease_expires_at = NULL, updated_at = ? WHERE task_id = ? "
                    "AND owner = ? AND version = ? RETURNING *",
                    (
                        _json(_result_payload(result)),
                        now,
                        task_id,
                        owner,
                        expected_version,
                    ),
                ).fetchone()
            else:
                updated = connection.execute(
                    "UPDATE agent_tasks SET cancel_requested = 1, updated_at = ? "
                    "WHERE task_id = ? AND owner = ? AND state = 'running' "
                    "AND version = ? RETURNING *",
                    (now, task_id, owner, expected_version),
                ).fetchone()
        if updated is None:
            raise AgentTaskConflict("AgentTask cancellation was fenced")
        return _task_from_row(updated)

    def resume_after_auth(
        self, task_id: str, *, owner: str, expected_version: int
    ) -> AgentTaskRecord:
        _key(task_id, "task_id")
        _key(owner, "owner")
        _version(expected_version)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE agent_tasks SET state = 'pending', result_json = NULL, "
                "ready_outbox_pending = 0, version = version + 1, updated_at = ? "
                "WHERE task_id = ? AND owner = ? "
                "AND state = 'auth_required' AND cancel_requested = 0 "
                "AND version = ? RETURNING *",
                (self._now(), task_id, owner, expected_version),
            ).fetchone()
            if updated is None:
                exists = connection.execute(
                    "SELECT 1 FROM agent_tasks WHERE task_id = ? AND owner = ?",
                    (task_id, owner),
                ).fetchone()
                if exists is None:
                    raise KeyError(task_id)
                raise AgentTaskConflict("AgentTask authentication resume is invalid")
        return _task_from_row(updated)

    def _assert_lease(self, connection: sqlite3.Connection, lease: AgentTaskLease) -> Any:
        row = connection.execute(
            "SELECT * FROM agent_tasks WHERE task_id = ? AND owner = ? "
            "AND state = 'running' AND lease_owner = ? AND lease_expires_at > ? "
            "AND version = ? AND fencing_token = ?",
            (
                lease.task_id,
                lease.owner,
                lease.worker_id,
                self._now(),
                lease.version,
                lease.fencing_token,
            ),
        ).fetchone()
        if row is None:
            raise AgentTaskConflict("AgentTask lease is stale or fenced")
        return row

    def _clock_value(self) -> datetime:
        value = self._clock()
        timestamp(value)
        return value.astimezone(UTC)

    def _now(self) -> str:
        return timestamp(self._clock_value())


def _new_task(
    *,
    owner: str,
    session_id: str,
    turn_id: str,
    project_id: str,
    workspace_id: str,
    task_kind: AgentTaskKind,
    request_key: str,
    request: AgentTaskRequest,
    envelope: AgentResourceEnvelope,
    completion_policy: AgentTaskCompletionPolicy,
    now: datetime,
) -> AgentTaskRecord:
    for value, label in (
        (owner, "owner"),
        (session_id, "session_id"),
        (turn_id, "turn_id"),
        (project_id, "project_id"),
        (workspace_id, "workspace_id"),
        (request_key, "request_key"),
    ):
        _key(value, label)
    if task_kind != "slurm_validation":
        raise ValueError("AgentTask kind is invalid")
    if not isinstance(request, AgentTaskRequest):
        raise TypeError("request must be AgentTaskRequest")
    if not isinstance(envelope, AgentResourceEnvelope):
        raise TypeError("envelope must be AgentResourceEnvelope")
    if not isinstance(completion_policy, AgentTaskCompletionPolicy):
        try:
            completion_policy = AgentTaskCompletionPolicy(completion_policy)
        except (TypeError, ValueError) as exc:
            raise ValueError("completion_policy is invalid") from exc
    envelope.assert_allows(request, owner=owner, now=now)
    now_text = timestamp(now)
    digest = hashlib.sha256(f"{owner}\0{request_key}".encode()).hexdigest()
    return AgentTaskRecord(
        task_id=f"task-{digest}",
        owner=owner,
        session_id=session_id,
        turn_id=turn_id,
        project_id=project_id,
        workspace_id=workspace_id,
        task_kind=task_kind,
        state=AgentTaskState.PENDING,
        version=0,
        request_key=request_key,
        request=request,
        resource_envelope=envelope,
        linked_run_id=None,
        result=None,
        cancel_requested=False,
        lease_owner=None,
        lease_expires_at=None,
        fencing_token=0,
        created_at=now_text,
        updated_at=now_text,
        completion_policy=completion_policy,
    )


def _insert_values(value: AgentTaskRecord) -> tuple[object, ...]:
    return (
        value.task_id,
        value.owner,
        value.session_id,
        value.turn_id,
        value.project_id,
        value.workspace_id,
        value.task_kind,
        value.request_key,
        _json(_request_payload(value.request)),
        _json(_envelope_payload(value.resource_envelope)),
        value.resource_envelope.expires_at,
        value.created_at,
        value.updated_at,
        value.completion_policy.value,
    )


def _assert_create_replay(existing: AgentTaskRecord, candidate: AgentTaskRecord) -> None:
    immutable = (
        "owner",
        "session_id",
        "turn_id",
        "project_id",
        "workspace_id",
        "task_kind",
        "request_key",
        "request",
        "resource_envelope",
        "completion_policy",
    )
    if any(getattr(existing, name) != getattr(candidate, name) for name in immutable):
        raise AgentTaskConflict("AgentTask request-key replay conflicts")


def _assert_create_arguments(
    existing: AgentTaskRecord,
    *,
    session_id: str,
    turn_id: str,
    project_id: str,
    workspace_id: str,
    task_kind: AgentTaskKind,
    request: AgentTaskRequest,
    envelope: AgentResourceEnvelope,
    completion_policy: AgentTaskCompletionPolicy,
) -> None:
    expected = (
        (existing.session_id, session_id),
        (existing.turn_id, turn_id),
        (existing.project_id, project_id),
        (existing.workspace_id, workspace_id),
        (existing.task_kind, task_kind),
        (existing.request, request),
        (existing.resource_envelope, envelope),
        (existing.completion_policy, completion_policy),
    )
    if any(actual != candidate for actual, candidate in expected):
        raise AgentTaskConflict("AgentTask request-key replay conflicts")


def _task_from_row(row: Mapping[str, Any] | Any) -> AgentTaskRecord:
    raw_result = row["result_json"]
    result_value = _loaded(raw_result) if raw_result is not None else None
    state = AgentTaskState(str(row["state"]))
    critical_gate_columns = ("completion_policy", "gate_state", "legacy_gate_unverified")
    legacy_gate_unverified = (
        True
        if any(not _row_has(row, column) or row[column] is None for column in critical_gate_columns)
        else bool(row["legacy_gate_unverified"])
    )
    raw_policy = row["completion_policy"] if _row_has(row, "completion_policy") else None
    raw_gate_state = row["gate_state"] if _row_has(row, "gate_state") else None
    raw_schedule = row["schedule_receipt"] if _row_has(row, "schedule_receipt") else None
    raw_gate = row["gate_receipt"] if _row_has(row, "gate_receipt") else None
    return AgentTaskRecord(
        task_id=str(row["task_id"]),
        owner=str(row["owner"]),
        session_id=str(row["session_id"]),
        turn_id=str(row["turn_id"]),
        project_id=str(row["project_id"]),
        workspace_id=str(row["workspace_id"]),
        task_kind=cast(AgentTaskKind, str(row["task_kind"])),
        state=state,
        version=int(row["version"]),
        request_key=str(row["request_key"]),
        request=_request_from_payload(_loaded(row["request_json"])),
        resource_envelope=_envelope_from_payload(_loaded(row["resource_envelope_json"])),
        linked_run_id=(str(row["linked_run_id"]) if row["linked_run_id"] is not None else None),
        result=_result_from_payload(result_value) if result_value is not None else None,
        cancel_requested=bool(row["cancel_requested"]),
        lease_owner=(str(row["lease_owner"]) if row["lease_owner"] is not None else None),
        lease_expires_at=(
            str(row["lease_expires_at"]) if row["lease_expires_at"] is not None else None
        ),
        fencing_token=int(row["fencing_token"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        completion_policy=(
            AgentTaskCompletionPolicy(str(raw_policy))
            if raw_policy is not None
            else AgentTaskCompletionPolicy.EVIDENCE_REQUIRED
        ),
        gate_state=(
            AgentTaskGateState(str(raw_gate_state))
            if raw_gate_state is not None
            else AgentTaskGateState.CREATED
        ),
        schedule_receipt=(
            AgentTaskScheduleReceipt(**_loaded(raw_schedule)) if raw_schedule is not None else None
        ),
        gate_receipt=(AgentTaskGateReceipt(**_loaded(raw_gate)) if raw_gate is not None else None),
        legacy_gate_unverified=legacy_gate_unverified,
    )


def _row_has(row: Mapping[str, Any] | Any, key: str) -> bool:
    keys = getattr(row, "keys", None)
    return key in keys() if callable(keys) else False


def _request_payload(value: AgentTaskRequest) -> dict[str, Any]:
    return {
        "partition": value.partition,
        "qos": value.qos,
        "cpus": value.cpus,
        "memory_mib": value.memory_mib,
        "gpu_type": value.gpu_type,
        "gpus": value.gpus,
        "walltime_seconds": value.walltime_seconds,
        "tasks": value.tasks,
        "submissions": value.submissions,
        "workspace_snapshot_digest": value.workspace_snapshot_digest,
        "payload": value.payload,
    }


def _request_from_payload(value: dict[str, Any]) -> AgentTaskRequest:
    return AgentTaskRequest(**value)


def _envelope_payload(value: AgentResourceEnvelope) -> dict[str, Any]:
    return {
        "partition": value.partition,
        "qos": value.qos,
        "cpus": value.cpus,
        "memory_mib": value.memory_mib,
        "gpu_type": value.gpu_type,
        "gpus": value.gpus,
        "walltime_seconds": value.walltime_seconds,
        "max_tasks": value.max_tasks,
        "max_submissions": value.max_submissions,
        "workspace_snapshot_digest": value.workspace_snapshot_digest,
        "expires_at": value.expires_at,
        "approved_by": value.approved_by,
    }


def _envelope_from_payload(value: dict[str, Any]) -> AgentResourceEnvelope:
    return AgentResourceEnvelope(**value)


def _result_payload(value: AgentTaskResult) -> dict[str, Any]:
    return {
        "status": value.status,
        "evidence_refs": list(value.evidence_refs),
        "error_code": value.error_code,
        "message": value.message,
    }


def _result_from_payload(value: dict[str, Any]) -> AgentTaskResult:
    return AgentTaskResult(
        status=cast(Any, value["status"]),
        evidence_refs=tuple(cast(list[str], value["evidence_refs"])),
        error_code=cast(str | None, value["error_code"]),
        message=cast(str | None, value["message"]),
    )


def gate_state_for_result(result: AgentTaskState) -> AgentTaskGateState:
    if result is AgentTaskState.CANCELLED:
        return AgentTaskGateState.CANCELLED
    if result is AgentTaskState.AUTH_REQUIRED:
        return AgentTaskGateState.INPUT_REQUIRED
    if result is AgentTaskState.FAILED:
        return AgentTaskGateState.FAILED
    return AgentTaskGateState.BLOCKED


def _receipt_identity(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _root_key(value: str | None, task_id: str) -> str:
    if value is None:
        return _receipt_identity(f"causation\0{task_id}")
    _key(value, "causation_root_key")
    return value


def _stage_key(value: str | None, payload: str | None) -> str | None:
    if value is None:
        return _receipt_identity(payload) if payload is not None else None
    _key(value, "stage_operation_key")
    return value


def _stored_stage_key(row: Mapping[str, Any] | Any, stage: str) -> str | None:
    column = f"{stage}_operation_key"
    if _row_has(row, column):
        return str(row[column]) if row[column] is not None else None
    if _row_has(row, "durable_operation_key") and row["durable_operation_key"] is not None:
        return str(row["durable_operation_key"])
    return None


def _validate_receipt_identity(
    current: AgentTaskRecord,
    receipt: AgentTaskGateReceipt | AgentTaskScheduleReceipt,
    policy: AgentTaskCompletionPolicy,
    gate_state: AgentTaskGateState,
) -> None:
    if isinstance(receipt, AgentTaskScheduleReceipt):
        required = (
            receipt.owner,
            receipt.session_id,
            receipt.originating_turn_id,
            receipt.request_digest,
            receipt.idempotency_key,
            receipt.resource_envelope_id,
            receipt.workspace_digest,
            receipt.created_at,
        )
        if any(value is None for value in required):
            raise AgentTaskConflict("schedule receipt identity is incomplete")
        expected_request = _receipt_identity(_json(_request_payload(current.request)))
        expected_envelope = _receipt_identity(_json(_envelope_payload(current.resource_envelope)))
        expected_state = {
            "admitted": AgentTaskGateState.ADMITTED,
            "submitting": AgentTaskGateState.SUBMITTING,
            "pending": AgentTaskGateState.PENDING,
            "submitted": AgentTaskGateState.PENDING,
            "submission_uncertain": AgentTaskGateState.PENDING,
        }[receipt.submit_state]
        if (
            receipt.task_id != current.task_id
            or receipt.owner != current.owner
            or receipt.session_id != current.session_id
            or receipt.originating_turn_id != current.turn_id
            or receipt.completion_policy is not policy
            or receipt.request_digest != expected_request
            or receipt.idempotency_key != current.request_key
            or receipt.resource_envelope_id != expected_envelope
            or receipt.workspace_digest != current.request.workspace_snapshot_digest
            or receipt.workspace_revision is not None
            or receipt.legacy_boundary is not True
            or gate_state is not expected_state
        ):
            raise AgentTaskConflict("schedule receipt identity does not match AgentTask")
        if current.linked_run_id is not None and receipt.run_id != current.linked_run_id:
            raise AgentTaskConflict("schedule receipt identity does not match AgentTask Run")
        return

    if receipt.task_id != current.task_id:
        raise AgentTaskConflict("gate receipt identity does not match AgentTask")
    if current.linked_run_id is None or receipt.run_id != current.linked_run_id:
        raise AgentTaskConflict("gate receipt identity does not match AgentTask Run")
    if gate_state not in {
        AgentTaskGateState.AWAITING_INTEGRITY,
        AgentTaskGateState.AWAITING_CAPSULE,
    }:
        raise AgentTaskConflict("gate receipt does not match gate state")
    if policy.requires_capsule:
        if receipt.capsule_state != "READY" or receipt.capsule_ref is None:
            raise AgentTaskConflict("Capsule gate receipt is not policy-compatible")
    elif gate_state is AgentTaskGateState.AWAITING_CAPSULE:
        raise AgentTaskConflict("Capsule gate state is not policy-compatible")


def _loaded(value: object) -> dict[str, Any]:
    loaded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(loaded, dict):
        raise ValueError("AgentTask JSON payload is invalid")
    return cast(dict[str, Any], loaded)


def _json(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _json_array(values: tuple[str, ...]) -> str:
    return json.dumps(
        list(values),
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _key(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError(f"{label} is invalid")


def _version(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected_version is invalid")


def _limit(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
        raise ValueError("limit is invalid")


def _lease_seconds(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3600:
        raise ValueError("lease_seconds is invalid")
