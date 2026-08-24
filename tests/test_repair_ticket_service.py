from __future__ import annotations

from pathlib import Path

from pilot107.agent.project import ExperimentProjectOrigin, ProjectSource
from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.workspace import AgentWorkspaceRecord, WorkspaceSnapshot
from pilot107.core.remediation import RemediationBudget, RemediationState
from pilot107.core.remediation_store import RemediationStore
from pilot107.core.repair_ticket_store import RepairTicketStore
from pilot107.core.run_store import RunStore
from pilot107.services.repair_ticket_service import RepairTicketService


def test_session_ticket_links_existing_unified_failed_run_project(tmp_path: Path) -> None:
    database = tmp_path / "pilot107.db"
    runs = RunStore(database)
    runs.create_run(
        run_id="run-failed",
        owner="alice",
        workdir="/public/home/alice/failed",
        script="python train.py",
        contract_id="contract-failed",
    )
    remediations = RemediationStore(database)
    remediations.create_session(
        session_id="remsession-ticket",
        owner="alice",
        request_key="repair-ticket-session",
        state=RemediationState.READY,
        source_run_id="run-failed",
        source_contract_id="contract-failed",
        source_diagnosis_digest="d" * 64,
        source_evidence_digest="e" * 64,
        automation_policy="manual_approval",
        budget=RemediationBudget(),
    )
    projects = SQLiteProjectStore(database)
    project = projects.create_project(
        owner="alice",
        origin=ExperimentProjectOrigin.FAILED_RUN,
        goal="repair train.py",
        request_key="repair-project",
        source=ProjectSource(
            kind="failed_run",
            ref_id="run-failed",
            cluster_path=None,
        ),
    )
    workspace = projects.save_workspace(
        AgentWorkspaceRecord(
            workspace_id="workspace-repair-ticket",
            project_id=project.project_id,
            owner="alice",
            local_root=str(tmp_path / "workspaces" / "alice" / "repair-ticket"),
            snapshot=WorkspaceSnapshot(
                source_ref="/public/home/alice/failed",
                digest="a" * 64,
                entries=(),
                captured_at="2026-08-25T00:00:00+00:00",
            ),
            created_at="2026-08-25T00:00:00+00:00",
            updated_at="2026-08-25T00:00:00+00:00",
        )
    )
    service = RepairTicketService(
        run_store=runs,
        repair_ticket_store=RepairTicketStore(database),
        remediation_store=remediations,
        project_store=projects,
    )

    ticket, created = service.create_from_session(
        "remsession-ticket",
        owner="alice",
        request_key="legacy-handoff",
    )

    assert created is True
    assert ticket.code_context == {
        "unified_project": {
            "project_id": project.project_id,
            "workspace_id": workspace.workspace_id,
            "source_run_id": "run-failed",
        }
    }
