#!/usr/bin/env python3
"""Smoke an optional campus model strictly through the internal Agentd boundary."""

from __future__ import annotations

import os
import sys
from typing import Any

from pilot107.agent.client import AgentdClient
from pilot107.agent.config import config_from_env
from pilot107.agent.protocol import AgentdClientError, AgentdTurnResult

REQUIRED_ENV = (
    "PILOT107_AGENTD_URL",
    "PILOT107_AGENTD_TOKEN",
    "PILOT107_AGENTD_MODEL_PROFILE",
)
SKIP_MESSAGE = "SKIP: pilot-agentd or campus profile is not configured"


def _explain_fixture() -> dict[str, Any]:
    return {
        "run_id": "run-campus-smoke",
        "status": "FAILED",
        "deterministic_summary": "The fixture exited with code 1.",
        "facts": [
            {
                "fact_id": "fact-campus-smoke",
                "statement": "The process exited with code 1.",
                "evidence_refs": ["evidence://run-campus-smoke/stderr"],
                "evidence_object_ids": ["object-campus-smoke"],
                "confidence": "high",
            }
        ],
        "bound_evidence": [
            {
                "object_id": "object-campus-smoke",
                "evidence_ref": "evidence://run-campus-smoke/stderr",
                "logical_path": "logs/stderr.tail.txt",
                "sha256": "a" * 64,
                "mime_type": "text/plain",
                "trust": "untrusted",
                "snippet": "process exited with code 1",
                "truncated": False,
                "redactions": [],
            }
        ],
        "code_context": None,
        "diagnoses": [],
        "required_output": {
            "summary": "one grounded sentence",
            "narrative": "short Chinese explanation",
            "recommendations": "array of concrete next actions",
            "warnings": "array of uncertainty notes",
            "citations": "one item per fact_id",
        },
    }


def _validate_result(terminal: AgentdTurnResult, expected_profile: str) -> None:
    if terminal.model_profile_id != expected_profile or not terminal.provider or not terminal.model:
        raise AssertionError("provider/model metadata is incomplete")
    if terminal.provider_calls < 1:
        raise AssertionError("provider call count is unavailable")
    usage = (
        terminal.input_tokens,
        terminal.output_tokens,
        terminal.cache_read_tokens,
        terminal.cache_write_tokens,
    )
    if any(value is not None and value < 0 for value in usage):
        raise AssertionError("usage must be unavailable or non-negative")
    result = terminal.result
    if not isinstance(result, dict):
        raise AssertionError("explain result is not structured")
    if not isinstance(result.get("summary"), str) or not result["summary"].strip():
        raise AssertionError("explain summary is empty")
    if not isinstance(result.get("narrative"), str) or not result["narrative"].strip():
        raise AssertionError("explain narrative is empty")
    if not isinstance(result.get("recommendations"), list):
        raise AssertionError("explain recommendations are unavailable")
    citations = result.get("citations")
    if not isinstance(citations, list) or not citations:
        raise AssertionError("explain citations are unavailable")
    first = citations[0]
    if not isinstance(first, dict) or first.get("fact_id") != "fact-campus-smoke":
        raise AssertionError("explain citation does not bind the supplied fact")


def main() -> int:
    if (
        any(not os.environ.get(name, "").strip() for name in REQUIRED_ENV)
        or os.environ.get("PILOT107_AGENTD_MODEL_PROFILE") == "faux-default"
    ):
        print(SKIP_MESSAGE)
        return 0

    try:
        config = config_from_env()
        terminal = AgentdClient(config).run_turn(
            task_kind="explain",
            prompt_profile_id="agent-explain-v1",
            toolset_id="emit-explanation-v1",
            input_payload=_explain_fixture(),
        )
        _validate_result(terminal, config.model_profile_id)
    except (AgentdClientError, AssertionError, ValueError) as error:
        print(f"campus llm smoke failed: {error}", file=sys.stderr)
        return 1

    usage_available = terminal.input_tokens is not None and terminal.output_tokens is not None
    print(
        "campus llm smoke ok: "
        f"provider={terminal.provider} model={terminal.model} "
        f"provider_calls={terminal.provider_calls} "
        f"usage_available={usage_available}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
