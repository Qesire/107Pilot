"""SQLite persistence for remediation sessions and their audit children."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pilot107.core.redaction import redact_sensitive_structure
from pilot107.core.remediation import (
    TERMINAL_REMEDIATION_STATES,
    ActionDecision,
    ActionExecution,
    ActionProposal,
    AgentTurn,
    EvaluationOutcome,
    EvaluationResult,
    RemediationBudget,
    RemediationConflict,
    RemediationEvent,
    RemediationInvariantError,
    RemediationSession,
    RemediationState,
    RemediationUsage,
    assert_remediation_transition,
)
from pilot107.core.remediation_migrations import REMEDIATION_MIGRATIONS
from pilot107.core.schema_migrations import apply_schema_migrations


def _now() -> str:
    return datetime.now(UTC).isoformat()


class RemediationStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            apply_schema_migrations(conn, REMEDIATION_MIGRATIONS)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def create_session(
        self,
        *,
        session_id: str,
        owner: str,
        request_key: str,
        state: RemediationState,
        source_run_id: str,
        source_contract_id: str | None,
        source_diagnosis_digest: str,
        source_evidence_digest: str,
        automation_policy: str,
        budget: RemediationBudget,
        provider: str = "none",
    ) -> tuple[RemediationSession, bool]:
        now = _now()
        usage = RemediationUsage()
        normalized_provider = self._normalize_provider(provider)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO remediation_sessions (
                    session_id, owner, request_key, state, version, source_run_id,
                    source_contract_id, source_diagnosis_digest, source_evidence_digest,
                    automation_policy, budget_json, usage_json, stop_reason, takeover_reason,
                    lease_owner, lease_expires_at, created_at, updated_at, provider
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?)
                """,
                (
                    session_id,
                    owner,
                    request_key,
                    state.value,
                    source_run_id,
                    source_contract_id,
                    source_diagnosis_digest,
                    source_evidence_digest,
                    automation_policy,
                    _json(budget.to_payload()),
                    _json(usage.to_payload()),
                    now,
                    now,
                    normalized_provider,
                ),
            )
            if cursor.rowcount == 1:
                self._append_event(
                    conn,
                    session_id=session_id,
                    event_type="session.created",
                    payload={
                        "state": state.value,
                        "version": 1,
                        "provider": normalized_provider,
                    },
                    created_at=now,
                )
            row = conn.execute(
                "SELECT * FROM remediation_sessions WHERE owner = ? AND request_key = ?",
                (owner, request_key),
            ).fetchone()
        if row is None:
            raise RuntimeError("failed to create remediation session")
        record = _row_to_session(row)
        if record.session_id != session_id or record.source_run_id != source_run_id:
            raise RemediationConflict("remediation request key conflicts with another session")
        return record, cursor.rowcount == 1

    def update_provider(
        self,
        session_id: str,
        *,
        provider: str,
    ) -> RemediationSession:
        """Persist the user's LLM provider choice on the session.

        Does not bump ``version`` or change ``state``: provider is a
        per-session configuration, not a state transition, so it must not
        interfere with the lease/state compare-and-swap used by ``advance``.
        """
        normalized_provider = self._normalize_provider(provider)
        now = _now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE remediation_sessions
                SET provider = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (normalized_provider, now, session_id),
            )
            if cursor.rowcount == 1:
                self._append_event(
                    conn,
                    session_id=session_id,
                    event_type="session.provider_updated",
                    payload={"provider": normalized_provider},
                    created_at=now,
                )
        if cursor.rowcount != 1:
            raise KeyError(session_id)
        return self.get_session(session_id)

    @staticmethod
    def _normalize_provider(provider: str) -> str:
        normalized = (provider or "none").strip().lower()
        if normalized not in {"none", "local", "campus"}:
            raise RemediationInvariantError(
                f"unsupported remediation provider: {provider}"
            )
        return normalized

    def get_session(self, session_id: str) -> RemediationSession:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM remediation_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return _row_to_session(row)

    def list_sessions(
        self,
        *,
        owner: str,
        states: tuple[RemediationState, ...] = (),
        limit: int = 50,
    ) -> list[RemediationSession]:
        items, _ = self.list_sessions_page(owner=owner, states=states, limit=limit)
        return items

    def list_sessions_page(
        self,
        *,
        owner: str,
        states: tuple[RemediationState, ...] = (),
        before: tuple[str, str] | None = None,
        limit: int = 50,
    ) -> tuple[list[RemediationSession], tuple[str, str] | None]:
        if limit <= 0 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        clauses = ["owner = ?"]
        values: list[Any] = [owner]
        if states:
            placeholders = ",".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            values.extend(state.value for state in states)
        if before is not None:
            updated_at, session_id = before
            clauses.append("(updated_at < ? OR (updated_at = ? AND session_id < ?))")
            values.extend((updated_at, updated_at, session_id))
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM remediation_sessions WHERE "
                + " AND ".join(clauses)
                + " ORDER BY updated_at DESC, session_id DESC LIMIT ?",
                (*values, limit + 1),
            ).fetchall()
        selected = rows[:limit]
        items = [_row_to_session(row) for row in selected]
        next_position = None
        if len(rows) > limit and selected:
            next_position = (
                str(selected[-1]["updated_at"]),
                str(selected[-1]["session_id"]),
            )
        return items, next_position

    def list_actionable_sessions(self, *, limit: int = 50) -> list[RemediationSession]:
        if limit <= 0 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        states = (
            RemediationState.WAITING_EVIDENCE,
            RemediationState.DIAGNOSING,
            RemediationState.PLANNING,
            RemediationState.EXECUTING,
            RemediationState.EVALUATING,
        )
        placeholders = ",".join("?" for _ in states)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM remediation_sessions
                WHERE state IN ({placeholders})
                ORDER BY updated_at, session_id
                LIMIT ?
                """,
                (*[state.value for state in states], limit),
            ).fetchall()
        return [_row_to_session(row) for row in rows]

    def transition(
        self,
        session_id: str,
        *,
        expected_version: int,
        expected_state: RemediationState,
        target_state: RemediationState,
        usage: RemediationUsage | None = None,
        stop_reason: str | None = None,
        takeover_reason: str | None = None,
    ) -> RemediationSession:
        assert_remediation_transition(expected_state, target_state)
        if target_state in TERMINAL_REMEDIATION_STATES and not stop_reason:
            raise ValueError("terminal remediation transition requires stop_reason")
        current = self.get_session(session_id)
        next_usage = usage or current.usage
        now = _now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE remediation_sessions
                SET state = ?, version = version + 1, usage_json = ?, stop_reason = ?,
                    takeover_reason = ?, updated_at = ?
                WHERE session_id = ? AND version = ? AND state = ?
                """,
                (
                    target_state.value,
                    _json(next_usage.to_payload()),
                    stop_reason,
                    takeover_reason,
                    now,
                    session_id,
                    expected_version,
                    expected_state.value,
                ),
            )
            if cursor.rowcount == 1:
                self._append_event(
                    conn,
                    session_id=session_id,
                    event_type="session.state_changed",
                    payload={
                        "from": expected_state.value,
                        "to": target_state.value,
                        "version": expected_version + 1,
                        "stop_reason": stop_reason,
                    },
                    created_at=now,
                )
        if cursor.rowcount != 1:
            raise RemediationConflict("remediation session version or state changed")
        return self.get_session(session_id)

    def claim_lease(
        self,
        session_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 30,
    ) -> tuple[RemediationSession, bool]:
        if not worker_id or lease_seconds <= 0:
            raise ValueError("worker_id and positive lease_seconds are required")
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        expires_at = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        terminal = tuple(state.value for state in TERMINAL_REMEDIATION_STATES)
        placeholders = ",".join("?" for _ in terminal)
        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE remediation_sessions
                SET lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE session_id = ?
                  AND state NOT IN ({placeholders})
                  AND (
                    lease_owner IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= ?
                    OR lease_owner = ?
                  )
                """,
                (worker_id, expires_at, now, session_id, *terminal, now, worker_id),
            )
        return self.get_session(session_id), cursor.rowcount == 1

    def release_lease(self, session_id: str, *, worker_id: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE remediation_sessions
                SET lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE session_id = ? AND lease_owner = ?
                """,
                (_now(), session_id, worker_id),
            )
        return cursor.rowcount == 1

    def append_turn(
        self,
        *,
        turn_id: str,
        session_id: str,
        turn_index: int,
        state: str,
        source_run_id: str,
        advice_id: str | None,
        payload: dict[str, Any],
    ) -> AgentTurn:
        now = _now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO remediation_turns (
                    turn_id, session_id, turn_index, state, source_run_id, advice_id,
                    payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    session_id,
                    turn_index,
                    state,
                    source_run_id,
                    advice_id,
                    _json(payload),
                    now,
                    now,
                ),
            )
            if cursor.rowcount == 1:
                self._append_event(
                    conn,
                    session_id=session_id,
                    event_type="turn.created",
                    payload={"turn_id": turn_id, "turn_index": turn_index, "state": state},
                    created_at=now,
                )
        turn = self.get_turn(turn_id)
        if (
            turn.session_id != session_id
            or turn.turn_index != turn_index
            or turn.source_run_id != source_run_id
            or turn.advice_id != advice_id
        ):
            raise RemediationConflict("remediation turn id conflicts with another record")
        return turn

    def get_turn(self, turn_id: str) -> AgentTurn:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM remediation_turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        if row is None:
            raise KeyError(turn_id)
        return _row_to_turn(row)

    def list_turns(self, session_id: str) -> list[AgentTurn]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM remediation_turns WHERE session_id = ? ORDER BY turn_index",
                (session_id,),
            ).fetchall()
        return [_row_to_turn(row) for row in rows]

    def append_proposal(
        self,
        *,
        proposal_id: str,
        session_id: str,
        turn_id: str,
        action_id: str,
        action_type: str,
        source: str,
        risk: str,
        approval_required: bool,
        policy_status: str,
        payload: dict[str, Any],
    ) -> ActionProposal:
        created_at = _now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO remediation_action_proposals (
                    proposal_id, session_id, turn_id, action_id, action_type, source,
                    risk, approval_required, policy_status, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    session_id,
                    turn_id,
                    action_id,
                    action_type,
                    source,
                    risk,
                    int(approval_required),
                    policy_status,
                    _json(payload),
                    created_at,
                ),
            )
            if cursor.rowcount == 1:
                self._append_event(
                    conn,
                    session_id=session_id,
                    event_type="proposal.created",
                    payload={
                        "proposal_id": proposal_id,
                        "action_type": action_type,
                        "risk": risk,
                        "policy_status": policy_status,
                    },
                    created_at=created_at,
                )
        proposal = self.get_proposal(proposal_id)
        if (
            proposal.session_id != session_id
            or proposal.turn_id != turn_id
            or proposal.action_id != action_id
        ):
            raise RemediationConflict("remediation proposal id conflicts with another record")
        return proposal

    def get_proposal(self, proposal_id: str) -> ActionProposal:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM remediation_action_proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
        if row is None:
            raise KeyError(proposal_id)
        return _row_to_proposal(row)

    def list_proposals(self, session_id: str) -> list[ActionProposal]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM remediation_action_proposals
                WHERE session_id = ? ORDER BY created_at, proposal_id
                """,
                (session_id,),
            ).fetchall()
        return [_row_to_proposal(row) for row in rows]

    def append_decision(self, decision: ActionDecision) -> ActionDecision:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO remediation_action_decisions (
                    decision_id, session_id, proposal_id, actor, decision,
                    expected_session_version, note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    decision.session_id,
                    decision.proposal_id,
                    decision.actor,
                    decision.decision,
                    decision.expected_session_version,
                    decision.note,
                    decision.created_at,
                ),
            )
            if cursor.rowcount == 1:
                self._append_event(
                    conn,
                    session_id=decision.session_id,
                    event_type=f"decision.{decision.decision}",
                    payload={
                        "decision_id": decision.decision_id,
                        "proposal_id": decision.proposal_id,
                        "actor": decision.actor,
                    },
                    created_at=decision.created_at,
                )
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM remediation_action_decisions WHERE decision_id = ?",
                (decision.decision_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("failed to append remediation decision")
        stored = _row_to_decision(row)
        if (
            stored.session_id != decision.session_id
            or stored.proposal_id != decision.proposal_id
            or stored.decision != decision.decision
        ):
            raise RemediationConflict("remediation decision id conflicts with another record")
        return stored

    def list_decisions(self, session_id: str) -> list[ActionDecision]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM remediation_action_decisions
                WHERE session_id = ? ORDER BY created_at, decision_id
                """,
                (session_id,),
            ).fetchall()
        return [_row_to_decision(row) for row in rows]

    def append_execution(self, execution: ActionExecution) -> ActionExecution:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO remediation_action_executions (
                    execution_id, session_id, proposal_id, state, derived_contract_id,
                    derived_run_id, error_code, error_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution.execution_id,
                    execution.session_id,
                    execution.proposal_id,
                    execution.state,
                    execution.derived_contract_id,
                    execution.derived_run_id,
                    execution.error_code,
                    execution.error_message,
                    execution.created_at,
                    execution.updated_at,
                ),
            )
            if cursor.rowcount == 1:
                self._append_event(
                    conn,
                    session_id=execution.session_id,
                    event_type="execution.created",
                    payload={
                        "execution_id": execution.execution_id,
                        "proposal_id": execution.proposal_id,
                        "state": execution.state,
                        "derived_run_id": execution.derived_run_id,
                    },
                    created_at=execution.created_at,
                )
        stored = self.get_execution(execution.execution_id)
        if (
            stored.session_id != execution.session_id
            or stored.proposal_id != execution.proposal_id
        ):
            raise RemediationConflict("remediation execution id conflicts with another record")
        return stored

    def get_execution(self, execution_id: str) -> ActionExecution:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM remediation_action_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        if row is None:
            raise KeyError(execution_id)
        return _row_to_execution(row)

    def update_execution(
        self,
        execution_id: str,
        *,
        state: str,
        derived_contract_id: str | None = None,
        derived_run_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> ActionExecution:
        current = self.get_execution(execution_id)
        updated_at = _now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE remediation_action_executions
                SET state = ?, derived_contract_id = ?, derived_run_id = ?,
                    error_code = ?, error_message = ?, updated_at = ?
                WHERE execution_id = ?
                """,
                (
                    state,
                    derived_contract_id or current.derived_contract_id,
                    derived_run_id or current.derived_run_id,
                    error_code,
                    error_message,
                    updated_at,
                    execution_id,
                ),
            )
            self._append_event(
                conn,
                session_id=current.session_id,
                event_type="execution.updated",
                payload={"execution_id": execution_id, "state": state},
                created_at=updated_at,
            )
        return self.get_execution(execution_id)

    def list_executions(self, session_id: str) -> list[ActionExecution]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM remediation_action_executions
                WHERE session_id = ? ORDER BY created_at, execution_id
                """,
                (session_id,),
            ).fetchall()
        return [_row_to_execution(row) for row in rows]

    def append_evaluation(self, evaluation: EvaluationResult) -> EvaluationResult:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO remediation_evaluations (
                    evaluation_id, session_id, execution_id, source_run_id, derived_run_id,
                    outcome, checks_json, comparison_json, evidence_refs_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.session_id,
                    evaluation.execution_id,
                    evaluation.source_run_id,
                    evaluation.derived_run_id,
                    evaluation.outcome.value,
                    _json(list(evaluation.checks)),
                    _json(evaluation.comparison),
                    _json(list(evaluation.evidence_refs)),
                    evaluation.created_at,
                ),
            )
            if cursor.rowcount == 1:
                self._append_event(
                    conn,
                    session_id=evaluation.session_id,
                    event_type="evaluation.created",
                    payload={
                        "evaluation_id": evaluation.evaluation_id,
                        "execution_id": evaluation.execution_id,
                        "outcome": evaluation.outcome.value,
                    },
                    created_at=evaluation.created_at,
                )
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM remediation_evaluations WHERE evaluation_id = ?",
                (evaluation.evaluation_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("failed to append remediation evaluation")
        stored = _row_to_evaluation(row)
        if (
            stored.session_id != evaluation.session_id
            or stored.execution_id != evaluation.execution_id
            or stored.derived_run_id != evaluation.derived_run_id
        ):
            raise RemediationConflict("remediation evaluation id conflicts with another record")
        return stored

    def list_evaluations(self, session_id: str) -> list[EvaluationResult]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM remediation_evaluations
                WHERE session_id = ? ORDER BY created_at, evaluation_id
                """,
                (session_id,),
            ).fetchall()
        return [_row_to_evaluation(row) for row in rows]

    def list_events_page(
        self,
        session_id: str,
        *,
        after_event_id: int = 0,
        limit: int = 100,
    ) -> tuple[list[RemediationEvent], int | None]:
        if after_event_id < 0:
            raise ValueError("after_event_id cannot be negative")
        if limit <= 0 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM remediation_session_events
                WHERE session_id = ? AND event_id > ?
                ORDER BY event_id LIMIT ?
                """,
                (session_id, after_event_id, limit + 1),
            ).fetchall()
        selected = rows[:limit]
        events = [_row_to_event(row) for row in selected]
        next_event_id = events[-1].event_id if len(rows) > limit and events else None
        return events, next_event_id

    @staticmethod
    def _append_event(
        conn: sqlite3.Connection,
        *,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        created_at: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO remediation_session_events (
                session_id, event_type, payload_json, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (session_id, event_type, _json(redact_sensitive_structure(payload)), created_at),
        )


def _row_to_session(row: sqlite3.Row) -> RemediationSession:
    budget_raw = json.loads(str(row["budget_json"]))
    usage_raw = json.loads(str(row["usage_json"]))
    if not isinstance(budget_raw, dict) or not isinstance(usage_raw, dict):
        raise ValueError("invalid remediation budget or usage payload")
    return RemediationSession(
        session_id=str(row["session_id"]),
        owner=str(row["owner"]),
        request_key=str(row["request_key"]),
        state=RemediationState(str(row["state"])),
        version=int(row["version"]),
        source_run_id=str(row["source_run_id"]),
        source_contract_id=(
            None if row["source_contract_id"] is None else str(row["source_contract_id"])
        ),
        source_diagnosis_digest=str(row["source_diagnosis_digest"]),
        source_evidence_digest=str(row["source_evidence_digest"]),
        automation_policy=str(row["automation_policy"]),
        budget=RemediationBudget.from_payload(budget_raw),
        usage=RemediationUsage(**{key: int(value) for key, value in usage_raw.items()}),
        stop_reason=None if row["stop_reason"] is None else str(row["stop_reason"]),
        takeover_reason=(
            None if row["takeover_reason"] is None else str(row["takeover_reason"])
        ),
        lease_owner=None if row["lease_owner"] is None else str(row["lease_owner"]),
        lease_expires_at=(
            None if row["lease_expires_at"] is None else str(row["lease_expires_at"])
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        # `in row` checks sqlite3.Row values, not column names; use .keys() to
        # detect the provider column added by migration 003e.003. SIM118 does
        # not apply to sqlite3.Row, so the noqa is intentional.
        provider=str(row["provider"]) if "provider" in row.keys() else "none",  # noqa: SIM118
    )


def _row_to_turn(row: sqlite3.Row) -> AgentTurn:
    return AgentTurn(
        turn_id=str(row["turn_id"]),
        session_id=str(row["session_id"]),
        turn_index=int(row["turn_index"]),
        state=str(row["state"]),
        source_run_id=str(row["source_run_id"]),
        advice_id=None if row["advice_id"] is None else str(row["advice_id"]),
        payload=_object_json(row["payload_json"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_proposal(row: sqlite3.Row) -> ActionProposal:
    return ActionProposal(
        proposal_id=str(row["proposal_id"]),
        session_id=str(row["session_id"]),
        turn_id=str(row["turn_id"]),
        action_id=str(row["action_id"]),
        action_type=str(row["action_type"]),
        source=str(row["source"]),
        risk=str(row["risk"]),
        approval_required=bool(row["approval_required"]),
        policy_status=str(row["policy_status"]),
        payload=_object_json(row["payload_json"]),
        created_at=str(row["created_at"]),
    )


def _row_to_decision(row: sqlite3.Row) -> ActionDecision:
    return ActionDecision(
        decision_id=str(row["decision_id"]),
        session_id=str(row["session_id"]),
        proposal_id=str(row["proposal_id"]),
        actor=str(row["actor"]),
        decision=str(row["decision"]),
        expected_session_version=int(row["expected_session_version"]),
        note=None if row["note"] is None else str(row["note"]),
        created_at=str(row["created_at"]),
    )


def _row_to_execution(row: sqlite3.Row) -> ActionExecution:
    return ActionExecution(
        execution_id=str(row["execution_id"]),
        session_id=str(row["session_id"]),
        proposal_id=str(row["proposal_id"]),
        state=str(row["state"]),
        derived_contract_id=(
            None if row["derived_contract_id"] is None else str(row["derived_contract_id"])
        ),
        derived_run_id=None if row["derived_run_id"] is None else str(row["derived_run_id"]),
        error_code=None if row["error_code"] is None else str(row["error_code"]),
        error_message=None if row["error_message"] is None else str(row["error_message"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_evaluation(row: sqlite3.Row) -> EvaluationResult:
    checks = json.loads(str(row["checks_json"]))
    evidence_refs = json.loads(str(row["evidence_refs_json"]))
    if not isinstance(checks, list) or not all(isinstance(item, dict) for item in checks):
        raise ValueError("stored remediation checks must be a list of objects")
    if not isinstance(evidence_refs, list) or not all(
        isinstance(item, str) for item in evidence_refs
    ):
        raise ValueError("stored remediation evidence refs must be strings")
    return EvaluationResult(
        evaluation_id=str(row["evaluation_id"]),
        session_id=str(row["session_id"]),
        execution_id=str(row["execution_id"]),
        source_run_id=str(row["source_run_id"]),
        derived_run_id=str(row["derived_run_id"]),
        outcome=EvaluationOutcome(str(row["outcome"])),
        checks=tuple(checks),
        comparison=_object_json(row["comparison_json"]),
        evidence_refs=tuple(evidence_refs),
        created_at=str(row["created_at"]),
    )


def _row_to_event(row: sqlite3.Row) -> RemediationEvent:
    return RemediationEvent(
        event_id=int(row["event_id"]),
        session_id=str(row["session_id"]),
        event_type=str(row["event_type"]),
        payload=_object_json(row["payload_json"]),
        created_at=str(row["created_at"]),
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _object_json(raw: object) -> dict[str, Any]:
    parsed = json.loads(str(raw))
    if not isinstance(parsed, dict):
        raise ValueError("stored remediation payload must be an object")
    return parsed
