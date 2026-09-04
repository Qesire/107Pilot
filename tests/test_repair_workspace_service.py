from __future__ import annotations

import json
from pathlib import Path

import pytest

from pilot107.api.run_workspace_routes import RunWorkspaceRoutes
from pilot107.core.remediation import RemediationBudget, RemediationState
from pilot107.core.remediation_store import RemediationStore
from pilot107.core.repair_ticket import RepairTicket, RepairTicketState
from pilot107.core.repair_ticket_store import RepairTicketStore
from pilot107.core.run_store import RunStore
from pilot107.core.states import RunState
from pilot107.services.run_workspace_service import RunWorkspaceService


def _service(tmp_path: Path) -> tuple[RunWorkspaceService, RunStore, RemediationStore, RepairTicketStore]:
    database = tmp_path / "pilot107.db"
    runs = RunStore(database)
    remediation = RemediationStore(database)
    tickets = RepairTicketStore(database)
    service = RunWorkspaceService(
        store=runs,
        remediation_store=remediation,
        repair_ticket_store=tickets,
    )
    return service, runs, remediation, tickets


def _failed_run(runs: RunStore) -> None:
    runs.create_run(
        run_id="run-failed",
        owner="alice",
        workdir="/public/home/alice/exp",
        script="python train.py",
        contract_id="contract-1",
    )
    runs.update_state("run-failed", RunState.FAILED, event_type="run.failed")
    runs.replace_diagnoses(
        "run-failed",
        [
            {
                "diagnosis_id": "diag-1",
                "rule_id": "PYTHON.MISSING_MODULE",
                "severity": "error",
                "summary": "缺少运行依赖",
                "evidence_refs": ["evidence://run-failed/stderr"],
                "retryable": True,
                "confidence": "high",
                "category": "runtime",
                "stage": "execution",
            }
        ],
    )


def test_repair_workspace_projects_existing_authorities_without_raw_agent_payload(
    tmp_path: Path,
) -> None:
    service, runs, remediation, tickets = _service(tmp_path)
    _failed_run(runs)
    source = runs.get_run("run-failed")

    remediation.create_session(
        session_id="remediation-1",
        owner="alice",
        request_key="repair-run-failed",
        state=RemediationState.AWAITING_APPROVAL,
        source_run_id="run-failed",
        source_contract_id="contract-1",
        source_diagnosis_digest="d" * 64,
        source_evidence_digest="e" * 64,
        automation_policy="approval_required",
        budget=RemediationBudget(),
        provider="campus",
    )
    runs.create_agent_advice(
        advice_id="advice-1",
        run_id="run-failed",
        owner="alice",
        request_key="advice-run-failed",
        state="ready",
        source_run_updated_at=source.updated_at,
        evidence_bundle_sha256="a" * 64,
        provider="campus",
        model="test-model",
        payload={"analysis": "private-model-thought", "actions": []},
    )
    execution, _ = runs.claim_agent_action_execution(
        execution_id="agent-execution-1",
        advice_id="advice-1",
        action_id="action-1",
        owner="alice",
        submit_requested=False,
    )
    assert execution.state == "executing"
    tickets.create_ticket(
        RepairTicket(
            ticket_id="ticket-1",
            owner="alice",
            state=RepairTicketState.OPEN,
            source_run_id="run-failed",
            source_contract_id="contract-1",
            session_id="remediation-1",
            diagnosis_ids=("diag-1",),
            requested_change="修复缺失依赖",
            code_context={"snippet": "private-source-body"},
        )
    )
    runs.create_run(
        run_id="run-derived",
        owner="alice",
        workdir="/public/home/alice/exp",
        script="python train.py",
        contract_id="contract-2",
        parent_run_id="run-failed",
        lineage_reason="manual_retry",
    )

    payload = service.get_repair("run-failed", owner="alice")

    assert payload["schema_version"] == "pilot107.repair-workspace/v1"
    assert payload["source_run"]["run_id"] == "run-failed"
    assert payload["diagnoses"][0]["evidence_refs"] == [
        "evidence://run-failed/stderr"
    ]
    assert payload["remediation_sessions"][0]["state"] == "awaiting_approval"
    assert payload["agent"]["advice"][0] == {
        "advice_id": "advice-1",
        "state": "ready",
        "version": 1,
        "provider": "campus",
        "model": "test-model",
        "evidence_bundle_sha256": "a" * 64,
        "source_run_updated_at": source.updated_at,
        "created_at": payload["agent"]["advice"][0]["created_at"],
        "updated_at": payload["agent"]["advice"][0]["updated_at"],
    }
    assert payload["agent"]["executions"][0]["execution_id"] == "agent-execution-1"
    assert payload["repair_tickets"][0]["ticket_id"] == "ticket-1"
    assert payload["derived_runs"][0]["run_id"] == "run-derived"
    assert payload["status"]["has_repair_activity"] is True
    assert payload["status"]["awaiting_approval"] is True
    assert payload["next_action"]["kind"] == "review_proposal"

    encoded = json.dumps(payload, ensure_ascii=False)
    assert "private-model-thought" not in encoded
    assert "private-source-body" not in encoded


def test_repair_workspace_is_owner_scoped(tmp_path: Path) -> None:
    service, runs, _, _ = _service(tmp_path)
    _failed_run(runs)

    with pytest.raises(PermissionError):
        service.get_repair("run-failed", owner="bob")


def test_repair_workspace_starts_from_evidence_bound_diagnosis(tmp_path: Path) -> None:
    service, runs, _, _ = _service(tmp_path)
    _failed_run(runs)

    payload = service.get_repair("run-failed", owner="alice")

    assert payload["status"] == {
        "has_repair_activity": False,
        "awaiting_approval": False,
        "has_derived_run": False,
        "verified_success": False,
    }
    assert payload["next_action"]["kind"] == "start_repair"
    assert payload["diagnoses"][0]["diagnosis_id"] == "diag-1"


def test_repair_workspace_route_has_distinct_contract(tmp_path: Path) -> None:
    service, runs, _, _ = _service(tmp_path)
    _failed_run(runs)
    routes = RunWorkspaceRoutes(service)

    response = routes.handle_get(
        ["runs", "run-failed", "repair-workspace"],
        params={},
        identity=None,
    )
    assert response is not None
    assert response.status == 200
    assert response.payload["schema_version"] == "pilot107.repair-workspace/v1"

    invalid = routes.handle_get(
        ["runs", "run-failed", "repair-workspace"],
        params={"unexpected": ["1"]},
        identity=None,
    )
    assert invalid is not None
    assert invalid.status == 400
    assert invalid.payload["error"]["code"] == "REPAIR_WORKSPACE.INVALID_QUERY"
