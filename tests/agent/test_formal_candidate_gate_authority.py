from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pilot107.agent.tasks import AgentTaskGateState, AgentTaskResult
from tests.agent.test_a4_vertical import A4Harness, _OneTaskStore


@pytest.fixture
def harness(tmp_path: Path) -> A4Harness:
    return A4Harness(tmp_path)


def _candidate(harness: A4Harness):
    return harness.service.prepare_formal_run_candidate(
        project_id=harness.project_id,
        workspace_id="workspace-a4",
        change_set_id="changeset-a4",
        session_id=harness.session.session_id,
        validation_task_id=harness.validation_task.task_id,
        owner="alice",
    )


def test_formal_candidate_rejects_legacy_success_without_terminal_gate(
    harness: A4Harness,
) -> None:
    task = replace(
        harness.validation_task,
        gate_state=AgentTaskGateState.COMPLETED,
        gate_receipt=None,
        legacy_gate_unverified=True,
    )
    harness.service.agent_task_store = _OneTaskStore(task)

    with pytest.raises(ValueError, match="Evidence gate"):
        _candidate(harness)


def test_formal_candidate_rejects_nonterminal_gate_even_with_receipt(
    harness: A4Harness,
) -> None:
    task = replace(
        harness.validation_task,
        gate_state=AgentTaskGateState.AWAITING_INTEGRITY,
    )
    harness.service.agent_task_store = _OneTaskStore(task)

    with pytest.raises(ValueError, match="Evidence gate"):
        _candidate(harness)


def test_formal_candidate_rejects_legacy_result_that_disagrees_with_gate(
    harness: A4Harness,
) -> None:
    task = replace(
        harness.validation_task,
        result=AgentTaskResult.succeeded(
            ("evidence://runs/other-run/results/not-authoritative.json",)
        ),
    )
    harness.service.agent_task_store = _OneTaskStore(task)

    with pytest.raises(ValueError, match="terminal Evidence gate"):
        _candidate(harness)


def test_formal_candidate_uses_terminal_gate_evidence_refs(
    harness: A4Harness,
) -> None:
    candidate = _candidate(harness)

    assert harness.validation_task.gate_receipt is not None
    assert candidate.validation_evidence_refs == harness.validation_task.gate_receipt.evidence_refs
