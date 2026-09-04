from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pilot107.agent.capabilities import AgentCapabilityClaims, AgentCapabilitySigner
from pilot107.agent.operation_ledger import (
    AgentOperationState,
    SQLiteAgentOperationLedger,
    operation_intent_for_invocation,
)
from pilot107.agent.operation_reconciler import SQLiteAgentOperationReconciler
from pilot107.agent.protocol import TOOL_INVOCATION_PROTOCOL_VERSION, ToolInvocation
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.agent.tool_gateway import AgentReadResult, AgentToolGateway


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 4, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def epoch(self) -> int:
        return int(self.value.timestamp())


def _running_turn(database: Path, clock: FixedClock):
    store = SQLiteAgentSessionStore(database, clock=clock)
    session, _ = store.create_session(
        owner="alice",
        request_key="session-request",
        profile_id="experiment_builder",
        model_profile_id="faux-default",
        source={"project_id": "project-1", "workspace_id": "workspace-1"},
    )
    turn, _ = store.create_turn(
        session_id=session.session_id,
        owner="alice",
        request_key="turn-request",
        message="continue builder",
        expected_state_version=session.state_version,
    )
    claim = store.claim_turn(turn.turn_id, worker_id="worker-1", lease_seconds=120)
    assert claim is not None
    return store, session, turn, claim


def _invocation(session, turn, claim, *, tool: str, request_key: str, call: str):
    arguments: dict[str, object] = {
        "project_id": "project-1",
        "workspace_id": "workspace-1",
        "session_id": session.session_id,
        "turn_id": turn.turn_id,
        "request_key": request_key,
    }
    if tool == "builder_build_submit":
        arguments |= {
            "approval_summary_zh": "继续已持久化的构建。",
            "expected_project_version": 1,
            "expected_workspace_snapshot_digest": "a" * 64,
            "base_change_set_id": None,
            "blueprint": {},
            "patches": [],
        }
    return ToolInvocation(
        schema_version=TOOL_INVOCATION_PROTOCOL_VERSION,
        invocation_id=f"invocation-{call}",
        idempotency_key=f"idempotency-{call}",
        owner="alice",
        session_id=session.session_id,
        turn_id=turn.turn_id,
        state_version=claim.state_version,
        profile_id="experiment_builder",
        tool_name=tool,
        arguments=arguments,
        deadline="2026-09-04T09:01:30Z",
    )


def _semantic_digest(arguments: dict[str, object]) -> str:
    value = dict(arguments)
    value.pop("turn_id", None)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _seed_unknown(store, ledger, invocation, claim):
    intent = operation_intent_for_invocation(
        store,
        invocation,
        arguments_digest=_semantic_digest(invocation.arguments),
    )
    assert intent is not None
    _, created = ledger.reserve(
        intent,
        invocation_id=invocation.invocation_id,
        expected_state_version=claim.state_version,
        expected_fencing_token=claim.fencing_token,
    )
    assert created
    ledger.start(
        intent.operation_key,
        owner="alice",
        invocation_id=invocation.invocation_id,
        expected_state_version=claim.state_version,
        expected_fencing_token=claim.fencing_token,
    )
    ledger.mark_unknown(
        intent.operation_key,
        owner="alice",
        expected_state_version=claim.state_version,
        expected_fencing_token=claim.fencing_token,
        error={"code": "AGENT.TOOL.OPERATION_UNKNOWN", "message": "crash"},
    )
    return intent


def _builder_table(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_builder_submissions (
                submission_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                request_key TEXT NOT NULL,
                change_set_id TEXT,
                sandbox_result_id TEXT,
                task_id TEXT,
                receipt_json TEXT
            )
            """
        )


def _task_table(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_tasks (
                task_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                request_key TEXT NOT NULL,
                state TEXT NOT NULL,
                version INTEGER NOT NULL,
                linked_run_id TEXT,
                schedule_receipt TEXT
            )
            """
        )


def test_builder_terminal_receipt_resolves_unknown_operation(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    clock = FixedClock()
    store, session, turn, claim = _running_turn(database, clock)
    ledger = SQLiteAgentOperationLedger(database, clock=clock)
    invocation = _invocation(
        session,
        turn,
        claim,
        tool="builder_build_submit",
        request_key="builder-request-1",
        call="seed",
    )
    intent = _seed_unknown(store, ledger, invocation, claim)
    _builder_table(database)
    receipt = {
        "submission_id": "builder-submission-1",
        "status": "scheduled",
        "phase": "validation_scheduled",
        "task_id": "task-1",
    }
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO agent_builder_submissions (
                submission_id, owner, request_key, change_set_id,
                sandbox_result_id, task_id, receipt_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "builder-submission-1",
                "alice",
                "builder-request-1",
                "changeset-1",
                "sandbox-1",
                "task-1",
                json.dumps(receipt),
            ),
        )

    reconciler = SQLiteAgentOperationReconciler(database, ledger=ledger, clock=clock)
    resolved = reconciler.reconcile(
        ledger.get(intent.operation_key, owner="alice"),
        invocation=invocation,
        expected_fencing_token=claim.fencing_token,
    )

    assert resolved is not None
    assert resolved.state is AgentOperationState.COMPLETED
    assert resolved.result is not None
    assert resolved.result["result"] == receipt
    assert resolved.result["evidence_refs"] == [
        "builder-submission:builder-submission-1",
        "changeset:changeset-1",
        "sandbox:sandbox-1",
        "agent-task:task-1",
    ]
    assert resolved.reconciliation_attempt == 1


def test_raw_pending_task_is_not_treated_as_schedule_receipt(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    clock = FixedClock()
    store, session, turn, claim = _running_turn(database, clock)
    ledger = SQLiteAgentOperationLedger(database, clock=clock)
    invocation = _invocation(
        session,
        turn,
        claim,
        tool="validation_schedule",
        request_key="validation-request-1",
        call="seed",
    )
    intent = _seed_unknown(store, ledger, invocation, claim)
    _task_table(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO agent_tasks (
                task_id, owner, request_key, state, version, linked_run_id, schedule_receipt
            ) VALUES (?, ?, ?, 'pending', 0, NULL, NULL)
            """,
            ("task-1", "alice", "validation-request-1"),
        )

    reconciler = SQLiteAgentOperationReconciler(database, ledger=ledger, clock=clock)
    assert (
        reconciler.reconcile(
            ledger.get(intent.operation_key, owner="alice"),
            invocation=invocation,
            expected_fencing_token=claim.fencing_token,
        )
        is None
    )
    assert ledger.get(intent.operation_key, owner="alice").state is AgentOperationState.UNKNOWN


def test_advanced_task_is_authoritative_schedule_fact(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    clock = FixedClock()
    store, session, turn, claim = _running_turn(database, clock)
    ledger = SQLiteAgentOperationLedger(database, clock=clock)
    invocation = _invocation(
        session,
        turn,
        claim,
        tool="validation_schedule",
        request_key="validation-request-2",
        call="seed",
    )
    intent = _seed_unknown(store, ledger, invocation, claim)
    _task_table(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO agent_tasks (
                task_id, owner, request_key, state, version, linked_run_id, schedule_receipt
            ) VALUES (?, ?, ?, 'running', 1, ?, NULL)
            """,
            ("task-2", "alice", "validation-request-2", "run-2"),
        )

    reconciler = SQLiteAgentOperationReconciler(database, ledger=ledger, clock=clock)
    resolved = reconciler.reconcile(
        ledger.get(intent.operation_key, owner="alice"),
        invocation=invocation,
        expected_fencing_token=claim.fencing_token,
    )

    assert resolved is not None
    assert resolved.state is AgentOperationState.COMPLETED
    assert resolved.result == {
        "result": {
            "task_id": "task-2",
            "state": "running",
            "linked_run_id": "run-2",
            "terminate": True,
        },
        "evidence_refs": ["agent-task:task-2"],
        "bytes_returned": resolved.result["bytes_returned"],
    }


def test_gateway_replays_reconciled_builder_receipt_without_handler(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    clock = FixedClock()
    store, session, turn, claim = _running_turn(database, clock)
    ledger = SQLiteAgentOperationLedger(database, clock=clock)
    seed = _invocation(
        session,
        turn,
        claim,
        tool="builder_build_submit",
        request_key="builder-request-gateway",
        call="seed",
    )
    intent = _seed_unknown(store, ledger, seed, claim)
    _builder_table(database)
    receipt = {
        "submission_id": "builder-submission-gateway",
        "status": "repair_required",
        "phase": "sandbox_failed",
    }
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO agent_builder_submissions (
                submission_id, owner, request_key, change_set_id,
                sandbox_result_id, task_id, receipt_json
            ) VALUES (?, ?, ?, NULL, NULL, NULL, ?)
            """,
            (
                "builder-submission-gateway",
                "alice",
                "builder-request-gateway",
                json.dumps(receipt),
            ),
        )

    called: list[str] = []

    def must_not_run(owner, arguments):
        called.append(owner)
        return AgentReadResult(result={}, evidence_refs=())

    signer = AgentCapabilitySigner(b"s" * 32, clock=clock.epoch)
    claims = AgentCapabilityClaims(
        owner="alice",
        session_id=session.session_id,
        turn_id=turn.turn_id,
        state_version=claim.state_version,
        fencing_token=claim.fencing_token,
        profile_id="experiment_builder",
        tools=frozenset({"builder_build_submit"}),
        max_invocations=16,
        max_bytes=262_144,
        expires_at=clock.epoch() + 120,
        project_id="project-1",
        workspace_id="workspace-1",
        operations=frozenset({"write", "validate"}),
        max_commands=16,
    )
    gateway = AgentToolGateway(
        store=store,
        signer=signer,
        handlers={},
        profile_handlers={"experiment_builder": {"builder_build_submit": must_not_run}},
        clock=clock,
    )
    replay = _invocation(
        session,
        turn,
        claim,
        tool="builder_build_submit",
        request_key="builder-request-gateway",
        call="replay",
    )

    result = gateway.invoke(signer.sign(claims), replay)

    assert result.result == receipt
    assert called == []
    assert ledger.get(intent.operation_key, owner="alice").state is AgentOperationState.COMPLETED
