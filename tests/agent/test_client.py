from __future__ import annotations

import io
import json
import traceback
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from email.message import Message
from typing import Any

import pytest

import pilot107.agent.protocol as agent_protocol
from pilot107.agent.client import AgentdClient
from pilot107.agent.config import AgentdClientConfig, config_from_env
from pilot107.agent.protocol import AgentdClientError, AgentTurnEvent

_DIGEST = "b" * 64


def _config(**overrides: Any) -> AgentdClientConfig:
    values: dict[str, Any] = {
        "base_url": "http://pilot-agentd:8091",
        "token": "internal-secret",
        "model_profile_id": "campus-default",
        "timeout_seconds": 30.0,
        "max_output_tokens": 1200,
    }
    values.update(overrides)
    return AgentdClientConfig(**values)


def _usage() -> dict[str, int | None]:
    return {
        "input_tokens": 12,
        "output_tokens": 8,
        "cache_read_tokens": None,
        "cache_write_tokens": 0,
    }


def _event(sequence: int, event_type: str, payload: dict[str, Any]) -> bytes:
    value = {
        "schema_version": "pilot107.agent-turn-event/v1",
        "turn_id": "turn-1",
        "sequence": sequence,
        "timestamp": "2026-08-10T00:00:00.000Z",
        "type": event_type,
        "payload": payload,
    }
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


def _completed(*, result: Any = None) -> bytes:
    return _event(
        2,
        "turn_completed",
        {
            "result": {"text": "ok"} if result is None else result,
            "provider": "campus-openai-compatible",
            "model": "campus-model",
            "model_profile_id": "campus-default",
            "usage": _usage(),
            "provider_calls": 1,
            "checkpoint_digest": _DIGEST,
            "duration_ms": 42,
        },
    )


def _success_body(*, result: Any = None) -> bytes:
    return b"".join(
        [
            _event(
                1,
                "turn_started",
                {"model_profile_id": "campus-default", "task_kind": "interactive"},
            ),
            _completed(result=result),
        ]
    )


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        content_type: str = "application/x-ndjson; charset=utf-8",
    ) -> None:
        self._body = io.BytesIO(body)
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.closed = False

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def readline(self, size: int = -1) -> bytes:
        return self._body.readline(size)

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def close(self) -> None:
        self.closed = True
        self._body.close()


class _TrackingEnv(Mapping[str, str]):
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.reads: list[str] = []

    def __getitem__(self, key: str) -> str:
        self.reads.append(key)
        return self.values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)

    def get(self, key: str, default: str | None = None) -> str | None:
        self.reads.append(key)
        return self.values.get(key, default)


def test_config_loader_reads_only_agentd_names_and_redacts_the_token() -> None:
    env = _TrackingEnv(
        {
            "PILOT107_AGENTD_URL": "http://pilot-agentd:8091/",
            "PILOT107_AGENTD_TOKEN": "never-represent-this-secret",
            "PILOT107_AGENTD_MODEL_PROFILE": "campus-default",
            "PILOT107_LLM_API_KEY": "must-not-be-read",
        }
    )

    config = config_from_env(env)

    assert config.base_url == "http://pilot-agentd:8091"
    assert set(env.reads) == {
        "PILOT107_AGENTD_URL",
        "PILOT107_AGENTD_TOKEN",
        "PILOT107_AGENTD_MODEL_PROFILE",
    }
    assert "never-represent-this-secret" not in repr(config)
    assert "must-not-be-read" not in repr(config)


@pytest.mark.parametrize(
    ("overrides", "name"),
    [
        ({"base_url": "ftp://secret.example/token"}, "PILOT107_AGENTD_URL"),
        ({"base_url": "http://user:password@agentd:8091"}, "PILOT107_AGENTD_URL"),
        ({"base_url": "http://agentd:8091/path?token=value"}, "PILOT107_AGENTD_URL"),
        ({"token": ""}, "PILOT107_AGENTD_TOKEN"),
        ({"token": "has whitespace"}, "PILOT107_AGENTD_TOKEN"),
        ({"model_profile_id": "bad/profile"}, "PILOT107_AGENTD_MODEL_PROFILE"),
        ({"timeout_seconds": 0.09}, "timeout_seconds"),
        ({"timeout_seconds": 301}, "timeout_seconds"),
        ({"max_output_tokens": 0}, "max_output_tokens"),
        ({"max_output_tokens": 32_001}, "max_output_tokens"),
    ],
)
def test_config_rejects_unsafe_or_unbounded_values_without_echoing_them(
    overrides: dict[str, Any], name: str
) -> None:
    secret_value = str(next(iter(overrides.values())))

    with pytest.raises(ValueError) as caught:
        _config(**overrides)

    assert name in str(caught.value)
    if secret_value:
        assert secret_value not in str(caught.value)


def test_config_loader_names_missing_agentd_configuration_without_values() -> None:
    with pytest.raises(ValueError) as caught:
        config_from_env({})

    message = str(caught.value)
    assert "PILOT107_AGENTD_URL" in message
    assert "PILOT107_AGENTD_TOKEN" in message
    assert "PILOT107_AGENTD_MODEL_PROFILE" in message


def test_run_turn_sends_bearer_only_in_the_header_and_a_closed_v1_request() -> None:
    captured: dict[str, Any] = {}

    def opener(request: urllib.request.Request, *, timeout: float) -> _Response:
        captured["request"] = request
        captured["timeout"] = timeout
        captured["json"] = json.loads(request.data or b"")
        return _Response(_success_body())

    result = AgentdClient(_config(), opener=opener).run_turn(
        turn_id="turn-1",
        task_kind="interactive",
        prompt_profile_id="hpc-assistant-v1",
        toolset_id="a0-none",
        input_payload={"message": "hello", "context_blocks": []},
    )

    request = captured["request"]
    request_json = captured["json"]
    serialized = json.dumps(request_json)
    assert request.full_url == "http://pilot-agentd:8091/internal/v1/turns"
    assert request.method == "POST"
    assert request.get_header("Authorization") == "Bearer internal-secret"
    assert request.get_header("Content-type") == "application/json"
    assert captured["timeout"] == 30.0
    assert request_json == {
        "schema_version": "pilot107.agent-turn-request/v1",
        "turn_id": "turn-1",
        "task_kind": "interactive",
        "model_profile_id": "campus-default",
        "prompt_profile_id": "hpc-assistant-v1",
        "toolset_id": "a0-none",
        "input": {"message": "hello", "context_blocks": []},
        "checkpoint": None,
        "limits": {"timeout_ms": 30_000, "max_output_tokens": 1200},
        "trace": {"correlation_id": "turn-1"},
    }
    assert "internal-secret" not in serialized
    assert "api_key" not in serialized.lower()
    assert "base_url" not in serialized.lower()
    assert result.result == {"text": "ok"}
    assert result.model == "campus-model"
    assert result.input_tokens == 12
    assert result.output_tokens == 8


def test_stream_durable_turn_sends_the_exact_v2_authority_envelope() -> None:
    captured: dict[str, Any] = {}

    def opener(request: urllib.request.Request, *, timeout: float) -> _Response:
        captured["request"] = request
        captured["timeout"] = timeout
        captured["json"] = json.loads(request.data or b"")
        return _Response(_success_body())

    request = agent_protocol.DurableAgentTurnRequest(
        session_id="session-1",
        turn_id="turn-1",
        owner="alice",
        state_version=3,
        model_profile_id="campus-default",
        message="why is run-1 pending?",
        context_refs=("run:run-1",),
        capability_token="opaque.capability.token",
    )

    events = list(
        AgentdClient(_config(), opener=opener).stream_durable_turn(request)
    )

    assert [event.type for event in events] == ["turn_started", "turn_completed"]
    assert captured["timeout"] == 30.0
    assert captured["request"].full_url == "http://pilot-agentd:8091/internal/v1/turns"
    assert captured["json"] == {
        "schema_version": "pilot107.agent-turn-request/v2",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "owner": "alice",
        "state_version": 3,
        "task_kind": "interactive_readonly",
        "model_profile_id": "campus-default",
        "prompt_profile_id": "hpc-readonly-v1",
        "toolset_id": "a1-readonly",
        "input": {
            "message": "why is run-1 pending?",
            "context_refs": ["run:run-1"],
        },
        "capability_token": "opaque.capability.token",
        "checkpoint": None,
        "limits": {"timeout_ms": 30_000, "max_output_tokens": 1_200},
        "trace": {"correlation_id": "turn-1"},
    }
    assert captured["request"].get_header("Authorization") == "Bearer internal-secret"


def test_repair_profile_uses_the_closed_project_tool_pairing() -> None:
    captured: dict[str, Any] = {}

    def opener(request: urllib.request.Request, *, timeout: float) -> _Response:
        del timeout
        captured["json"] = json.loads(request.data or b"")
        return _Response(_success_body())

    request = agent_protocol.DurableAgentTurnRequest(
        session_id="session-repair",
        turn_id="turn-1",
        owner="alice",
        state_version=3,
        model_profile_id="campus-default",
        message="repair the diagnosed failure in the isolated Workspace",
        context_refs=("run:run-failed", "remediation:remsession-repair"),
        capability_token="opaque.capability.token",
        profile_id="run_diagnosis_repair",
    )

    list(AgentdClient(_config(), opener=opener).stream_durable_turn(request))

    assert captured["json"] == {
        "schema_version": "pilot107.agent-turn-request/v2",
        "session_id": "session-repair",
        "turn_id": "turn-1",
        "owner": "alice",
        "state_version": 3,
        "task_kind": "run_diagnosis_repair",
        "model_profile_id": "campus-default",
        "prompt_profile_id": "run_diagnosis_repair",
        "toolset_id": "a2-project",
        "input": {
            "message": "repair the diagnosed failure in the isolated Workspace",
            "context_refs": ["run:run-failed", "remediation:remsession-repair"],
        },
        "capability_token": "opaque.capability.token",
        "checkpoint": None,
        "limits": {"timeout_ms": 30_000, "max_output_tokens": 1_200},
        "trace": {"correlation_id": "turn-1"},
    }


@pytest.mark.parametrize("profile_id", ["market_application", "template_publication"])
def test_market_profiles_use_the_closed_project_tool_pairing(profile_id: str) -> None:
    captured: dict[str, Any] = {}

    def opener(request: urllib.request.Request, *, timeout: float) -> _Response:
        del timeout
        captured["json"] = json.loads(request.data or b"")
        return _Response(_success_body())

    request = agent_protocol.DurableAgentTurnRequest(
        session_id="session-market",
        turn_id="turn-1",
        owner="alice",
        state_version=3,
        model_profile_id="campus-default",
        message="prepare the market lifecycle in the isolated Workspace",
        context_refs=("market:item-1",),
        capability_token="opaque.capability.token",
        profile_id=profile_id,
    )

    list(AgentdClient(_config(), opener=opener).stream_durable_turn(request))

    assert captured["json"]["task_kind"] == profile_id
    assert captured["json"]["prompt_profile_id"] == profile_id
    assert captured["json"]["toolset_id"] == "a2-project"


def test_stream_durable_turn_rejects_an_invalid_request_before_http() -> None:
    client = AgentdClient(
        _config(),
        opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("HTTP must not be called")
        ),
    )
    request = agent_protocol.DurableAgentTurnRequest(
        session_id="session-1",
        turn_id="turn-1",
        owner="alice",
        state_version=-1,
        model_profile_id="campus-default",
        message="why pending?",
        context_refs=(),
        capability_token="opaque.capability.token",
    )

    with pytest.raises(AgentdClientError) as caught:
        list(client.stream_durable_turn(request))

    assert caught.value.code == "invalid_request"


def test_default_opener_is_resolved_at_client_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[urllib.request.Request] = []

    def opener(request: urllib.request.Request, *, timeout: float) -> _Response:
        del timeout
        calls.append(request)
        return _Response(_success_body())

    monkeypatch.setattr("pilot107.agent.client.urllib.request.urlopen", opener)
    client = AgentdClient(_config())

    client.run_turn(
        turn_id="turn-1",
        task_kind="interactive",
        prompt_profile_id="hpc-assistant-v1",
        toolset_id="a0-none",
        input_payload={"message": "hello", "context_blocks": []},
    )

    assert len(calls) == 1


def test_stream_turn_forwards_events_and_closes_after_a_callback_error() -> None:
    response = _Response(_success_body())
    seen: list[str] = []

    def callback(event: AgentTurnEvent) -> None:
        seen.append(event.type)
        raise LookupError("consumer stopped")

    client = AgentdClient(_config(), opener=lambda *_args, **_kwargs: response)

    with pytest.raises(LookupError, match="consumer stopped"):
        list(
            client.stream_turn(
                turn_id="turn-1",
                task_kind="interactive",
                prompt_profile_id="hpc-assistant-v1",
                toolset_id="a0-none",
                input_payload={"message": "hello", "context_blocks": []},
                on_event=callback,
            )
        )

    assert seen == ["turn_started"]
    assert response.closed is True


def test_turn_failed_raises_a_stable_redacted_agentd_error() -> None:
    body = b"".join(
        [
            _event(
                1,
                "turn_started",
                {"model_profile_id": "campus-default", "task_kind": "interactive"},
            ),
            _event(
                2,
                "turn_failed",
                {
                    "error": {
                        "code": "provider_rate_limited",
                        "retryable": True,
                        "message": "upstream included sensitive-prompt-here",
                        "provider_status": 429,
                    }
                },
            ),
        ]
    )
    client = AgentdClient(_config(), opener=lambda *_args, **_kwargs: _Response(body))

    with pytest.raises(AgentdClientError) as caught:
        client.run_turn(
            turn_id="turn-1",
            task_kind="interactive",
            prompt_profile_id="hpc-assistant-v1",
            toolset_id="a0-none",
            input_payload={"message": "hello", "context_blocks": []},
        )

    assert caught.value.code == "provider_rate_limited"
    assert caught.value.retryable is True
    assert caught.value.provider_status == 429
    assert "sensitive-prompt-here" not in str(caught.value)


@pytest.mark.parametrize(
    ("failure", "retryable"),
    [
        (urllib.error.URLError("dns exposed-host.example"), True),
        (TimeoutError("token=secret"), True),
        (ConnectionResetError("peer secret"), True),
    ],
)
def test_transport_failures_map_without_leaking_transport_details(
    failure: Exception, retryable: bool
) -> None:
    def opener(*_args: object, **_kwargs: object) -> _Response:
        raise failure

    client = AgentdClient(_config(), opener=opener)

    with pytest.raises(AgentdClientError) as caught:
        list(
            client.stream_turn(
                turn_id="turn-1",
                task_kind="interactive",
                prompt_profile_id="hpc-assistant-v1",
                toolset_id="a0-none",
                input_payload={"message": "hello", "context_blocks": []},
            )
        )

    assert caught.value.code == "transport_error"
    assert caught.value.retryable is retryable
    assert str(failure) not in str(caught.value)
    rendered = "".join(
        traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__)
    )
    assert str(failure) not in rendered


def test_regular_json_http_error_maps_stable_code_and_closes_response() -> None:
    headers = Message()
    headers["Content-Type"] = "application/json; charset=utf-8"
    error = urllib.error.HTTPError(
        "http://pilot-agentd:8091/internal/v1/turns",
        409,
        "body includes secret",
        headers,
        io.BytesIO(b'{"error":{"code":"turn_active"}}'),
    )
    closed = False
    original_close = error.close

    def close() -> None:
        nonlocal closed
        closed = True
        original_close()

    error.close = close  # type: ignore[method-assign]
    client = AgentdClient(_config(), opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(AgentdClientError) as caught:
        list(
            client.stream_turn(
                turn_id="turn-1",
                task_kind="interactive",
                prompt_profile_id="hpc-assistant-v1",
                toolset_id="a0-none",
                input_payload={"message": "hello", "context_blocks": []},
            )
        )

    assert caught.value.code == "turn_active"
    assert caught.value.http_status == 409
    assert "secret" not in str(caught.value)
    rendered = "".join(
        traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__)
    )
    assert "body includes secret" not in rendered
    assert closed is True


def test_unreadable_http_error_body_still_maps_and_closes_without_leaking() -> None:
    headers = Message()
    headers["Content-Type"] = "application/json; charset=utf-8"
    error = urllib.error.HTTPError(
        "http://pilot-agentd:8091/internal/v1/turns",
        500,
        "safe status",
        headers,
        io.BytesIO(b""),
    )
    closed = False
    original_close = error.close

    def unreadable(_size: int = -1) -> bytes:
        raise OSError("sensitive response transport detail")

    def close() -> None:
        nonlocal closed
        closed = True
        original_close()

    error.read = unreadable  # type: ignore[method-assign]
    error.close = close  # type: ignore[method-assign]
    client = AgentdClient(_config(), opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(AgentdClientError) as caught:
        list(
            client.stream_turn(
                turn_id="turn-1",
                task_kind="interactive",
                prompt_profile_id="hpc-assistant-v1",
                toolset_id="a0-none",
                input_payload={"message": "hello", "context_blocks": []},
            )
        )

    assert caught.value.code == "http_error"
    assert "sensitive" not in str(caught.value)
    assert closed is True


@pytest.mark.parametrize(
    "content_type",
    [
        "application/json",
        "application/x-ndjson",
        "application/x-ndjson; charset=latin-1",
        "application/x-ndjson; charset=utf-8; boundary=unexpected",
    ],
)
def test_stream_turn_requires_the_exact_ndjson_utf8_content_type(content_type: str) -> None:
    client = AgentdClient(
        _config(),
        opener=lambda *_args, **_kwargs: _Response(_success_body(), content_type=content_type),
    )

    with pytest.raises(AgentdClientError) as caught:
        list(
            client.stream_turn(
                turn_id="turn-1",
                task_kind="interactive",
                prompt_profile_id="hpc-assistant-v1",
                toolset_id="a0-none",
                input_payload={"message": "hello", "context_blocks": []},
            )
        )

    assert caught.value.code == "protocol_error"


def test_request_rejects_task_pairing_and_recursive_secret_fields_before_http() -> None:
    def opener(*_args: object, **_kwargs: object) -> _Response:
        raise AssertionError("HTTP must not be called")

    client = AgentdClient(_config(), opener=opener)

    with pytest.raises(AgentdClientError) as pairing:
        list(
            client.stream_turn(
                task_kind="explain",
                prompt_profile_id="hpc-assistant-v1",
                toolset_id="a0-none",
                input_payload={},
            )
        )
    with pytest.raises(AgentdClientError) as injection:
        list(
            client.stream_turn(
                task_kind="interactive",
                prompt_profile_id="hpc-assistant-v1",
                toolset_id="a0-none",
                input_payload={
                    "message": "hello",
                    "context_blocks": [],
                    "nested": {"Authorization": "Bearer leaked"},
                },
            )
        )

    assert pairing.value.code == "invalid_request"
    assert injection.value.code == "invalid_request"
    assert "leaked" not in str(injection.value)


@pytest.mark.parametrize("status", ["accepted", "not_active"])
def test_cancel_turn_posts_the_explicit_escaped_turn_id(status: str) -> None:
    captured: dict[str, Any] = {}

    def opener(request: urllib.request.Request, *, timeout: float) -> _Response:
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response(
            json.dumps({"status": status}).encode(),
            content_type="application/json; charset=utf-8",
        )

    result = AgentdClient(_config(), opener=opener).cancel_turn("turn:to.cancel")

    request = captured["request"]
    assert result == status
    assert request.full_url.endswith("/internal/v1/turns/turn%3Ato.cancel/cancel")
    assert request.method == "POST"
    assert request.data == b"{}"
    assert request.get_header("Authorization") == "Bearer internal-secret"
    assert captured["timeout"] == 30.0


@pytest.mark.parametrize(
    "body",
    [
        b'{"status":"terminal"}',
        b'{"status":"accepted","extra":true}',
        b"[]",
        b"\xff",
        b"{",
        b" " * (64 * 1024 + 1),
    ],
)
def test_cancel_turn_rejects_invalid_or_oversized_json(body: bytes) -> None:
    client = AgentdClient(
        _config(),
        opener=lambda *_args, **_kwargs: _Response(
            body, content_type="application/json; charset=utf-8"
        ),
    )

    with pytest.raises(AgentdClientError) as caught:
        client.cancel_turn("turn-1")

    assert caught.value.code == "protocol_error"
