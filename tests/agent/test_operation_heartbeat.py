from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pilot107.agent.capabilities import AgentCapabilityClaims, AgentCapabilitySigner
from pilot107.agent.operation_attempts import SQLiteAgentOperationAttemptStore
from pilot107.agent.operation_ledger import (
    AgentOperationState,
    SQLiteAgentOperationLedger,
    operation_intent_for_invocation,
)
from pilot107.agent.protocol import TOOL_INVOCATION_PROTOCOL_VERSION, ToolInvocation
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.agent.tool_gateway import AgentReadResult, AgentToolGateway, AgentToolGatewayError


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def epoch(self) -> int:
        return int(self.value.timestamp())


class FailAfterInitialHeartbeatAttemptStore:
    def __init__(self, delegate: SQLiteAgentOperationAttemptStore) -> None:
        self.delegate = delegate
        self.calls = 0
        self.failed = threading.Event()

    def prepare(self, operation_key: str, **kwargs):
        return self.delegate.prepare(operation_key, **kwargs)

    def classify(self, operation_key: str, **kwargs):
        return self.delegate.classify(operation_key, **kwargs)

    def mark_stale(self, operation_key: str, **kwargs):
        return self.delegate.mark_stale(operation_key, **kwargs)

    def heartbeat(self, operation_key: str, **kwargs) -> bool:
        self.calls += 1
        if self.calls == 1:
            return self.delegate.heartbeat(operation_key, **kwargs)
        self.failed.set()
        return False


class FastOperationHeartbeatGateway(AgentToolGateway):
    def _operation_heartbeat_interval_seconds(self, claims: AgentCapabilityClaims) -> float:
        del claims
        return 0.01


def _running_turn(database: Path, clock: FixedClock):
    store = SQLiteAgentSessionStore(database, clock=clock)
    session, _ = store.create_session(
        owner="alice",
        request_key="session-operation-heartbeat",
        profile_id="experiment_builder",
        model_profile_id="faux-default",
        source={"project_id": "project-1", "workspace_id": "workspace-1"},
    )
    turn, _ = store.create_turn(
        session_id=session.session_id,
        owner="alice",
        request_key="turn-operation-heartbeat",
        message="patch once",
        expected_state_version=session.state_version,
    )
    claim = store.claim_turn(turn.turn_id, worker_id="worker-1", lease_seconds=60)
    assert claim is not None
    return store, session, turn, claim


def _invocation(session, turn, claim) -> ToolInvocation:
    return ToolInvocation(
        schema_version=TOOL_INVOCATION_PROTOCOL_VERSION,
        invocation_id="invocation-operation-heartbeat",
        idempotency_key="idempotency-operation-heartbeat",
        owner="alice",
        session_id=session.session_id,
        turn_id=turn.turn_id,
        state_version=claim.state_version,
        profile_id="experiment_builder",
        tool_name="workspace_patch",
        arguments={
            "project_id": "project-1",
            "workspace_id": "workspace-1",
            "approval_summary_zh": "只执行一次。",
            "patches": [
                {
                    "path": "train.py",
                    "expected_source_digest": "a" * 64,
                    "operation": "modify",
                    "content": "print('patched')\n",
                }
            ],
        },
        deadline="2026-09-04T12:01:30Z",
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
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_handler_success_cannot_commit_after_attempt_heartbeat_is_fenced(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    clock = FixedClock()
    store, session, turn, claim = _running_turn(database, clock)
    ledger = SQLiteAgentOperationLedger(database, clock=clock)
    durable_attempts = SQLiteAgentOperationAttemptStore(database, clock=clock)
    attempts = FailAfterInitialHeartbeatAttemptStore(durable_attempts)
    invocation = _invocation(session, turn, claim)
    intent = operation_intent_for_invocation(
        store,
        invocation,
        arguments_digest=_semantic_digest(invocation),
    )
    assert intent is not None

    def handler(owner, arguments):
        del owner, arguments
        assert attempts.failed.wait(1.0)
        return AgentReadResult(
            result={"change_set_id": "changeset-should-not-complete"},
            evidence_refs=("changeset:changeset-should-not-complete",),
        )

    signer = AgentCapabilitySigner(b"s" * 32, clock=clock.epoch)
    claims = AgentCapabilityClaims(
        owner="alice",
        session_id=session.session_id,
        turn_id=turn.turn_id,
        state_version=claim.state_version,
        fencing_token=claim.fencing_token,
        profile_id="experiment_builder",
        tools=frozenset({"workspace_patch"}),
        max_invocations=16,
        max_bytes=262_144,
        expires_at=clock.epoch() + 120,
        project_id="project-1",
        workspace_id="workspace-1",
        operations=frozenset({"write"}),
        max_commands=0,
    )
    gateway = FastOperationHeartbeatGateway(
        store=store,
        signer=signer,
        handlers={},
        profile_handlers={"experiment_builder": {"workspace_patch": handler}},
        operation_ledger=ledger,
        operation_attempt_store=attempts,
        clock=clock,
    )

    with pytest.raises(AgentToolGatewayError) as failure:
        gateway.invoke(signer.sign(claims), invocation)

    assert failure.value.code == "AGENT.TOOL.OPERATION_UNKNOWN"
    assert attempts.calls >= 2
    operation = ledger.get(intent.operation_key, owner="alice")
    assert operation.state is AgentOperationState.UNKNOWN
    assert operation.receipt_ref is None
    assert operation.result is None
