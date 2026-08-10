"""Business-task adapters over the strict pilot-agentd client."""

from __future__ import annotations

from typing import Any

from pilot107.agent.client import AgentdClient
from pilot107.agent.protocol import AgentdTurnResult

_TASK_PROFILES: dict[str, tuple[str, str]] = {
    "explain": ("agent-explain-v1", "emit-explanation-v1"),
    "contract_patch": ("contract-patch-v1", "emit-contract-patch-v1"),
    "remediation_plan": ("remediation-plan-v1", "emit-remediation-plan-v1"),
}


class AgentdConstrainedProvider:
    """Invoke one of Agentd's registered side-effect-free result tools."""

    def __init__(self, client: AgentdClient) -> None:
        self.client = client

    def invoke(self, task_kind: str, input_payload: dict[str, Any]) -> AgentdTurnResult:
        profile = _TASK_PROFILES.get(task_kind)
        if profile is None:
            raise ValueError("unsupported constrained task")
        prompt_profile_id, toolset_id = profile
        return self.client.run_turn(
            task_kind=task_kind,
            prompt_profile_id=prompt_profile_id,
            toolset_id=toolset_id,
            input_payload=input_payload,
        )
