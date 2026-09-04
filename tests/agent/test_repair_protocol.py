from __future__ import annotations

import pytest

from pilot107.agent.client import _build_durable_turn_request
from pilot107.agent.config import AgentdClientConfig
from pilot107.agent.protocol import AgentdClientError
from pilot107.agent.repair_protocol import (
    DURABLE_REPAIR_TURN_PROTOCOL_VERSION,
    ReceiptRepairingDurableAgentTurnRequest,
    ToolReceiptRepair,
)


def _config() -> AgentdClientConfig:
    return AgentdClientConfig(
        base_url="http://pilot-agentd:8091",
        token="internal-secret",
        model_profile_id="campus-default",
        timeout_seconds=30.0,
        max_output_tokens=1200,
    )


def _repair() -> ToolReceiptRepair:
    invocation_id = "inv-" + "a" * 64
    return ToolReceiptRepair(
        parent_checkpoint_digest=None,
        invocation_id=invocation_id,
        receipt_ref=f"agent-tool-receipt:{invocation_id}:sha256:" + "b" * 64,
        tool_call_id="call-1",
        tool_name="platform_get_snapshot",
        arguments={},
        assistant_text="checking durable state",
        content='{"ok":true}',
        details={
            "result": {"ok": True},
            "evidence_refs": ["evidence://snapshot"],
            "bytes_returned": 12,
        },
        is_error=False,
    )


def _request(*repairs: ToolReceiptRepair) -> ReceiptRepairingDurableAgentTurnRequest:
    return ReceiptRepairingDurableAgentTurnRequest(
        session_id="session-1",
        turn_id="turn-1",
        owner="alice",
        state_version=3,
        model_profile_id="campus-default",
        message="inspect durable state",
        context_refs=(),
        capability_token="opaque.capability.token",
        profile_id="hpc-readonly-v1",
        receipt_repairs=repairs,
    )


def test_repairing_request_serializes_as_closed_v3_envelope() -> None:
    repair = _repair()

    payload = _build_durable_turn_request(_config(), _request(repair))

    assert payload["schema_version"] == DURABLE_REPAIR_TURN_PROTOCOL_VERSION
    assert payload["session_id"] == "session-1"
    assert payload["turn_id"] == "turn-1"
    assert payload["owner"] == "alice"
    assert payload["state_version"] == 3
    assert payload["task_kind"] == "interactive_readonly"
    assert payload["model_profile_id"] == "campus-default"
    assert payload["prompt_profile_id"] == "hpc-readonly-v1"
    assert payload["toolset_id"] == "a1-readonly"
    assert payload["input"] == {
        "message": "inspect durable state",
        "context_refs": [],
    }
    assert payload["checkpoint"] is None
    assert payload["receipt_repairs"] == [
        {
            "parent_checkpoint_digest": None,
            "invocation_id": repair.invocation_id,
            "receipt_ref": repair.receipt_ref,
            "tool_call_id": "call-1",
            "tool_name": "platform_get_snapshot",
            "arguments": {},
            "assistant_text": "checking durable state",
            "content": '{"ok":true}',
            "details": {
                "result": {"ok": True},
                "evidence_refs": ["evidence://snapshot"],
                "bytes_returned": 12,
            },
            "is_error": False,
        }
    ]


def test_duplicate_receipt_repairs_fail_before_http_boundary() -> None:
    repair = _repair()

    with pytest.raises(AgentdClientError) as caught:
        _build_durable_turn_request(_config(), _request(repair, repair))

    assert caught.value.code == "invalid_request"
