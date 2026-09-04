"""Strict boundary types for durable tool-receipt checkpoint repair.

The existing durable Turn v2 and checkpoint v1 contracts remain unchanged.
Only recovery dispatches that carry proven terminal tool receipts use the v3
boundary envelope defined here; the agentd boundary reduces it back to the
existing executable request before the Turn executor sees it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pilot107.agent.protocol import (
    DurableAgentTurnRequest,
    parse_durable_turn_request,
    validate_json_object,
)

DURABLE_REPAIR_TURN_PROTOCOL_VERSION = "pilot107.agent-turn-request/v3"
_MAX_REPAIRS = 256
_DIGEST_LENGTH = 64


@dataclass(frozen=True)
class ToolReceiptRepair:
    parent_checkpoint_digest: str | None
    invocation_id: str
    receipt_ref: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    assistant_text: str
    content: str
    details: dict[str, Any]
    is_error: bool = False


@dataclass(frozen=True)
class ReceiptRepairingDurableAgentTurnRequest(DurableAgentTurnRequest):
    receipt_repairs: tuple[ToolReceiptRepair, ...] = ()


def serialize_receipt_repair(repair: ToolReceiptRepair) -> dict[str, Any]:
    validate_receipt_repair(repair)
    return {
        "parent_checkpoint_digest": repair.parent_checkpoint_digest,
        "invocation_id": repair.invocation_id,
        "receipt_ref": repair.receipt_ref,
        "tool_call_id": repair.tool_call_id,
        "tool_name": repair.tool_name,
        "arguments": repair.arguments,
        "assistant_text": repair.assistant_text,
        "content": repair.content,
        "details": repair.details,
        "is_error": repair.is_error,
    }


def validate_receipt_repair(repair: ToolReceiptRepair) -> None:
    if repair.parent_checkpoint_digest is not None and not _is_digest(
        repair.parent_checkpoint_digest
    ):
        raise ValueError("receipt repair parent checkpoint digest is invalid")
    for value, label in (
        (repair.invocation_id, "invocation_id"),
        (repair.tool_call_id, "tool_call_id"),
        (repair.tool_name, "tool_name"),
    ):
        if not _is_protocol_id(value):
            raise ValueError(f"receipt repair {label} is invalid")
    if (
        not isinstance(repair.receipt_ref, str)
        or not 1 <= len(repair.receipt_ref) <= 512
        or any(char in repair.receipt_ref for char in "\r\n\0")
    ):
        raise ValueError("receipt repair reference is invalid")
    if not isinstance(repair.assistant_text, str) or len(repair.assistant_text) > 256_000:
        raise ValueError("receipt repair assistant text is invalid")
    if not isinstance(repair.content, str) or len(repair.content) > 256_000:
        raise ValueError("receipt repair content is invalid")
    if repair.is_error is not False:
        # AC3 initially repairs only success receipts. Failure receipts remain
        # durable and replay-safe, but are not synthesized into checkpoint
        # messages until their pi-agent-core error representation is frozen.
        raise ValueError("receipt repair must represent a successful tool result")
    validate_json_object(repair.arguments)
    validate_json_object(repair.details)


def validate_repairing_payload(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != DURABLE_REPAIR_TURN_PROTOCOL_VERSION:
        raise ValueError("unsupported repair durable Turn request version")
    raw_repairs = payload.get("receipt_repairs")
    if not isinstance(raw_repairs, list) or len(raw_repairs) > _MAX_REPAIRS:
        raise ValueError("receipt repairs are invalid")

    # Reuse the existing closed v2 parser for every non-repair field.
    base = dict(payload)
    base["schema_version"] = "pilot107.agent-turn-request/v2"
    base.pop("receipt_repairs", None)
    parse_durable_turn_request(base)

    seen_calls: set[str] = set()
    seen_invocations: set[str] = set()
    seen_receipts: set[str] = set()
    for raw in raw_repairs:
        if not isinstance(raw, dict) or set(raw) != {
            "parent_checkpoint_digest",
            "invocation_id",
            "receipt_ref",
            "tool_call_id",
            "tool_name",
            "arguments",
            "assistant_text",
            "content",
            "details",
            "is_error",
        }:
            raise ValueError("receipt repair does not match the closed schema")
        repair = ToolReceiptRepair(
            parent_checkpoint_digest=raw["parent_checkpoint_digest"],
            invocation_id=raw["invocation_id"],
            receipt_ref=raw["receipt_ref"],
            tool_call_id=raw["tool_call_id"],
            tool_name=raw["tool_name"],
            arguments=raw["arguments"],
            assistant_text=raw["assistant_text"],
            content=raw["content"],
            details=raw["details"],
            is_error=raw["is_error"],
        )
        validate_receipt_repair(repair)
        if (
            repair.tool_call_id in seen_calls
            or repair.invocation_id in seen_invocations
            or repair.receipt_ref in seen_receipts
        ):
            raise ValueError("receipt repair identity is duplicated")
        seen_calls.add(repair.tool_call_id)
        seen_invocations.add(repair.invocation_id)
        seen_receipts.add(repair.receipt_ref)


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _DIGEST_LENGTH
        and all(char in "0123456789abcdef" for char in value)
    )


def _is_protocol_id(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return False
    return all(char.isascii() and (char.isalnum() or char in "._:-") for char in value)
