from __future__ import annotations

from typing import Any

import pytest

from pilot107.agent.config import AgentdClientConfig
from pilot107.agent.protocol import AgentdTurnResult
from pilot107.agent.providers import AgentdConstrainedProvider

_DIGEST = "c" * 64


class RecordingAgentdClient:
    def __init__(self, result: dict[str, Any]) -> None:
        self.config = AgentdClientConfig(
            base_url="http://pilot-agentd:8091",
            token="internal-secret",
            model_profile_id="campus-default",
        )
        self.calls: list[dict[str, Any]] = []
        self.result = result

    def run_turn(self, **kwargs: Any) -> AgentdTurnResult:
        self.calls.append(kwargs)
        return AgentdTurnResult(
            result=self.result,
            provider="campus-openai-compatible",
            model="campus-model",
            model_profile_id="campus-default",
            input_tokens=12,
            output_tokens=8,
            cache_read_tokens=None,
            cache_write_tokens=None,
            provider_calls=1,
            checkpoint_digest=_DIGEST,
            duration_ms=42,
            checkpoint=None,
        )


@pytest.mark.parametrize(
    ("task_kind", "prompt_profile_id", "toolset_id"),
    [
        ("explain", "agent-explain-v1", "emit-explanation-v1"),
        ("contract_patch", "contract-patch-v1", "emit-contract-patch-v1"),
        ("remediation_plan", "remediation-plan-v1", "emit-remediation-plan-v1"),
    ],
)
def test_constrained_provider_invokes_the_registered_agentd_profile(
    task_kind: str,
    prompt_profile_id: str,
    toolset_id: str,
) -> None:
    payload = {"facts": []}
    client = RecordingAgentdClient(result={"summary": "摘要"})
    provider = AgentdConstrainedProvider(client)  # type: ignore[arg-type]

    result = provider.invoke(task_kind, payload)

    assert result.result == {"summary": "摘要"}
    assert client.calls == [
        {
            "task_kind": task_kind,
            "prompt_profile_id": prompt_profile_id,
            "toolset_id": toolset_id,
            "input_payload": payload,
        }
    ]


def test_constrained_provider_rejects_an_unknown_task_before_calling_agentd() -> None:
    client = RecordingAgentdClient(result={})
    provider = AgentdConstrainedProvider(client)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="unsupported constrained task"):
        provider.invoke("arbitrary", {})

    assert client.calls == []
