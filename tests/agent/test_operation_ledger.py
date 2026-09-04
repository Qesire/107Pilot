from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pilot107.agent.operation_ledger import (
    POSTGRES_AGENT_OPERATION_SCHEMA,
    AgentOperationConflict,
    AgentOperationState,
    SQLiteAgentOperationLedger,
    durable_operation_identity,
)


class FixedClock:
    def __call__(self) -> datetime:
        return datetime(2026, 9, 4, 0, 0, tzinfo=UTC)


def _identity(**changes):
    values = {
        "owner": "alice",
        "session_id": "session-1",
        "tool_name": "validation_schedule",
        "arguments": {
            "run_id": "run-1",
            "partition": "Students",
            "request_key": "submit-1",
        },
        "user_request_key": "submit-1",
        "target_type": "run",
        "target_id": "run-1",
        "target_revision": "revision-7",
    }
    values.update(changes)
    return durable_operation_identity(**values)


def test_identity_is_canonical_and_provider_call_id_independent() -> None:
    first = _identity()
    reordered = _identity(
        arguments={
            "request_key": "submit-1",
            "partition": "Students",
            "run_id": "run-1",
        }
    )

    assert first == reordered
    assert first.operation_key.startswith("op-v1:")
    assert first.intent_digest.startswith("sha256:")


def test_target_revision_and_intent_change_operation_identity() -> None:
    base = _identity()
    changed_revision = _identity(target_revision="revision-8")
    changed_arguments = _identity(
        arguments={
            "run_id": "run-1",
            "partition": "P107-RTX5090",
            "request_key": "submit-1",
        }
    )

    assert changed_revision.operation_key != base.operation_key
    assert changed_revision.intent_digest != base.intent_digest
    assert changed_arguments.operation_key != base.operation_key
    assert changed_arguments.intent_digest != base.intent_digest


def test_new_provider_invocation_replays_same_reserved_receipt(tmp_path: Path) -> None:
    ledger = SQLiteAgentOperationLedger(tmp_path / "agent.db", clock=FixedClock())
    identity = _identity()

    first, created = ledger.reserve(identity, invocation_id="provider-call-1")
    replay, replay_created = ledger.reserve(identity, invocation_id="provider-call-2")

    assert created is True
    assert replay_created is False
    assert replay.operation_key == first.operation_key
    assert replay.latest_invocation_id == "provider-call-1"
    assert replay.state is AgentOperationState.RESERVED


def test_completed_receipt_is_terminal_and_content_stable(tmp_path: Path) -> None:
    ledger = SQLiteAgentOperationLedger(tmp_path / "agent.db", clock=FixedClock())
    identity = _identity()
    ledger.reserve(identity, invocation_id="provider-call-1")
    ledger.mark_running(
        identity.operation_key,
        owner="alice",
        invocation_id="provider-call-1",
    )
    result_digest = "sha256:" + hashlib.sha256(b"job:12345").hexdigest()

    completed = ledger.complete(
        identity.operation_key,
        owner="alice",
        invocation_id="provider-call-1",
        result_digest=result_digest,
        result_ref="run:run-1",
        side_effect_receipt_ref="slurm-submit:12345",
    )
    replay = ledger.complete(
        identity.operation_key,
        owner="alice",
        invocation_id="provider-call-2",
        result_digest=result_digest,
        result_ref="run:run-1",
        side_effect_receipt_ref="slurm-submit:12345",
    )

    assert completed.state is AgentOperationState.COMPLETED
    assert replay == completed

    with pytest.raises(AgentOperationConflict, match="terminal receipt content changed"):
        ledger.complete(
            identity.operation_key,
            owner="alice",
            invocation_id="provider-call-3",
            result_digest="sha256:" + hashlib.sha256(b"job:99999").hexdigest(),
            result_ref="run:run-1",
            side_effect_receipt_ref="slurm-submit:99999",
        )


def test_unknown_requires_reconciliation_before_terminal_claim(tmp_path: Path) -> None:
    ledger = SQLiteAgentOperationLedger(tmp_path / "agent.db", clock=FixedClock())
    identity = _identity()
    ledger.reserve(identity, invocation_id="provider-call-1")
    ledger.mark_running(
        identity.operation_key,
        owner="alice",
        invocation_id="provider-call-1",
    )
    ledger.mark_stale(identity.operation_key, owner="alice")
    unknown = ledger.mark_unknown(identity.operation_key, owner="alice")

    assert unknown.state is AgentOperationState.UNKNOWN
    with pytest.raises(AgentOperationConflict):
        ledger.complete(
            identity.operation_key,
            owner="alice",
            invocation_id="provider-call-2",
            result_digest="sha256:" + hashlib.sha256(b"unknown").hexdigest(),
            result_ref=None,
            side_effect_receipt_ref=None,
        )

    reconciling = ledger.begin_reconciliation(
        identity.operation_key,
        owner="alice",
        invocation_id="reconciler-1",
    )
    assert reconciling.state is AgentOperationState.RECONCILING


def test_reconcilable_listing_is_owner_scoped(tmp_path: Path) -> None:
    ledger = SQLiteAgentOperationLedger(tmp_path / "agent.db", clock=FixedClock())
    alice = _identity()
    bob = durable_operation_identity(
        owner="bob",
        session_id="session-2",
        tool_name="validation_schedule",
        arguments={"run_id": "run-2"},
        user_request_key="submit-2",
        target_type="run",
        target_id="run-2",
        target_revision="revision-1",
    )
    for identity, owner in ((alice, "alice"), (bob, "bob")):
        ledger.reserve(identity, invocation_id=f"call-{owner}")
        ledger.mark_running(
            identity.operation_key,
            owner=owner,
            invocation_id=f"call-{owner}",
        )
        ledger.mark_stale(identity.operation_key, owner=owner)

    assert [item.operation_key for item in ledger.list_reconcilable(owner="alice")] == [
        alice.operation_key
    ]


def test_postgres_contract_contains_same_identity_and_recovery_states() -> None:
    schema = "\n".join(POSTGRES_AGENT_OPERATION_SCHEMA)

    for field in (
        "operation_key",
        "intent_digest",
        "target_revision",
        "user_request_key",
        "side_effect_receipt_ref",
    ):
        assert field in schema
    for state in ("stale", "reconciling", "unknown"):
        assert state in schema
