from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

from pilot107.agent.config import AgentdClientConfig
from pilot107.agent.protocol import AgentdClientError, AgentdTurnResult, AgentTurnEvent

ROOT = Path(__file__).resolve().parents[1]
FAUX_SMOKE = ROOT / "scripts" / "smoke-pilot-agentd-faux.py"
CAMPUS_SMOKE = ROOT / "scripts" / "smoke-campus-llm.py"
AGENTD_CHECK = ROOT / "scripts" / "check-pilot-agentd.sh"


def _terminal(result: Any) -> AgentdTurnResult:
    return AgentdTurnResult(
        result=result,
        provider="faux-default",
        model="faux-1",
        model_profile_id="faux-default",
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        provider_calls=1,
        checkpoint_digest="a" * 64,
        duration_ms=1,
        checkpoint={"smoke": True},
    )


class _ScriptedClient:
    def __init__(self) -> None:
        self.config = AgentdClientConfig(
            base_url="http://pilot-agentd:8091",
            token="test-token",
            model_profile_id="faux-default",
        )
        self.calls: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self._responses = [
            _terminal("interactive ok"),
            _terminal(
                {
                    "summary": "grounded",
                    "narrative": "evidence-bound",
                    "recommendations": ["inspect logs"],
                    "warnings": [],
                    "citations": [
                        {
                            "fact_id": "fact-smoke",
                            "evidence_object_ids": ["object-smoke"],
                        }
                    ],
                }
            ),
            _terminal(
                {
                    "suggested_patch": {"resources.cpus_per_task": 2},
                    "explanation_zh": "确认后调整。",
                }
            ),
            _terminal(
                {
                    "schema_version": "pilot107.remediation-plan/v1",
                    "summary": "bounded plan",
                    "fact_ids": ["fact-smoke"],
                    "required_inputs": [],
                    "proposals": [],
                    "stop_conditions": ["stop when evidence is insufficient"],
                }
            ),
        ]

    def run_turn(self, **kwargs: Any) -> AgentdTurnResult:
        self.calls.append(kwargs)
        if len(self.calls) == 5:
            callback = kwargs["on_event"]
            callback(
                AgentTurnEvent(
                    turn_id=kwargs["turn_id"],
                    sequence=2,
                    type="message_delta",
                    timestamp="2026-08-14T00:00:00Z",
                    payload={"delta": "partial"},
                )
            )
            raise AgentdClientError(
                "cancelled",
                code="aborted",
                checkpoint={"smoke": "checkpoint"},
            )
        if len(self.calls) == 6:
            return _terminal("resumed ok")
        return self._responses.pop(0)

    def cancel_turn(self, turn_id: str) -> str:
        self.cancelled.append(turn_id)
        return "accepted"


def _load_faux_smoke() -> ModuleType:
    assert FAUX_SMOKE.is_file()
    spec = importlib.util.spec_from_file_location("pilot107_faux_smoke", FAUX_SMOKE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_faux_smoke_exercises_four_tasks_and_cancel_restore() -> None:
    module = _load_faux_smoke()
    client = _ScriptedClient()

    summary = module.run_faux_smoke(client)

    assert [call["task_kind"] for call in client.calls] == [
        "interactive",
        "explain",
        "contract_patch",
        "remediation_plan",
        "interactive",
        "interactive",
    ]
    assert client.cancelled == [client.calls[4]["turn_id"]]
    assert client.calls[5]["checkpoint"] == {"smoke": "checkpoint"}
    assert summary["contract_patch"]["needs_user_confirmation"] is True
    assert summary["resumed_text"] == "resumed ok"


def test_campus_smoke_safely_skips_without_agentd_configuration() -> None:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("PILOT107_AGENTD_")
    }

    completed = subprocess.run(
        ["python3", str(CAMPUS_SMOKE)],
        cwd=ROOT,
        env={**environment, "PYTHONPATH": str(ROOT / "src")},
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == "SKIP: pilot-agentd or campus profile is not configured"
    assert completed.stderr == ""
    assert "PILOT107_LLM_API_KEY" not in CAMPUS_SMOKE.read_text(encoding="utf-8")


def test_consolidated_agentd_check_is_part_of_local_ci() -> None:
    assert AGENTD_CHECK.is_file()
    assert AGENTD_CHECK.stat().st_mode & 0o111
    check = AGENTD_CHECK.read_text(encoding="utf-8")
    assert "node:22.19.0-bookworm-slim" in check
    assert "npm run check" in check
    assert "tests/test_architecture_boundaries.py" in check
    assert "tests/test_pilot_agentd_vertical.py" in check

    local_ci = (ROOT / "scripts" / "check-ci-local.sh").read_text(encoding="utf-8")
    assert "bash scripts/check-pilot-agentd.sh" in local_ci
