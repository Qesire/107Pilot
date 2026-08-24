from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path

from pilot107.adapters.slurm import FileEntry, FileStat
from pilot107.agent.project import ExperimentProjectOrigin
from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.sandbox import SandboxExecutor
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.agent.workspace import WorkspaceChangeSetState, WorkspaceImporter
from pilot107.api.project_agent_routes import ProjectAgentRoutes
from pilot107.api.remediation_routes import RemediationRoutes
from pilot107.core.advice import AdviceResult
from pilot107.core.contracts import ContractRecord
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.core.identity import UserIdentity
from pilot107.core.remediation import (
    ActionExecution,
    ActionProposal,
    EvaluationOutcome,
    RemediationBudget,
    RemediationSession,
    RemediationState,
)
from pilot107.core.remediation_store import RemediationStore
from pilot107.core.run_store import AgentAdviceRecord, RunStore, utc_now_iso
from pilot107.runtime_watch.model import (
    RuntimeLogCursor,
    RuntimeWatchRecord,
    RuntimeWatchState,
)
from pilot107.services.agent_session_service import AgentSessionService
from pilot107.services.project_agent_service import (
    FormalProjectRun,
    FormalRunApproval,
    ProjectAgentService,
)
from pilot107.services.remediation_service import RemediationService


class RepairSource:
    def __init__(self) -> None:
        self.files = {
            "/public/home/alice/failed/train.py": b"raise RuntimeError('broken')\n",
            "/public/home/alice/failed/config.yaml": b"epochs: 1\n",
        }

    def stat_path(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> FileStat:
        del timeout_seconds
        assert (path, owner) == ("/public/home/alice/failed", "alice")
        return FileStat(path=path, type="dir", size=0, mtime=1)

    def list_dir(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> list[FileEntry]:
        del timeout_seconds
        assert (path, owner) == ("/public/home/alice/failed", "alice")
        return [
            FileEntry(
                name=Path(file_path).name,
                type="file",
                size=len(content),
                mtime=index,
            )
            for index, (file_path, content) in enumerate(self.files.items(), start=2)
        ]

    def read_bytes_chunk(
        self,
        *,
        path: str,
        offset: int,
        length: int,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> tuple[str, int]:
        del timeout_seconds
        assert owner == "alice"
        content = self.files[path]
        return base64.b64encode(content[offset : offset + length]).decode(), len(content)

    def file_sha256(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> str:
        del timeout_seconds
        assert owner == "alice"
        return hashlib.sha256(self.files[path]).hexdigest()


class CodeRepairAdvice:
    def __init__(self) -> None:
        now = utc_now_iso()
        self.record = AgentAdviceRecord(
            advice_id="advice_code_repair",
            run_id="run_failed",
            owner="alice",
            request_key="code-repair",
            state="ready",
            version=1,
            source_run_updated_at=now,
            evidence_bundle_sha256="e" * 64,
            provider="none",
            model=None,
            payload={
                "schema_version": "AgentAdviceV1",
                "summary": "repair the failing training entrypoint",
                "actions": [
                    {
                        "action_id": "action_code_repair",
                        "type": "create_repair_ticket",
                        "source": "diagnosis_rule",
                        "risk": "medium",
                        "approval_required": True,
                        "policy_status": "allowed_preview",
                        "diagnosis_id": "diagnosis_runtime",
                    }
                ],
            },
            created_at=now,
            updated_at=now,
        )

    def advise(
        self,
        run_id: str,
        *,
        provider: str = "none",
        idempotency_key: str | None = None,
    ) -> AdviceResult:
        del provider, idempotency_key
        assert run_id == "run_failed"
        return AdviceResult(record=self.record, created=True)

    def get(self, advice_id: str) -> AgentAdviceRecord:
        assert advice_id == self.record.advice_id
        return self.record

    def approve(
        self,
        advice_id: str,
        *,
        expected_version: int,
        action_ids: list[str],
        actor: str,
        note: str | None = None,
    ) -> AgentAdviceRecord:
        del note
        assert advice_id == self.record.advice_id
        assert expected_version == 1
        assert action_ids == ["action_code_repair"]
        assert actor == "alice"
        self.record = replace(self.record, state="approved", version=2)
        return self.record


class FormalRepairSubmission:
    def __init__(self, formal: FormalProjectRun) -> None:
        self.formal = formal

    def approve_and_submit_formal_run(self, **values: object) -> FormalProjectRun:
        assert values == {
            "project_id": self.formal.contract.field_sources[0]["project_id"],
            "workspace_id": self.formal.contract.field_sources[0]["workspace_id"],
            "change_set_id": self.formal.contract.field_sources[0]["change_set_id"],
            "owner": "alice",
            "session_id": self.formal.approval.session_id,
            "validation_contract_id": "contract-validation",
            "validation_run_id": "run_failed",
            "validation_evidence_refs": ("evidence://validation",),
            "formal_contract_payload": {},
            "approved_digest": "a" * 64,
        }
        return self.formal


class FormalSubmissionMustNotRun:
    def approve_and_submit_formal_run(self, **values: object) -> FormalProjectRun:
        del values
        raise AssertionError("formal Run submission occurred before Remediation preflight")

def approved_repair(
    tmp_path: Path,
    *,
    budget: RemediationBudget | None = None,
) -> tuple[
    RemediationService,
    RemediationStore,
    ProjectAgentService,
    RepairSource,
    RemediationSession,
    ActionProposal,
    RemediationSession,
]:
    database = tmp_path / "pilot107.db"
    runs = RunStore(database)
    runs.create_run(
        run_id="run_failed",
        owner="alice",
        workdir="/public/home/alice/failed",
        script="python train.py",
        contract_id="contract_failed",
    )
    with runs.connect() as connection:
        connection.execute(
            """
            UPDATE runs
            SET state = 'FAILED', collection_state = 'succeeded',
                diagnosis_state = 'succeeded', exit_code = '42:0'
            WHERE run_id = 'run_failed'
            """
        )
    source = RepairSource()
    project_store = SQLiteProjectStore(database)
    agent_sessions = AgentSessionService(
        store=SQLiteAgentSessionStore(database),
        control_repository=SQLiteControlRepository(database),
    )
    projects = ProjectAgentService(
        store=project_store,
        workspace_root=tmp_path / "workspaces",
        sandbox=SandboxExecutor(store=project_store),
        importer=WorkspaceImporter(
            store=project_store,
            reader=source,
            owner_roots=("/public/home/{user}",),
            workspace_root=tmp_path / "workspaces",
        ),
        agent_session_service=agent_sessions,
    )
    remediations = RemediationStore(database)
    service = RemediationService(
        run_store=runs,
        remediation_store=remediations,
        advice_service=CodeRepairAdvice(),
        project_agent_service=projects,
    )
    session, _ = service.create(
        owner="alice",
        source_run_id="run_failed",
        request_key="repair-session",
        budget=budget,
    )
    planned = service.advance(session.session_id, worker_id="worker-a5")
    proposal = remediations.list_proposals(session.session_id)[0]
    approved = service.approve(
        session.session_id,
        proposal_id=proposal.proposal_id,
        actor="alice",
        expected_version=planned.version,
    )
    return service, remediations, projects, source, session, proposal, approved


def test_failed_run_repair_changes_code_in_workspace_not_source(tmp_path: Path) -> None:
    service, _, projects, source, session, proposal, approved = approved_repair(
        tmp_path
    )
    original = dict(source.files)

    repair = service.start_code_repair_project(
        session.session_id,
        proposal_id=proposal.proposal_id,
        actor="alice",
        expected_version=approved.version,
        request_key="repair-project",
    )
    digests = {
        item.path: item.source_sha256 for item in repair.workspace.snapshot.entries
    }
    change_set = projects.apply_patch(
        project_id=repair.project.project_id,
        workspace_id=repair.workspace.workspace_id,
        owner="alice",
        relative_path="train.py",
        expected_source_digest=digests["train.py"],
        operation="modify",
        content="print('repaired')\n",
    )
    sandbox = projects.execute_sandbox(
        project_id=repair.project.project_id,
        workspace_id=repair.workspace.workspace_id,
        owner="alice",
        change_set_id=change_set.change_set_id,
        argv=("python", "-m", "py_compile", "train.py"),
        timeout=3,
    )

    assert repair.project.origin is ExperimentProjectOrigin.FAILED_RUN
    assert repair.project.source is not None
    assert repair.project.source.ref_id == "run_failed"
    assert source.files == original
    assert [item.path for item in change_set.files] == ["train.py"]
    assert sandbox.status == "succeeded"
    assert projects.get_project(
        repair.project.project_id, owner="alice"
    ).change_sets[0].state.value == "reviewable"
    assert approved.state is RemediationState.READY


def test_approved_code_repair_http_action_returns_unified_project(
    tmp_path: Path,
) -> None:
    service, _, _, _, session, proposal, approved = approved_repair(tmp_path)

    response = RemediationRoutes(service).handle_post(
        ["remediation-sessions", session.session_id, "repair-project"],
        body=json.dumps(
            {
                "proposal_id": proposal.proposal_id,
                "expected_version": approved.version,
                "request_key": "repair-project-http",
            }
        ).encode(),
        identity=UserIdentity(username="alice"),
    )

    assert response is not None
    assert response.status == 201
    assert response.payload["project"]["origin"] == "failed_run"
    assert response.payload["project"]["source"]["ref_id"] == "run_failed"
    assert response.payload["repair_profile"] == "run_diagnosis_repair"
    assert response.payload["remediation_session_id"] == session.session_id


def test_repair_profile_persists_project_run_remediation_and_envelope(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pilot107.db"
    service = AgentSessionService(
        store=SQLiteAgentSessionStore(database),
        control_repository=SQLiteControlRepository(database),
    )
    envelope = {
        "partition": "debug",
        "qos": "normal",
        "cpus": 1,
        "memory_mib": 1024,
        "gpu_type": None,
        "gpus": 0,
        "walltime_seconds": 300,
        "max_tasks": 1,
        "max_submissions": 1,
        "workspace_snapshot_digest": "a" * 64,
        "expires_at": "2026-08-25T00:00:00Z",
        "approved_by": "alice",
    }

    session, _ = service.create_session(
        owner="alice",
        request_key="repair-agent-session",
        profile_id="run_diagnosis_repair",
        model_profile_id="campus-default",
        source={
            "project_id": "project-repair",
            "workspace_id": "workspace-repair",
            "run_id": "run-failed",
            "remediation_session_id": "remsession-repair",
            "resource_envelope": envelope,
        },
    )

    assert session.profile_id == "run_diagnosis_repair"
    assert session.source == {
        "project_id": "project-repair",
        "workspace_id": "workspace-repair",
        "run_id": "run-failed",
        "remediation_session_id": "remsession-repair",
        "resource_envelope": envelope,
    }


def test_formal_repair_run_rejoins_existing_remediation_evaluation(
    tmp_path: Path,
) -> None:
    service, remediations, projects, _, session, proposal, approved = approved_repair(
        tmp_path
    )
    repair = service.start_code_repair_project(
        session.session_id,
        proposal_id=proposal.proposal_id,
        actor="alice",
        expected_version=approved.version,
        request_key="repair-project-formal",
    )
    source_digest = next(
        item.source_sha256
        for item in repair.workspace.snapshot.entries
        if item.path == "train.py"
    )
    assert source_digest is not None
    draft = projects.apply_patch(
        project_id=repair.project.project_id,
        workspace_id=repair.workspace.workspace_id,
        owner="alice",
        relative_path="train.py",
        expected_source_digest=source_digest,
        operation="modify",
        content="print('repaired')\n",
    )
    projects.execute_sandbox(
        project_id=repair.project.project_id,
        workspace_id=repair.workspace.workspace_id,
        owner="alice",
        change_set_id=draft.change_set_id,
        argv=("python", "-m", "py_compile", "train.py"),
        timeout=3,
    )
    reviewable = projects.store.get_change_set(draft.change_set_id, owner="alice")
    published = projects.store.replace_change_set(
        replace(reviewable, state=WorkspaceChangeSetState.PUBLISHED),
        expected_version=reviewable.version,
    )
    assert projects.agent_session_service is not None
    agent_session, _ = projects.agent_session_service.create_session(
        owner="alice",
        request_key="repair-formal-session",
        profile_id="run_diagnosis_repair",
        model_profile_id="campus-default",
        source={
            "project_id": repair.project.project_id,
            "workspace_id": repair.workspace.workspace_id,
            "run_id": session.source_run_id,
            "remediation_session_id": session.session_id,
            "resource_envelope": _repair_envelope(),
        },
    )
    formal_contract = ContractRecord(
        contract_id="contract_formal_repair",
        owner="alice",
        recipe_version_id="recipe_python_cpu@1.0.0",
        payload={},
        field_sources=[
            {
                "field": "formal_run",
                "source": "agent_formal_approval",
                "approved_by": "alice",
                "project_id": repair.project.project_id,
                "workspace_id": repair.workspace.workspace_id,
                "session_id": agent_session.session_id,
                "change_set_id": published.change_set_id,
            }
        ],
        created_at="2026-08-25T00:00:00+00:00",
        updated_at="2026-08-25T00:00:00+00:00",
        derivation_reason="agent_formal_run",
    )
    formal_run = service.run_store.create_run(
        run_id="run_formal_repair",
        owner="alice",
        workdir="/public/home/alice/failed",
        script="python train.py",
        contract_id=formal_contract.contract_id,
        parent_run_id=session.source_run_id,
        lineage_reason="agent_formal_run",
    )
    with service.run_store.connect() as connection:
        connection.execute(
            "UPDATE runs SET state = 'SUBMITTED', job_id = '9001' WHERE run_id = ?",
            (formal_run.run_id,),
        )
    formal_run = service.run_store.get_run(formal_run.run_id)
    formal = FormalProjectRun(
        approval=FormalRunApproval(
            approval_digest="a" * 64,
            approved_by="alice",
            change_set_digest=published.digest,
            published_snapshot_digest="b" * 64,
            contract_digest="c" * 64,
            validation_contract_id="contract-validation",
            validation_run_id=session.source_run_id,
            validation_evidence_digest="d" * 64,
            session_id=agent_session.session_id,
        ),
        contract=formal_contract,
        run=formal_run,
        watch=_runtime_watch(formal_run.run_id),
    )

    # Simulate a process crash after the durable execution row was inserted
    # but before PREPARING could transition to EXECUTING.
    preparing = remediations.transition(
        session.session_id,
        expected_version=approved.version,
        expected_state=RemediationState.READY,
        target_state=RemediationState.PREPARING,
    )
    interrupted_execution_id = "remexec_" + hashlib.sha256(
        f"{session.session_id}\0{proposal.proposal_id}".encode()
    ).hexdigest()[:32]
    interrupted_at = utc_now_iso()
    remediations.append_execution(
        ActionExecution(
            execution_id=interrupted_execution_id,
            session_id=session.session_id,
            proposal_id=proposal.proposal_id,
            state="submitted",
            derived_contract_id=formal_contract.contract_id,
            derived_run_id=formal_run.run_id,
            error_code=None,
            error_message=None,
            created_at=interrupted_at,
            updated_at=interrupted_at,
        )
    )
    assert preparing.state is RemediationState.PREPARING

    route = ProjectAgentRoutes(
        FormalRepairSubmission(formal),  # type: ignore[arg-type]
        formal_run_observer=service,
    )
    request_body = json.dumps(
        {
            "project_id": repair.project.project_id,
            "workspace_id": repair.workspace.workspace_id,
            "session_id": agent_session.session_id,
            "validation_contract_id": "contract-validation",
            "validation_run_id": session.source_run_id,
            "validation_evidence_refs": ["evidence://validation"],
            "formal_contract": {},
            "approved_digest": "a" * 64,
        }
    ).encode()
    response = route.handle_post(
        ["agent-changesets", published.change_set_id, "formal-submit"],
        body=request_body,
        identity=UserIdentity(username="alice"),
    )
    replay_response = route.handle_post(
        ["agent-changesets", published.change_set_id, "formal-submit"],
        body=request_body,
        identity=UserIdentity(username="alice"),
    )
    bound = remediations.list_executions(session.session_id)[0]
    rebound_session = remediations.get_session(session.session_id)

    assert response is not None and response.status == 201
    assert replay_response is not None and replay_response.status == 201
    assert rebound_session.state is RemediationState.EXECUTING
    assert rebound_session.usage.attempts == 1
    assert rebound_session.usage.submissions == 1
    assert bound.derived_contract_id == formal_contract.contract_id
    assert bound.derived_run_id == formal_run.run_id
    assert len(remediations.list_executions(session.session_id)) == 1

    with service.run_store.connect() as connection:
        connection.execute(
            """
            UPDATE runs
            SET state = 'SUCCEEDED', terminal_state = 'COMPLETED', exit_code = '0:0',
                collection_state = 'succeeded', diagnosis_state = 'skipped'
            WHERE run_id = 'run_formal_repair'
            """
        )
    evaluated = service.advance(session.session_id, worker_id="worker-a5-evaluate")
    evaluation = remediations.list_evaluations(session.session_id)[0]
    assert evaluation.outcome is EvaluationOutcome.EXECUTION_SUCCESS_UNVERIFIED
    assert evaluated.state is RemediationState.BLOCKED
    assert evaluated.stop_reason == "execution_success_unverified"
    assert evaluation.derived_run_id == formal_run.run_id


def test_formal_repair_budget_is_checked_before_run_submission(tmp_path: Path) -> None:
    service, remediations, projects, _, session, proposal, approved = approved_repair(
        tmp_path,
        budget=RemediationBudget(max_submissions=0),
    )
    repair = service.start_code_repair_project(
        session.session_id,
        proposal_id=proposal.proposal_id,
        actor="alice",
        expected_version=approved.version,
        request_key="repair-project-budget",
    )
    source_digest = next(
        item.source_sha256
        for item in repair.workspace.snapshot.entries
        if item.path == "train.py"
    )
    draft = projects.apply_patch(
        project_id=repair.project.project_id,
        workspace_id=repair.workspace.workspace_id,
        owner="alice",
        relative_path="train.py",
        expected_source_digest=source_digest,
        operation="modify",
        content="print('repaired')\n",
    )
    projects.execute_sandbox(
        project_id=repair.project.project_id,
        workspace_id=repair.workspace.workspace_id,
        owner="alice",
        change_set_id=draft.change_set_id,
        argv=("python", "-m", "py_compile", "train.py"),
        timeout=3,
    )
    reviewable = projects.store.get_change_set(draft.change_set_id, owner="alice")
    published = projects.store.replace_change_set(
        replace(reviewable, state=WorkspaceChangeSetState.PUBLISHED),
        expected_version=reviewable.version,
    )
    assert projects.agent_session_service is not None
    agent_session, _ = projects.agent_session_service.create_session(
        owner="alice",
        request_key="repair-budget-session",
        profile_id="run_diagnosis_repair",
        model_profile_id="campus-default",
        source={
            "project_id": repair.project.project_id,
            "workspace_id": repair.workspace.workspace_id,
            "run_id": session.source_run_id,
            "remediation_session_id": session.session_id,
            "resource_envelope": _repair_envelope(),
        },
    )
    route = ProjectAgentRoutes(
        FormalSubmissionMustNotRun(),  # type: ignore[arg-type]
        formal_run_observer=service,
    )

    response = route.handle_post(
        ["agent-changesets", published.change_set_id, "formal-submit"],
        body=json.dumps(
            {
                "project_id": repair.project.project_id,
                "workspace_id": repair.workspace.workspace_id,
                "session_id": agent_session.session_id,
                "validation_contract_id": "contract-validation",
                "validation_run_id": session.source_run_id,
                "validation_evidence_refs": ["evidence://validation"],
                "formal_contract": {},
                "approved_digest": "a" * 64,
            }
        ).encode(),
        identity=UserIdentity(username="alice"),
    )

    assert response is not None
    assert response.status == 409
    assert response.payload["error"]["code"] == "REMEDIATION.BUDGET_EXHAUSTED"
    assert remediations.get_session(session.session_id).state is RemediationState.EXHAUSTED


def _repair_envelope() -> dict[str, object]:
    return {
        "partition": "debug",
        "qos": "normal",
        "cpus": 1,
        "memory_mib": 1024,
        "gpu_type": None,
        "gpus": 0,
        "walltime_seconds": 300,
        "max_tasks": 1,
        "max_submissions": 1,
        "workspace_snapshot_digest": "a" * 64,
        "expires_at": "2026-08-26T00:00:00Z",
        "approved_by": "alice",
    }


def _runtime_watch(run_id: str) -> RuntimeWatchRecord:
    timestamp = "2026-08-25T00:00:00+00:00"
    return RuntimeWatchRecord(
        watch_id="watch-formal-repair",
        run_id=run_id,
        owner="alice",
        connection_id="default",
        state=RuntimeWatchState.WATCHING,
        version=1,
        next_poll_at=timestamp,
        lease_owner=None,
        lease_expires_at=None,
        fencing_token=0,
        cursors=(
            RuntimeLogCursor.initial(run_id=run_id, owner="alice", stream="stdout"),
            RuntimeLogCursor.initial(run_id=run_id, owner="alice", stream="stderr"),
        ),
        created_at=timestamp,
        updated_at=timestamp,
        stopped_at=None,
        last_error_code=None,
        last_error_at=None,
    )
