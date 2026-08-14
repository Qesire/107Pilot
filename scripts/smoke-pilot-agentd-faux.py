#!/usr/bin/env python3
"""Exercise the complete Python-to-Agentd boundary with deterministic faux output."""

from __future__ import annotations

import sys
import uuid
from typing import Any

from pilot107.agent.client import AgentdClient
from pilot107.agent.config import config_from_env
from pilot107.agent.protocol import AgentdClientError
from pilot107.core.agent import AgentExplanation, AgentFact, OpenAICompatibleLLMProvider
from pilot107.core.remediation_llm import (
    OpenAICompatibleRemediationPlanProvider,
    RemediationPlanningContext,
    parse_remediation_plan,
    validate_remediation_plan,
)


def _fact_fixture() -> AgentFact:
    return AgentFact(
        fact_id="fact-smoke",
        statement="The deterministic smoke fixture exited with code 1.",
        evidence_refs=("evidence://run-faux-smoke/stderr",),
        confidence="high",
        evidence_object_ids=("object-smoke",),
    )


def _interactive(client: AgentdClient) -> str:
    terminal = client.run_turn(
        task_kind="interactive",
        prompt_profile_id="hpc-assistant-v1",
        toolset_id="a0-none",
        input_payload={"message": "hello", "context_blocks": []},
    )
    if not isinstance(terminal.result, str) or not terminal.result.strip():
        raise AssertionError("interactive faux result is empty")
    return terminal.result


def _explain(client: AgentdClient, fact: AgentFact) -> str:
    provider = OpenAICompatibleLLMProvider(client=client)
    explanation = provider.explain(
        AgentExplanation(
            run_id="run-faux-smoke",
            provider="none",
            status="explained",
            summary="The smoke fixture failed deterministically.",
            facts=(fact,),
            diagnoses=(),
        )
    )
    if not explanation.summary.strip() or not explanation.narrative.strip():
        raise AssertionError("explain faux result is incomplete")
    if not explanation.citations or explanation.citations[0].fact_id != fact.fact_id:
        raise AssertionError("explain faux result is not evidence-bound")
    return explanation.summary


def _contract_patch(client: AgentdClient) -> dict[str, Any]:
    provider = OpenAICompatibleLLMProvider(client=client)
    result = provider.suggest_contract_patch(
        current_contract={"resources": {"cpus_per_task": 1}},
        recipe_version_id="recipe-faux-smoke",
        user_intent="use two CPUs per task",
    )
    if result["needs_user_confirmation"] is not True or not result["suggested_patch"]:
        raise AssertionError("contract patch did not preserve the confirmation boundary")
    return result


def _remediation(client: AgentdClient, fact: AgentFact) -> str:
    context = RemediationPlanningContext(run_id="run-faux-smoke", facts=(fact,))
    provider = OpenAICompatibleRemediationPlanProvider(client=client)
    plan = parse_remediation_plan(provider.propose(context))
    validate_remediation_plan(plan, context)
    return plan.summary


def _cancel_restore(client: AgentdClient) -> str:
    cancel_turn_id = f"faux-cancel-{uuid.uuid4().hex}"
    cancel_status: str | None = None

    def cancel_after_delta(event: Any) -> None:
        nonlocal cancel_status
        if event.type == "message_delta" and cancel_status is None:
            cancel_status = client.cancel_turn(cancel_turn_id)

    try:
        client.run_turn(
            turn_id=cancel_turn_id,
            task_kind="interactive",
            prompt_profile_id="hpc-assistant-v1",
            toolset_id="a0-none",
            input_payload={"message": "produce a cancellable response", "context_blocks": []},
            on_event=cancel_after_delta,
        )
    except AgentdClientError as error:
        if error.code != "aborted" or error.checkpoint is None:
            raise AssertionError("cancelled Turn did not return a safe checkpoint") from error
        checkpoint = error.checkpoint
    else:
        raise AssertionError("cancellable Turn completed before cancellation")

    if cancel_status != "accepted":
        raise AssertionError("Agentd did not accept cancellation for the active Turn")

    terminal = client.run_turn(
        turn_id=f"faux-resume-{uuid.uuid4().hex}",
        task_kind="interactive",
        prompt_profile_id="hpc-assistant-v1",
        toolset_id="a0-none",
        input_payload={"message": "resume", "context_blocks": []},
        checkpoint=checkpoint,
    )
    if not isinstance(terminal.result, str) or not terminal.result.strip():
        raise AssertionError("restored faux Turn is empty")
    return terminal.result


def run_faux_smoke(client: AgentdClient) -> dict[str, Any]:
    if client.config.model_profile_id != "faux-default":
        raise AssertionError("faux smoke requires model profile faux-default")
    fact = _fact_fixture()
    return {
        "interactive_text": _interactive(client),
        "explanation_summary": _explain(client, fact),
        "contract_patch": _contract_patch(client),
        "remediation_summary": _remediation(client, fact),
        "resumed_text": _cancel_restore(client),
    }


def main() -> int:
    try:
        summary = run_faux_smoke(AgentdClient(config_from_env()))
    except (AgentdClientError, AssertionError, ValueError) as error:
        print(f"pilot-agentd faux smoke failed: {error}", file=sys.stderr)
        return 1

    print(
        "pilot-agentd faux smoke ok: "
        f"interactive_chars={len(summary['interactive_text'])} "
        f"explain_chars={len(summary['explanation_summary'])} "
        f"patch_fields={len(summary['contract_patch']['suggested_patch'])} "
        f"remediation_chars={len(summary['remediation_summary'])} "
        f"resumed_chars={len(summary['resumed_text'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
