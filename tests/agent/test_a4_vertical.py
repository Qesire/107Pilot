from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pilot107.adapters.slurm import InMemorySlurmBackend
from pilot107.agent.project import (
    ProjectBlueprint,
    ProjectContractIntent,
    ProjectValidation,
)
from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.publisher import (
    WorkspacePublication,
    WorkspacePublicationFile,
    WorkspacePublicationState,
    WorkspacePublisher,
)
from pilot107.agent.sandbox import SandboxExecutor
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.agent.task_store import SQLiteAgentTaskStore
from pilot107.agent.tasks import (
    AgentResourceEnvelope,
    AgentTaskGateReceipt,
    AgentTaskGateState,
    AgentTaskRequest,
    AgentTaskResult,
    AgentTaskState,
)
from pilot107.agent.workspace import (
    AgentWorkspaceRecord,
    WorkspaceApproval,
    WorkspaceChangeSet,
    WorkspaceChangeSetState,
    WorkspaceFileChange,
    WorkspaceSnapshot,
)
from pilot107.api.project_agent_routes import ProjectAgentRoutes
from pilot107.core.contracts import ContractService, ContractStore, RecipeCatalog
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.core.evidence_binding import EvidenceBinder
from pilot107.core.identity import UserIdentity
from pilot107.core.paths import SafePath
from pilot107.core.run_service import RunService
from pilot107.core.run_store import RunStore
from pilot107.core.states import CollectionState
from pilot107.runtime_watch.model import RuntimeWatchState
from pilot107.runtime_watch.reader import RuntimeLogSource
from pilot107.runtime_watch.service import RuntimeWatchService
from pilot107.runtime_watch.store import SQLiteRuntimeWatchStore
from pilot107.services.agent_session_service import AgentSessionService
from pilot107.services.project_agent_service import ProjectAgentService
from pilot107.worker.evidence import AuthorizedFilesystemEvidenceTransport, EvidenceStore
from pilot107.worker.runtime_worker import RuntimeReconcileWorker


class SourceResolver:
    def __init__(self, root: Path) -> None:
        self.root = root

    def resolve(self, *, run_id: str, owner: str, connection_id: str) -> RuntimeLogSource:
        del connection_id

        def path(stream: str) -> SafePath:
            value = self.root / f"{run_id}.{stream}"
            return SafePath(original=str(value), resolved=value, root=self.root)

        return RuntimeLogSource(
            run_id=run_id,
            owner=owner,
            stdout_path=path("out"),
            stderr_path=path("err"),
        )


class PublishedSnapshotRelay:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)

    def path_sha256(
        self,
        *,
        path: str,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> str | None:
        del owner, timeout_seconds
        content = self.files.get(path)
        return None if content is None else hashlib.sha256(content).hexdigest()


class A4Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.clock = lambda: datetime(2026, 8, 19, tzinfo=UTC)
        self.database = tmp_path / "pilot107.db"
        self.project_store = SQLiteProjectStore(self.database, clock=self.clock)
        self.contract_store = ContractStore(self.database)
        self.contract_service = ContractService(catalog=RecipeCatalog(), store=self.contract_store)
        self.run_store = RunStore(self.database)
        self.control = SQLiteControlRepository(self.database, clock=self.clock)
        self.backend = InMemorySlurmBackend()
        self.run_service = RunService(
            store=self.run_store,
            backend=self.backend,
            control_repository=self.control,
            dispatcher_id="a4-run-worker",
            submission_retry_delay_seconds=0,
            clock=self.clock,
        )
        self.evidence_store = EvidenceStore(tmp_path / "evidence")
        self.evidence_binder = EvidenceBinder(
            store=self.run_store, evidence_root=self.evidence_store.root
        )
        self.log_root = tmp_path / "logs"
        self.log_root.mkdir()
        self.watch_store = SQLiteRuntimeWatchStore(
            self.database, segment_root=tmp_path / "segments", clock=self.clock
        )
        transport = AuthorizedFilesystemEvidenceTransport(allowed_roots=[self.log_root])
        self.watch_service = RuntimeWatchService(
            store=self.watch_store,
            transport_for_connection=lambda connection_id: transport,
            source_resolver=SourceResolver(self.log_root),
            worker_id="a4-watch-worker",
            clock=self.clock,
        )
        self.session_store = SQLiteAgentSessionStore(self.database, clock=self.clock)
        self.task_store = SQLiteAgentTaskStore(self.database, clock=self.clock)
        self.session_service = AgentSessionService(
            store=self.session_store, control_repository=self.control
        )
        self._create_published_project(tmp_path)
        self.publication_relay = PublishedSnapshotRelay(
            {"/public/home/alice/a4-project/main.py": b"print('formal')\n"}
        )
        self.session, _ = self.session_service.create_session(
            owner="alice",
            request_key="a4-session",
            profile_id="experiment_builder",
            model_profile_id="faux-default",
            source={"project_id": self.project_id, "workspace_id": "workspace-a4"},
        )
        self.validation_contract = self.contract_service.create(
            owner="alice", payload=self.contract_payload("validation")
        )
        self.validation_run = self.run_service.submit(
            self.contract_service.to_submit_request(self.validation_contract)
        )
        assert self.validation_run.job_id is not None
        self.backend.advance_job(
            job_id=self.validation_run.job_id,
            raw_state="COMPLETED",
            exit_code="0:0",
        )
        self.validation_run = self.run_service.reconcile_once(self.validation_run.run_id)
        self.validation_ref = self._register_evidence(
            self.validation_run.run_id,
            "validation/result.json",
            '{"checks":"passed"}\n',
        )
        self.validation_task = self._create_validation_task()
        self.service = ProjectAgentService(
            store=self.project_store,
            workspace_root=tmp_path / "workspaces",
            sandbox=SandboxExecutor(store=self.project_store),
            publisher=WorkspacePublisher(
                store=self.project_store,
                relay=self.publication_relay,  # type: ignore[arg-type]
                owner_roots=("/public/home/{owner}",),
            ),
            contract_service=self.contract_service,
            run_service=self.run_service,
            runtime_watch_service=self.watch_service,
            agent_session_service=self.session_service,
            evidence_binder=self.evidence_binder,
            agent_task_store=self.task_store,
        )

    def contract_payload(self, name: str) -> dict[str, object]:
        return {
            "recipe_version_id": "recipe_python_cpu@1.0.0",
            "project": {
                "name": f"a4-{name}",
                "workdir": "/public/home/alice/a4-project",
            },
            "entry": {
                "command": "echo result > result.txt",
                "expected_outputs": ["result.txt"],
            },
            "resources": {
                "partition": "debug",
                "qos": "normal",
                "nodes": 1,
                "ntasks": 1,
                "cpus_per_task": 1,
                "time_limit": "00:05:00",
            },
        }

    def approve_and_submit(self):
        payload = self.contract_payload("formal")
        preview = self.service.prepare_formal_run(
            project_id=self.project_id,
            workspace_id="workspace-a4",
            change_set_id="changeset-a4",
            owner="alice",
            session_id=self.session.session_id,
            validation_contract_id=self.validation_contract.contract_id,
            validation_run_id=self.validation_run.run_id,
            validation_evidence_refs=(self.validation_ref,),
            formal_contract_payload=payload,
        )
        return self.service.approve_and_submit_formal_run(
            project_id=self.project_id,
            workspace_id="workspace-a4",
            change_set_id="changeset-a4",
            owner="alice",
            session_id=self.session.session_id,
            validation_contract_id=self.validation_contract.contract_id,
            validation_run_id=self.validation_run.run_id,
            validation_evidence_refs=(self.validation_ref,),
            formal_contract_payload=payload,
            approved_digest=preview.approval_digest,
        )

    def _create_validation_task(self):
        envelope = AgentResourceEnvelope(
            partition="debug",
            qos="normal",
            cpus=4,
            memory_mib=4096,
            gpu_type=None,
            gpus=0,
            walltime_seconds=600,
            max_tasks=1,
            max_submissions=1,
            workspace_snapshot_digest="a" * 64,
            expires_at="2026-08-19T01:00:00Z",
            approved_by="alice",
        )
        task, _ = self.task_store.create_task(
            owner="alice",
            session_id=self.session.session_id,
            turn_id="turn-a4-validation",
            project_id=self.project_id,
            workspace_id="workspace-a4",
            task_kind="slurm_validation",
            request_key="a4-validation-task",
            request=AgentTaskRequest(
                partition="debug",
                qos="normal",
                cpus=4,
                memory_mib=4096,
                gpu_type=None,
                gpus=0,
                walltime_seconds=600,
                tasks=1,
                submissions=1,
                workspace_snapshot_digest="a" * 64,
                payload={"script": "bash scripts/run_experiment.sh"},
            ),
            envelope=envelope,
        )
        lease = self.task_store.claim_task(
            task.task_id, owner="alice", worker_id="a4-task-worker", lease_seconds=60
        )
        assert lease is not None
        self.task_store.link_run(task.task_id, lease=lease, run_id=self.validation_run.run_id)
        receipt = AgentTaskGateReceipt(
            task_id=task.task_id,
            run_id=self.validation_run.run_id,
            run_terminal_state="completed",
            evidence_refs=(self.validation_ref,),
            evidence_digest="d" * 64,
            integrity_verified_at="2026-08-19T00:05:00Z",
            workspace_revision=None,
            workspace_digest="a" * 64,
            legacy_boundary=True,
            capsule_ref=None,
            capsule_state="not_required",
            source_revision="workspace-snapshot:sha256:" + "a" * 64,
            platform_snapshot_ref="snapshot:platform-a4",
            terminal_at=self.validation_run.updated_at,
        )
        return self.task_store.finalize_task(
            task.task_id,
            lease=lease,
            gate_receipt=receipt,
            result=AgentTaskResult.succeeded((self.validation_ref,)),
        )

    def _create_published_project(self, tmp_path: Path) -> None:
        project = self.project_store.create_project(
            owner="alice",
            origin="blank",
            goal="formal experiment",
            request_key="a4-project",
        )
        project_id = project.project_id
        self.project_store.save_blueprint(
            project_id,
            "alice",
            project.version,
            ProjectBlueprint(
                goal="Verify heat diffusion convergence and CPU scaling.",
                entrypoints=("scripts/run_experiment.sh",),
                files=(),
                validations=(
                    ProjectValidation(
                        validation_id="heat-slurm-validation",
                        execution="slurm",
                        argv=("bash", "scripts/run_experiment.sh"),
                        expected_outputs=("convergence.json", "scaling.json", "report.md"),
                    ),
                ),
                contract_intent=ProjectContractIntent(
                    recipe_version_id="recipe_python_cpu@1.0.0",
                    resource_hints={"cpus_per_task": 4, "gpus": 0},
                ),
                expected_outputs=(),
                dependencies=(),
                open_questions=(),
            ),
        )
        local_root = tmp_path / "workspaces" / "alice" / "workspace-a4"
        local_root.mkdir(parents=True)
        (local_root / "main.py").write_text("print('formal')\n")
        workspace = AgentWorkspaceRecord(
            workspace_id="workspace-a4",
            project_id=project_id,
            owner="alice",
            local_root=str(local_root),
            snapshot=WorkspaceSnapshot(
                source_ref="/public/home/alice/a4-project",
                digest="a" * 64,
                entries=(),
                captured_at="2026-08-19T00:00:00Z",
            ),
            created_at="2026-08-19T00:00:00Z",
            updated_at="2026-08-19T00:00:00Z",
        )
        self.project_store.save_workspace(workspace)
        after = hashlib.sha256(b"print('formal')\n").hexdigest()
        change_set = WorkspaceChangeSet(
            change_set_id="changeset-a4",
            project_id=project_id,
            workspace_id=workspace.workspace_id,
            owner="alice",
            base_snapshot_digest="a" * 64,
            digest="b" * 64,
            state=WorkspaceChangeSetState.PUBLISHED,
            version=4,
            files=(
                WorkspaceFileChange(
                    path="main.py",
                    operation="create",
                    before_sha256=None,
                    after_sha256=after,
                    diff_sha256="c" * 64,
                    size_bytes=16,
                ),
            ),
            sandbox_results=(),
            approval=WorkspaceApproval(
                actor="alice",
                approved_digest="b" * 64,
                approved_at="2026-08-19T00:00:00Z",
            ),
            created_at="2026-08-19T00:00:00Z",
            updated_at="2026-08-19T00:00:00Z",
        )
        self.project_store.save_change_set(change_set, diff_text="new main.py")
        self.project_store.save_workspace_publication(
            WorkspacePublication(
                publication_id="publication-a4",
                change_set_id=change_set.change_set_id,
                project_id=project_id,
                workspace_id=workspace.workspace_id,
                owner="alice",
                target_root="/public/home/alice/a4-project",
                approved_digest=change_set.digest,
                approved_by="alice",
                state=WorkspacePublicationState.PUBLISHED,
                version=4,
                files=(
                    WorkspacePublicationFile(
                        path="main.py",
                        operation="create",
                        expected_sha256=None,
                        desired_sha256=after,
                        staging_path=None,
                        state="committed",
                        size_bytes=16,
                    ),
                ),
                error_code=None,
                created_at="2026-08-19T00:00:00Z",
                updated_at="2026-08-19T00:00:00Z",
            )
        )
        self.project_id = project_id

    def _register_evidence(self, run_id: str, logical_path: str, content: str) -> str:
        artifact = self.evidence_store.write_text(
            run_id=run_id,
            logical_path=logical_path,
            content=content,
            content_type="application/json",
        )
        ref = f"evidence://runs/{run_id}/{logical_path}"
        self.run_store.upsert_evidence_objects(
            run_id,
            [
                {
                    "object_id": f"evidence-{hashlib.sha256(ref.encode()).hexdigest()[:16]}",
                    "category": "result",
                    "logical_path": logical_path,
                    "store_path": str(artifact.path),
                    "source_uri": ref,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "mime_type": artifact.content_type,
                    "collection_status": "collected",
                    "mutable_during_run": False,
                    "finalized_at": "2026-08-19T00:05:00Z",
                }
            ],
        )
        return ref


@pytest.fixture
def harness(tmp_path: Path) -> A4Harness:
    return A4Harness(tmp_path)


def test_formal_run_binds_approved_changeset_contract_and_validation(
    harness: A4Harness,
) -> None:
    formal = harness.approve_and_submit()

    assert formal.contract.parent_contract_id == harness.validation_contract.contract_id
    assert formal.contract.derivation_reason == "agent_formal_run"
    assert formal.run.parent_run_id == harness.validation_run.run_id
    assert formal.run.lineage_reason == "agent_formal_run"
    assert formal.watch.run_id == formal.run.run_id
    assert formal.run.job_id is not None
    assert formal.approval.approval_digest in str(formal.contract.field_sources)


def test_formal_candidate_derives_validation_lineage_from_succeeded_agent_task(
    harness: A4Harness,
) -> None:
    candidate = harness.service.prepare_formal_run_candidate(
        project_id=harness.project_id,
        workspace_id="workspace-a4",
        change_set_id="changeset-a4",
        session_id=harness.session.session_id,
        validation_task_id=harness.validation_task.task_id,
        owner="alice",
    )

    assert candidate.validation_contract_id == harness.validation_contract.contract_id
    assert candidate.validation_run_id == harness.validation_run.run_id
    assert candidate.validation_evidence_refs == (harness.validation_ref,)
    assert candidate.published_workdir == "/public/home/alice/a4-project"
    assert candidate.default_command == "bash scripts/run_experiment.sh"
    assert candidate.resource_hints == {"cpus_per_task": 4, "gpus": 0}


class _OneTaskStore:
    def __init__(self, task) -> None:
        self.task = task

    def get_task(self, task_id: str, *, owner: str):
        del task_id, owner
        return self.task


@pytest.mark.parametrize(
    "task_patch",
    [
        {
            "state": AgentTaskState.PENDING,
            "result": None,
            "linked_run_id": None,
            "gate_receipt": None,
            "gate_state": AgentTaskGateState.CREATED,
        },
        {
            "state": AgentTaskState.FAILED,
            "result": AgentTaskResult(
                status="failed", evidence_refs=(), error_code="validation_failed", message=None
            ),
        },
        {"owner": "bob"},
        {"session_id": "session-other"},
        {"project_id": "project-other"},
        {"workspace_id": "workspace-other"},
        {"result": AgentTaskResult.succeeded(())},
    ],
)
def test_formal_candidate_rejects_untrusted_or_unbound_agent_tasks(
    harness: A4Harness, task_patch: dict[str, object]
) -> None:
    harness.service.agent_task_store = _OneTaskStore(replace(harness.validation_task, **task_patch))

    with pytest.raises(ValueError, match="AgentTask|Evidence|lineage"):
        harness.service.prepare_formal_run_candidate(
            project_id=harness.project_id,
            workspace_id="workspace-a4",
            change_set_id="changeset-a4",
            session_id=harness.session.session_id,
            validation_task_id=harness.validation_task.task_id,
            owner="alice",
        )


def test_formal_approval_rejects_resource_change_and_replays_one_run(
    harness: A4Harness,
) -> None:
    payload = harness.contract_payload("formal")
    preview = harness.service.prepare_formal_run(
        project_id=harness.project_id,
        workspace_id="workspace-a4",
        change_set_id="changeset-a4",
        owner="alice",
        session_id=harness.session.session_id,
        validation_contract_id=harness.validation_contract.contract_id,
        validation_run_id=harness.validation_run.run_id,
        validation_evidence_refs=(harness.validation_ref,),
        formal_contract_payload=payload,
    )
    changed = replace_resource(payload, cpus=2)

    with pytest.raises(ValueError, match="approval digest"):
        harness.service.approve_and_submit_formal_run(
            project_id=harness.project_id,
            workspace_id="workspace-a4",
            change_set_id="changeset-a4",
            owner="alice",
            session_id=harness.session.session_id,
            validation_contract_id=harness.validation_contract.contract_id,
            validation_run_id=harness.validation_run.run_id,
            validation_evidence_refs=(harness.validation_ref,),
            formal_contract_payload=changed,
            approved_digest=preview.approval_digest,
        )

    first = harness.approve_and_submit()
    second = harness.approve_and_submit()
    assert second.run.run_id == first.run.run_id
    assert second.contract.contract_id == first.contract.contract_id


def test_formal_submit_rechecks_remote_published_snapshot(
    harness: A4Harness,
) -> None:
    payload = harness.contract_payload("formal")
    preview = harness.service.prepare_formal_run(
        project_id=harness.project_id,
        workspace_id="workspace-a4",
        change_set_id="changeset-a4",
        owner="alice",
        session_id=harness.session.session_id,
        validation_contract_id=harness.validation_contract.contract_id,
        validation_run_id=harness.validation_run.run_id,
        validation_evidence_refs=(harness.validation_ref,),
        formal_contract_payload=payload,
    )
    harness.publication_relay.files["/public/home/alice/a4-project/main.py"] = (
        b"external change after publication\n"
    )

    with pytest.raises(ValueError, match="published Workspace changed"):
        harness.service.approve_and_submit_formal_run(
            project_id=harness.project_id,
            workspace_id="workspace-a4",
            change_set_id="changeset-a4",
            owner="alice",
            session_id=harness.session.session_id,
            validation_contract_id=harness.validation_contract.contract_id,
            validation_run_id=harness.validation_run.run_id,
            validation_evidence_refs=(harness.validation_ref,),
            formal_contract_payload=payload,
            approved_digest=preview.approval_digest,
        )


def test_terminal_watch_and_evidence_enqueue_one_result_explanation(
    harness: A4Harness,
) -> None:
    formal = harness.approve_and_submit()
    assert formal.run.job_id is not None
    (harness.log_root / f"{formal.run.run_id}.out").write_text("finished\n")
    (harness.log_root / f"{formal.run.run_id}.err").write_text("")
    harness.backend.advance_job(
        job_id=formal.run.job_id,
        raw_state="COMPLETED",
        exit_code="0:0",
    )
    terminal = harness.run_service.reconcile_once(formal.run.run_id)
    harness.watch_service.on_run_terminal(run_id=terminal.run_id, owner="alice")
    harness.watch_service.tick()
    final_ref = harness._register_evidence(
        terminal.run_id,
        "results/summary.json",
        '{"scheduler":"completed","scientific_validity":"not_assessed"}\n',
    )

    first = harness.service.complete_formal_run(
        run_id=terminal.run_id,
        owner="alice",
        session_id=harness.session.session_id,
        evidence_refs=(final_ref,),
    )
    second = harness.service.complete_formal_run(
        run_id=terminal.run_id,
        owner="alice",
        session_id=harness.session.session_id,
        evidence_refs=(final_ref,),
    )

    assert first.watch.state is RuntimeWatchState.STOPPED
    assert first.evidence_bundle.sha256 == second.evidence_bundle.sha256
    assert first.explanation_turn.turn_id == second.explanation_turn.turn_id
    assert "does not establish scientific validity" in first.explanation_turn.message


def test_terminal_watch_handoff_survives_restart_and_auto_enqueues_explanation(
    harness: A4Harness,
) -> None:
    formal = harness.approve_and_submit()
    assert formal.run.job_id is not None
    (harness.log_root / f"{formal.run.run_id}.out").write_text("finished\n")
    (harness.log_root / f"{formal.run.run_id}.err").write_text("")
    harness.backend.advance_job(
        job_id=formal.run.job_id,
        raw_state="COMPLETED",
        exit_code="0:0",
    )
    terminal = harness.run_service.reconcile_once(formal.run.run_id)
    assert harness.run_store.defer_logs_finalize_for_runtime_watch(terminal.run_id)
    harness.watch_service.on_run_terminal(run_id=terminal.run_id, owner="alice")
    harness.watch_service.tick()
    assert harness.run_store.release_logs_finalize_after_runtime_watch(terminal.run_id)

    _, created = harness.session_service.enqueue_formal_result_handoff(
        run=terminal,
        contract=formal.contract,
    )
    _, replay_created = harness.session_service.enqueue_formal_result_handoff(
        run=terminal,
        contract=formal.contract,
    )
    assert created is True
    assert replay_created is False

    while tasks := harness.run_store.acquire_due_collection_tasks(
        lease_owner="a4-collector",
        limit=20,
        lease_seconds=60,
    ):
        for task in tasks:
            harness.run_store.mark_collection_task_succeeded(
                task.task_id,
                lease_owner="a4-collector",
                payload={"artifacts": [], "warnings": []},
            )
    assert harness.run_store.get_run(terminal.run_id).collection_state is CollectionState.SUCCEEDED
    harness._register_evidence(
        terminal.run_id,
        "results/summary.json",
        '{"scheduler":"completed","scientific_validity":"not_assessed"}\n',
    )

    restarted = AgentSessionService(
        store=SQLiteAgentSessionStore(harness.database, clock=harness.clock),
        control_repository=SQLiteControlRepository(harness.database, clock=harness.clock),
    )
    first = RuntimeReconcileWorker(
        service=harness.run_service,
        agent_session_service=restarted,
        runtime_watch_service=harness.watch_service,
        formal_result_evidence_binder=harness.evidence_binder,
        worker_id="a4-result-worker",
    ).tick()
    second = RuntimeReconcileWorker(
        service=harness.run_service,
        agent_session_service=restarted,
        runtime_watch_service=harness.watch_service,
        formal_result_evidence_binder=harness.evidence_binder,
        worker_id="a4-result-worker-restarted",
    ).tick()

    assert first.formal_results_checked == 1
    assert first.formal_results_succeeded == 1
    assert first.formal_result_errors == []
    assert second.formal_results_checked == 0
    assert second.formal_results_succeeded == 0
    with restarted.store.connect() as connection:
        rows = connection.execute(
            "SELECT turn_id FROM agent_turns WHERE session_id = ?",
            (harness.session.session_id,),
        ).fetchall()
    assert len(rows) == 1
    turn = restarted.store.get_turn(str(rows[0]["turn_id"]), owner="alice")
    assert turn.request_key == f"formal-run:{terminal.run_id}:result-explanation"
    assert "does not establish scientific validity" in turn.message


def test_formal_result_dispatch_waits_for_collected_terminal_evidence(
    harness: A4Harness,
) -> None:
    formal = harness.approve_and_submit()
    assert formal.run.job_id is not None
    harness.backend.advance_job(
        job_id=formal.run.job_id,
        raw_state="COMPLETED",
        exit_code="0:0",
    )
    terminal = harness.run_service.reconcile_once(formal.run.run_id)
    (harness.log_root / f"{formal.run.run_id}.out").write_text("finished\n")
    (harness.log_root / f"{formal.run.run_id}.err").write_text("")
    assert harness.run_store.defer_logs_finalize_for_runtime_watch(terminal.run_id)
    harness.watch_service.on_run_terminal(run_id=terminal.run_id, owner="alice")
    harness.watch_service.tick()
    assert harness.run_store.release_logs_finalize_after_runtime_watch(terminal.run_id)
    while tasks := harness.run_store.acquire_due_collection_tasks(
        lease_owner="a4-collector",
        limit=20,
        lease_seconds=60,
    ):
        for task in tasks:
            harness.run_store.mark_collection_task_succeeded(
                task.task_id,
                lease_owner="a4-collector",
                payload={"artifacts": [], "warnings": []},
            )
    harness.session_service.enqueue_formal_result_handoff(
        run=terminal,
        contract=formal.contract,
    )

    result = RuntimeReconcileWorker(
        service=harness.run_service,
        agent_session_service=harness.session_service,
        runtime_watch_service=harness.watch_service,
        formal_result_evidence_binder=harness.evidence_binder,
        worker_id="a4-result-worker",
    ).tick()

    assert result.formal_results_checked == 1
    assert result.formal_results_succeeded == 0
    assert result.formal_result_errors == [f"{terminal.run_id}:ValueError"]


def test_formal_run_http_flow_requires_preview_digest(harness: A4Harness) -> None:
    routes = ProjectAgentRoutes(harness.service)
    body = {
        "project_id": harness.project_id,
        "workspace_id": "workspace-a4",
        "session_id": harness.session.session_id,
        "validation_contract_id": harness.validation_contract.contract_id,
        "validation_run_id": harness.validation_run.run_id,
        "validation_evidence_refs": [harness.validation_ref],
        "formal_contract": harness.contract_payload("formal-http"),
    }
    preview = routes.handle_post(
        ["agent-changesets", "changeset-a4", "formal-preview"],
        body=json.dumps(body).encode(),
        identity=UserIdentity(username="alice"),
    )
    assert preview is not None and preview.status == 200

    submitted = routes.handle_post(
        ["agent-changesets", "changeset-a4", "formal-submit"],
        body=json.dumps({**body, "approved_digest": preview.payload["approval_digest"]}).encode(),
        identity=UserIdentity(username="alice"),
    )

    assert submitted is not None and submitted.status == 201
    assert submitted.payload["run"]["lineage_reason"] == "agent_formal_run"
    assert submitted.payload["watch"]["run_id"] == submitted.payload["run"]["run_id"]


def test_formal_candidate_http_derives_lineage_and_rejects_injected_ids(
    harness: A4Harness,
) -> None:
    routes = ProjectAgentRoutes(harness.service)
    body = {
        "project_id": harness.project_id,
        "workspace_id": "workspace-a4",
        "session_id": harness.session.session_id,
        "validation_task_id": harness.validation_task.task_id,
    }
    response = routes.handle_post(
        ["agent-changesets", "changeset-a4", "formal-run-candidate"],
        body=json.dumps(body).encode(),
        identity=UserIdentity(username="alice"),
    )
    injected = routes.handle_post(
        ["agent-changesets", "changeset-a4", "formal-run-candidate"],
        body=json.dumps({**body, "validation_run_id": "run-injected"}).encode(),
        identity=UserIdentity(username="alice"),
    )

    assert response is not None and response.status == 200
    assert response.payload["validation_run_id"] == harness.validation_run.run_id
    assert response.payload["published_workdir"] == "/public/home/alice/a4-project"
    assert injected is not None and injected.status == 400


def replace_resource(payload: dict[str, object], *, cpus: int) -> dict[str, object]:
    result = {**payload}
    resources = dict(result["resources"])  # type: ignore[arg-type]
    resources["cpus_per_task"] = cpus
    result["resources"] = resources
    return result
