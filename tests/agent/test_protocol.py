from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any

import pytest

from pilot107.agent.protocol import (
    MAX_NDJSON_LINE_BYTES,
    AgentdClientError,
    parse_event_lines,
)

_DIGEST = "a" * 64


def _usage() -> dict[str, int | None]:
    return {
        "input_tokens": 12,
        "output_tokens": 8,
        "cache_read_tokens": None,
        "cache_write_tokens": 0,
    }


def _checkpoint() -> dict[str, Any]:
    return {
        "schema_version": "pilot107.agent-checkpoint/v1",
        "turn_id": "turn-1",
        "lineage": ["parent-1"],
        "model_profile_id": "campus-default",
        "prompt_profile_id": "hpc-assistant-v1",
        "messages": [
            {
                "role": "user",
                "content": "hello",
                "tool_call_id": None,
                "tool_name": None,
                "is_error": None,
            }
        ],
        "completed_tools": [
            {
                "tool_call_id": "call-1",
                "tool_name": "emit_result",
                "arguments": {"value": 1},
                "result": ["ok", None],
                "is_error": False,
            }
        ],
        "usage": _usage(),
        "digest": _DIGEST,
    }


def _completed_payload(*, result: Any = None) -> dict[str, Any]:
    return {
        "result": {"text": "ok"} if result is None else result,
        "provider": "campus-openai-compatible",
        "model": "campus-model",
        "model_profile_id": "campus-default",
        "usage": _usage(),
        "provider_calls": 1,
        "checkpoint_digest": _DIGEST,
        "duration_ms": 42,
        "checkpoint": _checkpoint(),
    }


def _failed_payload() -> dict[str, Any]:
    return {
        "error": {
            "code": "provider_rate_limited",
            "retryable": True,
            "message": "The model provider is rate limited.",
            "provider_status": 429,
        },
        "checkpoint": _checkpoint(),
    }


def _event(sequence: int, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "pilot107.agent-turn-event/v1",
        "turn_id": "turn-1",
        "sequence": sequence,
        "timestamp": "2026-08-10T00:00:00.000Z",
        "type": event_type,
        "payload": payload,
    }


def _line(event: dict[str, Any]) -> bytes:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


def _valid_payloads() -> dict[str, dict[str, Any]]:
    return {
        "turn_started": {
            "model_profile_id": "campus-default",
            "task_kind": "interactive",
        },
        "message_delta": {"delta": "hello"},
        "tool_call_requested": {
            "tool_call_id": "call-1",
            "tool_name": "emit_result",
            "arguments": {"answer": [1, True, None]},
        },
        "tool_call_started": {
            "tool_call_id": "call-1",
            "tool_name": "emit_result",
        },
        "tool_call_progress": {
            "tool_call_id": "call-1",
            "tool_name": "emit_result",
            "progress": "working",
        },
        "tool_call_completed": {
            "tool_call_id": "call-1",
            "tool_name": "emit_result",
            "result": {"answer": 1.5},
            "is_error": False,
        },
        "checkpoint": {"checkpoint": _checkpoint()},
        "turn_completed": _completed_payload(),
        "turn_failed": _failed_payload(),
    }


def test_parse_event_stream_accepts_all_v1_event_payloads_and_arbitrary_json_result() -> None:
    payloads = _valid_payloads()
    event_types = [
        "turn_started",
        "message_delta",
        "tool_call_requested",
        "tool_call_started",
        "tool_call_progress",
        "tool_call_completed",
        "checkpoint",
    ]
    lines = [
        _line(_event(index, event_type, payloads[event_type]))
        for index, event_type in enumerate(event_types, start=1)
    ]
    lines.append(
        _line(
            _event(
                8,
                "turn_completed",
                _completed_payload(result=["interactive text", None, 3]),
            )
        )
    )

    events = list(parse_event_lines("turn-1", lines))

    assert [event.type for event in events] == [*event_types, "turn_completed"]
    assert events[-1].payload["result"] == ["interactive text", None, 3]


def test_parse_event_stream_accepts_a_closed_failed_terminal() -> None:
    events = list(
        parse_event_lines(
            "turn-1",
            [
                _line(_event(1, "turn_started", _valid_payloads()["turn_started"])),
                _line(_event(2, "turn_failed", _failed_payload())),
            ],
        )
    )

    assert events[-1].type == "turn_failed"


@pytest.mark.parametrize(
    ("mutate", "name"),
    [
        (
            lambda event: event.__setitem__("schema_version", "pilot107.agent-turn-event/v2"),
            "version",
        ),
        (lambda event: event.__setitem__("turn_id", "other-turn"), "turn id"),
        (lambda event: event.__setitem__("sequence", True), "integer sequence"),
        (lambda event: event.__setitem__("timestamp", ""), "timestamp"),
        (lambda event: event.__setitem__("type", "reasoning_delta"), "event type"),
        (lambda event: event.__setitem__("extra", "no"), "top-level field"),
    ],
)
def test_parse_event_stream_rejects_invalid_or_open_event_envelopes(
    mutate: Callable[[dict[str, Any]], None], name: str
) -> None:
    event = _event(1, "turn_completed", _completed_payload())
    mutate(event)

    with pytest.raises(AgentdClientError) as caught:
        list(parse_event_lines("turn-1", [_line(event)]))

    assert caught.value.code == "protocol_error", name


@pytest.mark.parametrize("event_type", sorted(_valid_payloads()))
def test_every_event_payload_rejects_unknown_fields(event_type: str) -> None:
    payload = copy.deepcopy(_valid_payloads()[event_type])
    payload["unknown"] = "must fail closed"

    with pytest.raises(AgentdClientError) as caught:
        list(parse_event_lines("turn-1", [_line(_event(1, event_type, payload))]))

    assert caught.value.code == "protocol_error"


@pytest.mark.parametrize(
    ("event_type", "missing"),
    [
        ("turn_started", "task_kind"),
        ("message_delta", "delta"),
        ("tool_call_requested", "arguments"),
        ("tool_call_started", "tool_name"),
        ("tool_call_progress", "progress"),
        ("tool_call_completed", "is_error"),
        ("checkpoint", "checkpoint"),
        ("turn_completed", "provider_calls"),
        ("turn_failed", "error"),
    ],
)
def test_every_event_payload_requires_its_wire_fields(event_type: str, missing: str) -> None:
    payload = copy.deepcopy(_valid_payloads()[event_type])
    del payload[missing]

    with pytest.raises(AgentdClientError) as caught:
        list(parse_event_lines("turn-1", [_line(_event(1, event_type, payload))]))

    assert caught.value.code == "protocol_error"


@pytest.mark.parametrize(
    ("event_type", "mutate"),
    [
        ("turn_started", lambda payload: payload.__setitem__("task_kind", "unknown")),
        ("message_delta", lambda payload: payload.__setitem__("delta", 1)),
        ("tool_call_requested", lambda payload: payload.__setitem__("tool_call_id", "bad/id")),
        ("tool_call_started", lambda payload: payload.__setitem__("tool_name", "")),
        ("tool_call_progress", lambda payload: payload.__setitem__("progress", "x" * 256_001)),
        ("tool_call_completed", lambda payload: payload.__setitem__("is_error", 0)),
        (
            "checkpoint",
            lambda payload: payload["checkpoint"]["messages"][0].__setitem__("extra", True),
        ),
        (
            "turn_completed",
            lambda payload: payload["usage"].__setitem__("input_tokens", -1),
        ),
        (
            "turn_failed",
            lambda payload: payload["error"].__setitem__("code", "raw_provider_error"),
        ),
    ],
)
def test_every_event_payload_validates_types_and_bounds(
    event_type: str, mutate: Callable[[dict[str, Any]], None]
) -> None:
    payload = copy.deepcopy(_valid_payloads()[event_type])
    mutate(payload)

    with pytest.raises(AgentdClientError) as caught:
        list(parse_event_lines("turn-1", [_line(_event(1, event_type, payload))]))

    assert caught.value.code == "protocol_error"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda checkpoint: checkpoint.__setitem__("schema_version", "pilot107.agent-checkpoint/v2"),
        lambda checkpoint: checkpoint.__setitem__("digest", "A" * 64),
        lambda checkpoint: checkpoint["usage"].__setitem__("unexpected", 1),
        lambda checkpoint: checkpoint["completed_tools"][0]["arguments"].__setitem__(
            "Authorization", "secret"
        ),
        lambda checkpoint: checkpoint["messages"][0].__setitem__("role", "system"),
    ],
)
def test_checkpoint_validation_matches_the_closed_shared_schema(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    payload = {"checkpoint": _checkpoint()}
    mutate(payload["checkpoint"])

    with pytest.raises(AgentdClientError) as caught:
        list(parse_event_lines("turn-1", [_line(_event(1, "checkpoint", payload))]))

    assert caught.value.code == "protocol_error"


def test_parse_event_stream_requires_contiguous_sequence_starting_at_one() -> None:
    lines = [
        _line(_event(1, "turn_started", _valid_payloads()["turn_started"])),
        _line(_event(3, "turn_completed", _completed_payload())),
    ]

    with pytest.raises(AgentdClientError) as caught:
        list(parse_event_lines("turn-1", lines))

    assert caught.value.code == "protocol_error"


def test_parse_event_stream_rejects_eof_without_terminal() -> None:
    with pytest.raises(AgentdClientError) as caught:
        list(
            parse_event_lines(
                "turn-1",
                [_line(_event(1, "turn_started", _valid_payloads()["turn_started"]))],
            )
        )

    assert caught.value.code == "protocol_error"


def test_parse_event_stream_rejects_data_after_terminal() -> None:
    lines = [
        _line(_event(1, "turn_completed", _completed_payload())),
        _line(_event(2, "message_delta", {"delta": "late"})),
    ]

    with pytest.raises(AgentdClientError) as caught:
        list(parse_event_lines("turn-1", lines))

    assert caught.value.code == "protocol_error"


@pytest.mark.parametrize(
    "line",
    [
        b"\xff\n",
        b"[]\n",
        b'{"schema_version":\n',
        b"\n",
    ],
)
def test_parse_event_stream_rejects_invalid_utf8_nonobjects_and_malformed_json(line: bytes) -> None:
    with pytest.raises(AgentdClientError) as caught:
        list(parse_event_lines("turn-1", [line]))

    assert caught.value.code == "protocol_error"


def test_parse_event_stream_enforces_the_one_mib_line_limit_in_bytes() -> None:
    oversized = b" " * (MAX_NDJSON_LINE_BYTES + 1)

    with pytest.raises(AgentdClientError) as caught:
        list(parse_event_lines("turn-1", [oversized]))

    assert caught.value.code == "protocol_error"
    assert "line" in str(caught.value)


def test_parse_event_stream_enforces_the_eight_mib_cumulative_limit() -> None:
    lines = [
        _line(_event(sequence, "message_delta", {"delta": "x" * 255_000}))
        for sequence in range(1, 35)
    ]

    with pytest.raises(AgentdClientError) as caught:
        list(parse_event_lines("turn-1", lines))

    assert caught.value.code == "protocol_error"
    assert "cumulative" in str(caught.value)
