from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pilot107.agent.operation_attempts import (
    AgentOperationAttemptStatus,
    SQLiteAgentOperationAttemptStore,
)
from pilot107.agent.operation_ledger import (
    AgentOperationState,
    SQLiteAgentOperationLedger,
    operation_intent_for_invocation,
)
from pilot107.agent.protocol import TOOL_INVOCATION_PROTOCOL_VERSION, ToolInvocation
from pilot107.agent.store import SQLiteAgentSessionStore


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _running_turn(database: Path, clock: MutableClock):
    store = SQLiteAgentSessionStore(database, clock=clock)
    session, _ = store.create_session(
        owner="alice",
        request_key="session-attempt",
        profile_id="experiment_builder",
        model_profile_id="faux-default",
        source={"project_id": "project-1", "workspace_id": "workspace-1"},
    )
    turn, _ = store.create_turn(
        session_id=session.session_id,
        owner="alice",
        request_key="turn-attempt",
        message="mutate once",
        expected_state_version=session.state_version,
    )
    claim = store.claim_turn(turn.turn_id, worker_id="worker-1", lease_seconds=30)
    assert claim is not None
    return store, session, turn, claim


def _invocation(session, turn, claim, call_id: str) -> ToolInvocation:
    return ToolInvocation(
        schema_version=TOOL_INVOCATION_PROTOCOL_VERSION,
        invocation_id=f"invocation-{call_id}",
        idempotency_key=f"idempotency-{call_id}",
        owner="alice",
        session_id=session.session_id,
        turn_id=turn.turn_id,
        state_version=claim.state_version,
        profile_id="experiment_builder",
        tool_name="builder_build_submit",
        arguments={
            "project_id": "project-1",
            "workspace_id": "workspace-1",
            "session_id": session.session_id,
            "turn_id": turn.turn_id,
            "request_key": "builder-attempt-1",
            "expected_project_version": 1,
            "expected_workspace_snapshot_digest": "a" * 64,
        },
        deadline="2026-09-04T10:05:00Z",
    )


def _semantic_digest(invocation: ToolInvocation) -> str:
    arguments = dict(invocation.arguments)
    arguments.pop("turn_id", None)
    encoded = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def test_attempt_records_active_turn_fence_before_running(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    clock = MutableClock()
    store, session, turn, claim = _running_turn(database, clock)
    ledger = SQLiteAgentOperationLedger(database, clock=clock)
    attempts = SQLiteAgentOperationAttemptStore(database, clock=clock)
    invocation = _invocation(session, turn, claim, "first")
    intent = operation_intent_for_invocation(
        store,
        invocation,
        arguments_digest=_semantic_digest(invocation),
    )
    assert intent is not None
    ledger.reserve(
        intent,
        invocation_id=invocation.invocation_id,
        expected_state_version=claim.state_version,
        expected_fencing_token=claim.fencing_token,
    )

    attempt = attempts.prepare(
        intent.operation_key,
        owner="alice",
        session_id=session.session_id,
        turn_id=turn.turn_id,
        state_version=claim.state_version,
        fencing_token=claim.fencing_token,
        invocation_id=invocation.invocation_id,
    )
    ledger.start(
        intent.operation_key,
        owner="alice",
        invocation_id=invocation.invocation_id,
        expected_state_version=claim.state_version,
        expected_fencing_token=claim.fencing_token,
    )

    assert attempt.active_turn_id == turn.turn_id
    assert attempt.state_version == claim.state_version
    assert attempt.fencing_token == claim.fencing_token
    assert attempts.classify(
        intent.operation_key,
        owner="alice",
        turn_id=turn.turn_id,
        state_version=claim.state_version,
        fencing_token=claim.fencing_token,
    ) is AgentOperationAttemptStatus.ACTIVE
    assert attempts.heartbeat(
        intent.operation_key,
        owner="alice",
        turn_id=turn.turn_id,
        state_version=claim.state_version,
        fencing_token=claim.fencing_token,
    )


def test_same_turn_reclaim_marks_old_running_attempt_stale(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    clock = MutableClock()
    store, session, turn, first_claim = _running_turn(database, clock)
    ledger = SQLiteAgentOperationLedger(database, clock=clock)
    attempts = SQLiteAgentOperationAttemptStore(database, clock=clock)
    invocation = _invocation(session, turn, first_claim, "first")
    intent = operation_intent_for_invocation(
        store,
        invocation,
        arguments_digest=_semantic_digest(invocation),
    )
    assert intent is not None
    ledger.reserve(
        intent,
        invocation_id=invocation.invocation_id,
        expected_state_version=first_claim.state_version,
        expected_fencing_token=first_claim.fencing_token,
    )
    attempts.prepare(
        intent.operation_key,
        owner="alice",
        session_id=session.session_id,
        turn_id=turn.turn_id,
        state_version=first_claim.state_version,
        fencing_token=first_claim.fencing_token,
        invocation_id=invocation.invocation_id,
    )
    ledger.start(
        intent.operation_key,
        owner="alice",
        invocation_id=invocation.invocation_id,
        expected_state_version=first_claim.state_version,
        expected_fencing_token=first_claim.fencing_token,
    )

    clock.advance(31)
    reclaimed = store.claim_turn(turn.turn_id, worker_id="worker-2", lease_seconds=30)
    assert reclaimed is not None
    assert reclaimed.fencing_token > first_claim.fencing_token
    assert attempts.classify(
        intent.operation_key,
        owner="alice",
        turn_id=turn.turn_id,
        state_version=reclaimed.state_version,
        fencing_token=reclaimed.fencing_token,
    ) is AgentOperationAttemptStatus.STALE

    marked = attempts.mark_stale(
        intent.operation_key,
        owner="alice",
        session_id=session.session_id,
        current_turn_id=turn.turn_id,
        current_state_version=reclaimed.state_version,
        current_fencing_token=reclaimed.fencing_token,
        invocation_id="invocation-reclaimed",
    )

    assert marked
    assert ledger.get(intent.operation_key, owner="alice").state is AgentOperationState.STALE
    assert not attempts.heartbeat(
        intent.operation_key,
        owner="alice",
        turn_id=turn.turn_id,
        state_version=first_claim.state_version,
        fencing_token=first_claim.fencing_token,
    )
