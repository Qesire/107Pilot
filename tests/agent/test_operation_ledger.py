from __future__ import annotations

import hashlib
import json
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pilot107.agent.capabilities import AgentCapabilityClaims, AgentCapabilitySigner
from pilot107.agent.operation_ledger import (
    AgentOperationState,
    SQLiteAgentOperationLedger,
    operation_intent_for_invocation,
)
from pilot107.agent.protocol import TOOL_INVOCATION_PROTOCOL_VERSION, ToolInvocation
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.agent.tool_gateway import AgentReadResult, AgentToolGateway, AgentToolGatewayError
from pilot107.agent.workspace import AgentWorkspaceRecord, WorkspaceChangeSet


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def epoch(self) -> int:
        return int(self.value.timestamp())

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _running_project_turn(database: Path, clock: MutableClock):
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
        request_key="user-request-1",
        message="patch the experiment",
        expected_state_version=session.state_version,
    )
    claim = store.claim_turn(turn.turn_id, worker_id="worker-1", lease_seconds=60)
    assert claim is not None
    return store, session, turn, claim


def _claims(clock, session, turn, claim) -> AgentCapabilityClaims:
    return AgentCapabilityClaims(
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


def _patch_invocation(session, turn, claim, *, call: str, content: str = "print('fixed')\n"):
    return ToolInvocation(
        schema_version=TOOL_INVOCATION_PROTOCOL_VERSION,
        invocation_id=f"invocation-{call}",
        idempotency_key=f"idempotency-{call}",
        owner="alice",
        session_id=session.session_id,
        turn_id=turn.turn_id,
        state_version=claim.state_version,
        profile_id="experiment_builder",
        tool_name="workspace_patch",
        arguments={
            "project_id": "project-1",
            "workspace_id": "workspace-1",
            "approval_summary_zh": "修复训练入口。",
            "patches": [
                {
                    "path": "train.py",
                    "expected_source_digest": "a" * 64,
                    "operation": "modify",
                    "content": content,
                }
            ],
        },
        deadline="2026-09-04T08:01:30Z",
    )


def _gateway(store, clock, handler):
    signer = AgentCapabilitySigner(b"s" * 32, clock=clock.epoch)
    gateway = AgentToolGateway(
        store=store,
        signer=signer,
        handlers={},
        profile_handlers={"experiment_builder": {"workspace_patch": handler}},
        clock=clock,
    )
    return signer, gateway


def _arguments_digest(arguments: object) -> str:
    encoded = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_mutation_replays_completed_operation_across_provider_call_ids(tmp_path: Path) -> None:
    clock = MutableClock()
    store, session, turn, claim = _running_project_turn(tmp_path / "agent.db", clock)
    calls: list[str] = []

    def patch(owner, arguments):
        calls.append(owner)
        return AgentReadResult(
            result={"change_set_id": "changeset-1", "state": "draft"},
            evidence_refs=("changeset:changeset-1",),
        )

    signer, gateway = _gateway(store, clock, patch)
    token = signer.sign(_claims(clock, session, turn, claim))

    first = gateway.invoke(token, _patch_invocation(session, turn, claim, call="provider-a"))
    replay = gateway.invoke(token, _patch_invocation(session, turn, claim, call="provider-b"))

    assert first.result == replay.result == {"change_set_id": "changeset-1", "state": "draft"}
    assert replay.invocation_id == "invocation-provider-b"
    assert calls == ["alice"]


def test_same_stable_operation_rejects_changed_canonical_intent(tmp_path: Path) -> None:
    clock = MutableClock()
    store, session, turn, claim = _running_project_turn(tmp_path / "agent.db", clock)
    calls: list[str] = []

    def patch(owner, arguments):
        calls.append(owner)
        return AgentReadResult(
            result={"change_set_id": "changeset-1"},
            evidence_refs=("changeset:changeset-1",),
        )

    signer, gateway = _gateway(store, clock, patch)
    token = signer.sign(_claims(clock, session, turn, claim))
    gateway.invoke(token, _patch_invocation(session, turn, claim, call="provider-a"))

    with pytest.raises(AgentToolGatewayError) as conflict:
        gateway.invoke(
            token,
            _patch_invocation(
                session,
                turn,
                claim,
                call="provider-b",
                content="print('different')\n",
            ),
        )

    assert conflict.value.code == "AGENT.TOOL.OPERATION_CONFLICT"
    assert calls == ["alice"]


def test_unknown_mutation_outcome_is_not_reexecuted(tmp_path: Path) -> None:
    clock = MutableClock()
    store, session, turn, claim = _running_project_turn(tmp_path / "agent.db", clock)
    calls: list[str] = []

    def patch(owner, arguments):
        calls.append(owner)
        raise RuntimeError("crash after an externally visible mutation")

    signer, gateway = _gateway(store, clock, patch)
    token = signer.sign(_claims(clock, session, turn, claim))
    first_invocation = _patch_invocation(session, turn, claim, call="provider-a")

    with pytest.raises(AgentToolGatewayError) as first:
        gateway.invoke(token, first_invocation)
    assert first.value.code == "AGENT.TOOL.OPERATION_UNKNOWN"

    intent = operation_intent_for_invocation(
        store,
        first_invocation,
        arguments_digest=_arguments_digest(first_invocation.arguments),
    )
    assert intent is not None
    ledger = SQLiteAgentOperationLedger(tmp_path / "agent.db", clock=clock)
    assert ledger.get(intent.operation_key, owner="alice").state is AgentOperationState.UNKNOWN

    with pytest.raises(AgentToolGatewayError) as replay:
        gateway.invoke(token, _patch_invocation(session, turn, claim, call="provider-b"))
    assert replay.value.code == "AGENT.TOOL.OPERATION_UNKNOWN"
    assert calls == ["alice"]


def test_terminal_receipt_rebuilds_new_provider_invocation_without_handler(tmp_path: Path) -> None:
    clock = MutableClock()
    store, session, turn, claim = _running_project_turn(tmp_path / "agent.db", clock)
    seed_invocation = _patch_invocation(session, turn, claim, call="provider-a")
    intent = operation_intent_for_invocation(
        store,
        seed_invocation,
        arguments_digest=_arguments_digest(seed_invocation.arguments),
    )
    assert intent is not None
    ledger = SQLiteAgentOperationLedger(tmp_path / "agent.db", clock=clock)
    _, created = ledger.reserve(
        intent,
        invocation_id=seed_invocation.invocation_id,
        expected_state_version=claim.state_version,
        expected_fencing_token=claim.fencing_token,
    )
    assert created
    ledger.start(
        intent.operation_key,
        owner="alice",
        invocation_id=seed_invocation.invocation_id,
        expected_state_version=claim.state_version,
        expected_fencing_token=claim.fencing_token,
    )
    terminal = ledger.complete(
        intent.operation_key,
        owner="alice",
        expected_state_version=claim.state_version,
        expected_fencing_token=claim.fencing_token,
        result={
            "result": {"change_set_id": "changeset-1"},
            "evidence_refs": ["changeset:changeset-1"],
            "bytes_returned": 31,
        },
        side_effect_ref="changeset:changeset-1",
    )
    assert terminal.receipt_ref is not None

    calls: list[str] = []

    def must_not_run(owner, arguments):
        calls.append(owner)
        raise AssertionError("receipt replay must not call the mutation handler")

    signer, gateway = _gateway(store, clock, must_not_run)
    token = signer.sign(_claims(clock, session, turn, claim))
    replay = gateway.invoke(token, _patch_invocation(session, turn, claim, call="provider-b"))

    assert replay.result == {"change_set_id": "changeset-1"}
    assert calls == []


def test_operation_reservation_is_fenced_with_its_origin_turn(tmp_path: Path) -> None:
    clock = MutableClock()
    store, session, turn, claim = _running_project_turn(tmp_path / "agent.db", clock)
    invocation = _patch_invocation(session, turn, claim, call="provider-a")
    intent = operation_intent_for_invocation(
        store,
        invocation,
        arguments_digest=_arguments_digest(invocation.arguments),
    )
    assert intent is not None
    ledger = SQLiteAgentOperationLedger(tmp_path / "agent.db", clock=clock)

    clock.advance(61)
    reclaimed = store.claim_turn(turn.turn_id, worker_id="worker-2", lease_seconds=60)
    assert reclaimed is not None

    with pytest.raises(Exception):
        ledger.reserve(
            intent,
            invocation_id=invocation.invocation_id,
            expected_state_version=claim.state_version,
            expected_fencing_token=claim.fencing_token,
        )


def test_ac1_does_not_change_workspace_snapshot_contract() -> None:
    assert [field.name for field in fields(AgentWorkspaceRecord)] == [
        "workspace_id",
        "project_id",
        "owner",
        "local_root",
        "snapshot",
        "created_at",
        "updated_at",
    ]
    assert "base_snapshot_digest" in {field.name for field in fields(WorkspaceChangeSet)}
    assert "live_revision" not in {field.name for field in fields(AgentWorkspaceRecord)}
    assert "live_digest" not in {field.name for field in fields(AgentWorkspaceRecord)}
