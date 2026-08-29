from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any

import pytest

import pilot107.agent.protocol as agent_protocol
from pilot107.agent.protocol import (
    MAX_NDJSON_LINE_BYTES,
    AgentdClientError,
    parse_event_lines,
)

_DIGEST = "a" * 64


def _durable_request() -> dict[str, Any]:
    return {
        "schema_version": "pilot107.agent-turn-request/v2",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "owner": "alice",
        "state_version": 3,
        "task_kind": "interactive_readonly",
        "model_profile_id": "faux-default",
        "prompt_profile_id": "hpc-readonly-v1",
        "toolset_id": "a1-readonly",
        "input": {
            "message": "why is run-1 pending?",
            "context_refs": ["run:run-1"],
        },
        "capability_token": "opaque.test.token",
        "checkpoint": None,
        "limits": {"timeout_ms": 60_000, "max_output_tokens": 1_200},
        "trace": {"correlation_id": "turn-1"},
    }


def _tool_invocation() -> dict[str, Any]:
    return {
        "schema_version": "pilot107.agent-tool-invocation/v1",
        "invocation_id": "invocation-1",
        "idempotency_key": "turn-1:call-1",
        "owner": "alice",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "state_version": 3,
        "profile_id": "hpc-readonly-v1",
        "tool_name": "run_get",
        "arguments": {"run_id": "run-1"},
        "deadline": "2026-08-14T12:00:00Z",
    }


def test_durable_turn_request_accepts_only_the_a1_readonly_pairing() -> None:
    parsed = agent_protocol.parse_durable_turn_request(_durable_request())

    assert parsed.session_id == "session-1"
    assert parsed.message == "why is run-1 pending?"
    assert parsed.context_refs == ("run:run-1",)
    assert "opaque.test.token" not in repr(parsed)


def test_durable_turn_request_accepts_the_reasoner_timeout_boundary() -> None:
    request = _durable_request()
    request["limits"]["timeout_ms"] = 660_000

    assert agent_protocol.parse_durable_turn_request(request).turn_id == "turn-1"

    request["limits"]["timeout_ms"] = 660_001
    with pytest.raises(ValueError, match="durable Turn request"):
        agent_protocol.parse_durable_turn_request(request)


@pytest.mark.parametrize("profile_id", ["market_application", "template_publication"])
def test_market_profiles_use_the_closed_project_pairing(profile_id: str) -> None:
    request = _durable_request()
    request.update(
        task_kind=profile_id,
        prompt_profile_id=profile_id,
        toolset_id="a2-project",
    )

    parsed = agent_protocol.parse_durable_turn_request(request)

    assert parsed.profile_id == profile_id


@pytest.mark.parametrize(
    "mutate",
    [
        lambda request: request.__setitem__("extra", True),
        lambda request: request.__setitem__("prompt_profile_id", "hpc-assistant-v1"),
        lambda request: request.__setitem__("toolset_id", "a0-none"),
        lambda request: request["input"].__setitem__("extra", True),
    ],
)
def test_durable_turn_request_rejects_open_or_mismatched_shapes(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    request = _durable_request()
    mutate(request)

    with pytest.raises(ValueError, match="durable Turn request"):
        agent_protocol.parse_durable_turn_request(request)


def test_tool_invocation_and_result_envelopes_are_closed_and_mutually_exclusive() -> None:
    invocation = agent_protocol.parse_tool_invocation(_tool_invocation())
    success = agent_protocol.parse_tool_result(
        {
            "schema_version": "pilot107.agent-tool-result/v1",
            "invocation_id": "invocation-1",
            "result": {"run_id": "run-1", "state": "PENDING"},
            "error": None,
            "evidence_refs": ["run:run-1"],
            "bytes_returned": 45,
        }
    )

    assert invocation.tool_name == "run_get"
    assert success.result == {"run_id": "run-1", "state": "PENDING"}

    with pytest.raises(ValueError, match="tool result"):
        agent_protocol.parse_tool_result(
            {
                "schema_version": "pilot107.agent-tool-result/v1",
                "invocation_id": "invocation-1",
                "result": {},
                "error": {"code": "forbidden", "message": "denied", "retryable": False},
                "evidence_refs": [],
                "bytes_returned": 0,
            }
        )


def test_tool_invocation_rejects_unknown_tools_and_authority_fields() -> None:
    unknown_tool = _tool_invocation()
    unknown_tool["tool_name"] = "shell_exec"
    authority = _tool_invocation()
    authority["capability_token"] = "must-be-in-the-header"

    with pytest.raises(ValueError, match="tool invocation"):
        agent_protocol.parse_tool_invocation(unknown_tool)
    with pytest.raises(ValueError, match="tool invocation"):
        agent_protocol.parse_tool_invocation(authority)


def test_workspace_tools_are_invalid_for_a1_but_remain_valid_for_a2() -> None:
    readonly = _tool_invocation()
    readonly.update(
        tool_name="workspace_read",
        arguments={"workspace": "guessed", "path": "README.md"},
    )

    with pytest.raises(ValueError, match="invalid tool invocation"):
        agent_protocol.parse_tool_invocation(readonly)

    project = _tool_invocation()
    project.update(
        profile_id="experiment_builder",
        tool_name="workspace_read",
        arguments={"workspace": "workspace-1", "path": "README.md"},
    )
    assert agent_protocol.parse_tool_invocation(project).tool_name == "workspace_read"


@pytest.mark.parametrize(
    "tool_name", ["builder_context_get", "builder_build_submit"]
)
def test_phase_aware_builder_tools_are_valid_only_for_project_profiles(
    tool_name: str,
) -> None:
    project = _tool_invocation()
    project.update(
        profile_id="experiment_builder",
        tool_name=tool_name,
        arguments={"project_id": "project-1", "workspace_id": "workspace-1"},
    )
    assert agent_protocol.parse_tool_invocation(project).tool_name == tool_name

    readonly = dict(project)
    readonly["profile_id"] = "hpc-readonly-v1"
    with pytest.raises(ValueError, match="invalid tool invocation"):
        agent_protocol.parse_tool_invocation(readonly)


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
    "code",
    ["empty_provider_response", "tool_step_budget_exhausted"],
)
def test_turn_failed_accepts_bounded_pi_terminal_codes(code: str) -> None:
    payload = _failed_payload()
    payload["error"]["code"] = code

    events = list(parse_event_lines("turn-1", [_line(_event(1, "turn_failed", payload))]))

    assert events[0].payload["error"]["code"] == code


@pytest.mark.parametrize(
    "task_kind",
    ["interactive_readonly", "experiment_builder", "run_diagnosis_repair"],
)
def test_turn_started_accepts_real_durable_task_kinds(task_kind: str) -> None:
    payload = copy.deepcopy(_valid_payloads()["turn_started"])
    payload["task_kind"] = task_kind

    events = list(
        parse_event_lines(
            "turn-1",
            [
                _line(_event(1, "turn_started", payload)),
                _line(_event(2, "turn_completed", _completed_payload())),
            ],
        )
    )

    assert events[0].payload["task_kind"] == task_kind


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
