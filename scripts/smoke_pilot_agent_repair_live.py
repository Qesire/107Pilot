"""D1 smoke for failed-Run code repair through the unified Project lifecycle."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from smoke_pilot_agent_a4_live import (
    ComposeEvidenceTransport,
    ComposePublicationRelay,
    _run_checked,
    _wait_run,
)

from pilot107.adapters.slurm import (
    DockerComposeExecutor,
    DockerComposeTarget,
    DockerSimulatorCommandBackend,
    FileEntry,
    FileStat,
)
from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.publisher import WorkspacePublicationState, WorkspacePublisher
from pilot107.agent.sandbox import SandboxExecutor
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.agent.workspace import WorkspaceChangeSetState, WorkspaceImporter
from pilot107.api.project_agent_routes import ProjectAgentRoutes
from pilot107.core.advice import AdviceResult
from pilot107.core.contracts import ContractService, ContractStore, RecipeCatalog
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.core.evidence_binding import EvidenceBinder
from pilot107.core.identity import UserIdentity
from pilot107.core.remediation import EvaluationOutcome, RemediationState
from pilot107.core.remediation_store import RemediationStore
from pilot107.core.run_service import RunService
from pilot107.core.run_store import AgentAdviceRecord, RunRecord, RunStore, utc_now_iso
from pilot107.core.states import CollectionState, RunState
from pilot107.runtime_watch.model import RuntimeWatchState
from pilot107.runtime_watch.service import (
    RunStoreRuntimeLogSourceResolver,
    RuntimeWatchPolicy,
    RuntimeWatchService,
)
from pilot107.runtime_watch.store import SQLiteRuntimeWatchStore
from pilot107.services.agent_session_service import AgentSessionService
from pilot107.services.project_agent_service import ProjectAgentService
from pilot107.services.remediation_service import RemediationService
from pilot107.worker.evidence import EvidenceStore


class ComposeWorkspaceRelay(ComposePublicationRelay):
    """Add bounded read primitives needed by WorkspaceImporter."""

    def stat_path(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> FileStat:
        program = (
            "import json,pathlib,sys;"
            "p=pathlib.Path(sys.argv[1]);s=p.lstat();"
            "t='dir' if p.is_dir() else ('file' if p.is_file() else 'other');"
            "print(json.dumps({'path':str(p),'type':t,'size':s.st_size,'mtime':int(s.st_mtime)}))"
        )
        value = json.loads(
            _run_checked(
                self.executor,
                ["python3", "-c", program, path],
                user=owner,
                timeout=timeout_seconds,
            )
        )
        return FileStat(
            path=str(value["path"]),
            type=str(value["type"]),
            size=int(value["size"]),
            mtime=int(value["mtime"]),
        )

    def list_dir(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> list[FileEntry]:
        program = """
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
items = []
for path in sorted(root.iterdir(), key=lambda item: item.name):
    stat = path.lstat()
    kind = 'dir' if path.is_dir() else ('file' if path.is_file() else 'other')
    items.append({
        'name': path.name,
        'type': kind,
        'size': stat.st_size,
        'mtime': int(stat.st_mtime),
    })
print(json.dumps(items))
""".strip()
        values = json.loads(
            _run_checked(
                self.executor,
                ["python3", "-c", program, path],
                user=owner,
                timeout=timeout_seconds,
            )
        )
        return [
            FileEntry(
                name=str(item["name"]),
                type=str(item["type"]),
                size=int(item["size"]),
                mtime=int(item["mtime"]),
            )
            for item in values
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
        program = (
            "import base64,json,pathlib,sys;"
            "p=pathlib.Path(sys.argv[1]);f=p.open('rb');f.seek(int(sys.argv[2]));"
            "d=f.read(int(sys.argv[3]));f.close();"
            "print(json.dumps({'data':base64.b64encode(d).decode(),'size':p.stat().st_size}))"
        )
        value = json.loads(
            _run_checked(
                self.executor,
                ["python3", "-c", program, path, str(offset), str(length)],
                user=owner,
                timeout=timeout_seconds,
            )
        )
        return str(value["data"]), int(value["size"])

    def file_sha256(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> str:
        return _run_checked(
            self.executor,
            ["sha256sum", path],
            user=owner,
            timeout=timeout_seconds,
        ).split()[0]


class RepairAdvice:
    def __init__(self, run_id: str) -> None:
        now = utc_now_iso()
        self.record = AgentAdviceRecord(
            advice_id="advice-repair-d1",
            run_id=run_id,
            owner="alice",
            request_key="repair-d1",
            state="ready",
            version=1,
            source_run_updated_at=now,
            evidence_bundle_sha256="e" * 64,
            provider="none",
            model=None,
            payload={
                "schema_version": "AgentAdviceV1",
                "summary": "repair the failed training entrypoint",
                "actions": [
                    {
                        "action_id": "repair-train-entrypoint",
                        "type": "create_repair_ticket",
                        "source": "diagnosis_rule",
                        "risk": "medium",
                        "approval_required": True,
                        "policy_status": "allowed_preview",
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
        if run_id != self.record.run_id:
            raise KeyError(run_id)
        return AdviceResult(record=self.record, created=True)

    def get(self, advice_id: str) -> AgentAdviceRecord:
        if advice_id != self.record.advice_id:
            raise KeyError(advice_id)
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
        if (
            advice_id != self.record.advice_id
            or expected_version != self.record.version
            or action_ids != ["repair-train-entrypoint"]
            or actor != "alice"
        ):
            raise ValueError("invalid D1 repair approval")
        self.record = replace(self.record, state="approved", version=2)
        return self.record


def _contract_payload(root: str, *, name: str) -> dict[str, object]:
    return {
        "recipe_version_id": "recipe_python_cpu@1.0.0",
        "project": {"name": name, "workdir": root},
        "entry": {"command": "python train.py", "expected_outputs": ["result.txt"]},
        "resources": {
            "partition": "Students",
            "qos": "qos_stu_default",
            "nodes": 1,
            "ntasks": 1,
            "cpus_per_task": 1,
            "memory": "128M",
            "gpus_total": 0,
            "gpu_type": None,
            "time_limit": "00:02:00",
        },
    }


def _finish_collection(store: RunStore, run_id: str) -> None:
    while tasks := store.acquire_due_collection_tasks(
        lease_owner="repair-d1-collector",
        limit=20,
        lease_seconds=60,
    ):
        for task in tasks:
            store.mark_collection_task_succeeded(
                task.task_id,
                lease_owner="repair-d1-collector",
                payload={"artifacts": [], "warnings": []},
            )
    if store.get_run(run_id).collection_state is not CollectionState.SUCCEEDED:
        raise RuntimeError(f"D1 collection did not complete for {run_id}")


def _register_json_evidence(
    store: RunStore,
    evidence: EvidenceStore,
    *,
    run_id: str,
    logical_path: str,
    payload: dict[str, object],
) -> str:
    artifact = evidence.write_json(
        run_id=run_id,
        logical_path=logical_path,
        payload=payload,
    )
    ref = f"evidence://runs/{run_id}/{logical_path}"
    store.upsert_evidence_objects(
        run_id,
        [
            {
                "object_id": f"evidence-{hashlib.sha256(ref.encode()).hexdigest()[:20]}",
                "category": "result",
                "logical_path": logical_path,
                "store_path": str(artifact.path),
                "source_uri": ref,
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
                "mime_type": artifact.content_type,
                "collection_status": "collected",
                "mutable_during_run": False,
                "finalized_at": datetime.now(UTC).isoformat(),
            }
        ],
    )
    return ref


def _wait_watch(
    watch_service: RuntimeWatchService,
    watch_store: SQLiteRuntimeWatchStore,
    run: RunRecord,
) -> str:
    if not watch_service.on_run_terminal(run_id=run.run_id, owner=run.owner):
        raise RuntimeError("repair Runtime Watch terminal drain was not scheduled")
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    while datetime.now(UTC) < deadline:
        result = watch_service.tick()
        watch = watch_store.get_watch_for_run(run.run_id, owner=run.owner)
        if watch.state is RuntimeWatchState.STOPPED:
            chunks = [
                watch_store.read_segment_content(segment.segment_id, owner=run.owner)
                for stream in ("stdout", "stderr")
                for segment in watch_store.list_segments(
                    run.run_id,
                    owner=run.owner,
                    stream=stream,
                )
            ]
            return b"".join(chunks).decode("utf-8", errors="replace")
        if result.errors:
            raise RuntimeError(f"repair Runtime Watch errors: {result.errors}")
    raise RuntimeError("repair Runtime Watch did not drain")


def _validation_envelope(snapshot_digest: str) -> dict[str, object]:
    return {
        "partition": "Students",
        "qos": "qos_stu_default",
        "cpus": 1,
        "memory_mib": 128,
        "gpu_type": None,
        "gpus": 0,
        "walltime_seconds": 120,
        "max_tasks": 1,
        "max_submissions": 1,
        "workspace_snapshot_digest": snapshot_digest,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "approved_by": "alice",
    }


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    compose_dir = root / "simulator" / "compose"
    executor = DockerComposeExecutor(
        DockerComposeTarget(
            compose_file=compose_dir / "compose.yml",
            env_file=compose_dir / ".env.example",
            workdir=compose_dir,
        )
    )
    backend = DockerSimulatorCommandBackend(
        executor=executor,
        allowed_roots=["/public/home/alice"],
        timeout_seconds=20,
    )
    suffix = uuid4().hex[:10]
    source_root = f"/public/home/alice/pilot107-repair-d1-{suffix}"
    try:
        _run_checked(executor, ["mkdir", "-p", source_root])
        _run_checked(
            executor,
            ["tee", f"{source_root}/train.py"],
            stdin="print('source-exit-42')\nraise SystemExit(42)\n",
        )
        _run_checked(
            executor,
            ["tee", f"{source_root}/config.yaml"],
            stdin="epochs: 1\n",
        )
        relay = ComposeWorkspaceRelay(executor)
        original_train_digest = relay.file_sha256(path=f"{source_root}/train.py", owner="alice")
        with tempfile.TemporaryDirectory(prefix="pilot107-repair-d1-") as temporary:
            temporary_root = Path(temporary)
            database = temporary_root / "pilot107.db"
            control = SQLiteControlRepository(database)
            run_store = RunStore(database)
            contract_store = ContractStore(database)
            contract_service = ContractService(
                catalog=RecipeCatalog(
                    store=contract_store,
                    partition_qos={"Students": ("qos_stu_default",)},
                    default_partition="Students",
                    default_qos="qos_stu_default",
                ),
                store=contract_store,
                partition_qos={"Students": ("qos_stu_default",)},
            )
            run_service = RunService(
                store=run_store,
                backend=backend,
                control_repository=control,
                dispatcher_id="repair-d1-run",
                submission_retry_delay_seconds=0,
            )
            evidence_store = EvidenceStore(temporary_root / "evidence")
            evidence_binder = EvidenceBinder(
                store=run_store,
                evidence_root=evidence_store.root,
            )
            source_contract = contract_service.create(
                owner="alice",
                payload=_contract_payload(source_root, name="repair-d1-source"),
            )
            source_run = run_service.submit(
                contract_service.to_submit_request(source_contract)
            )
            source_run = _wait_run(run_service, source_run.run_id)
            if source_run.state is not RunState.FAILED or source_run.exit_code != "42:0":
                raise RuntimeError(
                    f"D1 source Run did not fail with 42: {source_run.state} {source_run.exit_code}"
                )
            _finish_collection(run_store, source_run.run_id)
            run_store.replace_diagnoses(
                source_run.run_id,
                [
                    {
                        "diagnosis_id": "diagnosis-repair-d1",
                        "rule_id": "RUNTIME.NONZERO_EXIT",
                        "severity": "error",
                        "summary": "train.py exits with code 42",
                        "evidence_refs": [f"run:{source_run.run_id}"],
                        "suggested_patch": {},
                        "retryable": True,
                    }
                ],
            )
            project_store = SQLiteProjectStore(database)
            session_service = AgentSessionService(
                store=SQLiteAgentSessionStore(database),
                control_repository=control,
            )
            watch_store = SQLiteRuntimeWatchStore(
                database,
                segment_root=temporary_root / "watch-segments",
            )
            watch_service = RuntimeWatchService(
                store=watch_store,
                transport_for_connection=lambda _connection_id: ComposeEvidenceTransport(
                    executor,
                    "/public/home/alice",
                ),
                source_resolver=RunStoreRuntimeLogSourceResolver(
                    run_store=run_store,
                    allowed_roots=("/public/home/alice",),
                ),
                worker_id="repair-d1-watch",
                policy=RuntimeWatchPolicy(active_poll_seconds=1, quiet_poll_seconds=1),
                default_connection_id="d1",
            )
            project_service = ProjectAgentService(
                store=project_store,
                workspace_root=temporary_root / "workspaces",
                sandbox=SandboxExecutor(store=project_store),
                importer=WorkspaceImporter(
                    store=project_store,
                    reader=relay,
                    owner_roots=("/public/home/{user}",),
                    workspace_root=temporary_root / "workspaces",
                ),
                publisher=WorkspacePublisher(
                    store=project_store,
                    relay=relay,
                    owner_roots=("/public/home/{owner}",),
                ),
                contract_service=contract_service,
                run_service=run_service,
                runtime_watch_service=watch_service,
                agent_session_service=session_service,
                evidence_binder=evidence_binder,
            )
            remediation_store = RemediationStore(database)
            remediation = RemediationService(
                run_store=run_store,
                remediation_store=remediation_store,
                advice_service=RepairAdvice(source_run.run_id),
                contract_store=contract_store,
                evidence_store=evidence_store,
                project_agent_service=project_service,
            )
            created, _ = remediation.create(
                owner="alice",
                source_run_id=source_run.run_id,
                request_key="repair-d1-session",
            )
            planned = remediation.advance(created.session_id, worker_id="repair-d1-plan")
            proposal = remediation_store.list_proposals(created.session_id)[0]
            approved = remediation.approve(
                created.session_id,
                proposal_id=proposal.proposal_id,
                actor="alice",
                expected_version=planned.version,
            )
            repair = remediation.start_code_repair_project(
                created.session_id,
                proposal_id=proposal.proposal_id,
                actor="alice",
                expected_version=approved.version,
                request_key="repair-d1-project",
            )
            train_entry = next(
                item
                for item in repair.workspace.snapshot.entries
                if item.path == "train.py"
            )
            change_set = project_service.apply_patch(
                project_id=repair.project.project_id,
                workspace_id=repair.workspace.workspace_id,
                owner="alice",
                relative_path="train.py",
                expected_source_digest=train_entry.source_sha256,
                operation="modify",
                content=(
                    "from pathlib import Path\n"
                    "print('repair-success')\n"
                    "Path('result.txt').write_text('verified\\n')\n"
                ),
            )
            sandbox = project_service.execute_sandbox(
                project_id=repair.project.project_id,
                workspace_id=repair.workspace.workspace_id,
                owner="alice",
                change_set_id=change_set.change_set_id,
                argv=("python", "-m", "py_compile", "train.py"),
                timeout=5,
            )
            if sandbox.status != "succeeded":
                raise RuntimeError(f"D1 repair Sandbox failed: {sandbox.status}")
            reviewable = project_store.get_change_set(change_set.change_set_id, owner="alice")
            if (
                reviewable.state is not WorkspaceChangeSetState.REVIEWABLE
                or [item.path for item in reviewable.files] != ["train.py"]
            ):
                raise RuntimeError("D1 repair did not produce one reviewable train.py ChangeSet")
            current_source_digest = relay.file_sha256(
                path=f"{source_root}/train.py",
                owner="alice",
            )
            if current_source_digest != original_train_digest:
                raise RuntimeError("D1 isolated repair modified source before approval")
            publication = project_service.publish_change_set(
                project_id=repair.project.project_id,
                workspace_id=repair.workspace.workspace_id,
                owner="alice",
                change_set_id=reviewable.change_set_id,
                expected_version=reviewable.version,
                approved_digest=reviewable.digest,
            )
            if publication.state is not WorkspacePublicationState.PUBLISHED:
                raise RuntimeError(f"D1 repair publication failed: {publication.state}")
            published_digest = relay.file_sha256(
                path=f"{source_root}/train.py",
                owner="alice",
            )
            if published_digest == original_train_digest:
                raise RuntimeError("D1 approved repair was not published")

            validation_contract = contract_service.create(
                owner="alice",
                payload=_contract_payload(source_root, name="repair-d1-validation"),
            )
            validation_run = run_service.submit(
                contract_service.to_submit_request(validation_contract)
            )
            validation_run = _wait_run(run_service, validation_run.run_id)
            if validation_run.state is not RunState.SUCCEEDED:
                raise RuntimeError("D1 repaired validation Run failed")
            _finish_collection(run_store, validation_run.run_id)
            run_store.replace_diagnoses(validation_run.run_id, [])
            validation_ref = _register_json_evidence(
                run_store,
                evidence_store,
                run_id=validation_run.run_id,
                logical_path="validation/result.json",
                payload={"checks": "passed", "job_id": validation_run.job_id},
            )
            repair_agent_session, _ = session_service.create_session(
                owner="alice",
                request_key="repair-d1-agent-session",
                profile_id="run_diagnosis_repair",
                model_profile_id="faux-default",
                source={
                    "project_id": repair.project.project_id,
                    "workspace_id": repair.workspace.workspace_id,
                    "run_id": source_run.run_id,
                    "remediation_session_id": created.session_id,
                    "resource_envelope": _validation_envelope(
                        repair.workspace.snapshot.digest
                    ),
                },
            )
            formal_payload = _contract_payload(source_root, name="repair-d1-formal")
            formal_approval = project_service.prepare_formal_run(
                project_id=repair.project.project_id,
                workspace_id=repair.workspace.workspace_id,
                change_set_id=reviewable.change_set_id,
                owner="alice",
                session_id=repair_agent_session.session_id,
                validation_contract_id=validation_contract.contract_id,
                validation_run_id=validation_run.run_id,
                validation_evidence_refs=(validation_ref,),
                formal_contract_payload=formal_payload,
            )
            response = ProjectAgentRoutes(
                project_service,
                formal_run_observer=remediation,
            ).handle_post(
                ["agent-changesets", reviewable.change_set_id, "formal-submit"],
                body=json.dumps(
                    {
                        "project_id": repair.project.project_id,
                        "workspace_id": repair.workspace.workspace_id,
                        "session_id": repair_agent_session.session_id,
                        "validation_contract_id": validation_contract.contract_id,
                        "validation_run_id": validation_run.run_id,
                        "validation_evidence_refs": [validation_ref],
                        "formal_contract": formal_payload,
                        "approved_digest": formal_approval.approval_digest,
                    }
                ).encode(),
                identity=UserIdentity(username="alice"),
            )
            if response is None or response.status != 201:
                raise RuntimeError(f"D1 formal repair submit failed: {response}")
            formal_run_id = str(response.payload["run"]["run_id"])
            executing = remediation_store.get_session(created.session_id)
            if (
                executing.state is not RemediationState.EXECUTING
                or executing.usage.attempts != 1
                or executing.usage.submissions != 1
            ):
                raise RuntimeError("D1 formal repair was not bound into Remediation")
            formal_run = _wait_run(run_service, formal_run_id)
            if formal_run.state is not RunState.SUCCEEDED:
                raise RuntimeError("D1 formal repair Run failed")
            logs = _wait_watch(watch_service, watch_store, formal_run)
            if "repair-success" not in logs:
                raise RuntimeError("D1 repair Runtime Watch missed stdout")
            _finish_collection(run_store, formal_run.run_id)
            run_store.replace_diagnoses(formal_run.run_id, [])
            evidence_store.write_json(
                run_id=formal_run.run_id,
                logical_path="outputs/inventory.json",
                payload={
                    "files": [
                        {
                            "relative_path": "result.txt",
                            "attribution": "created",
                            "baseline_sha256": None,
                            "final_sha256": hashlib.sha256(b"verified\n").hexdigest(),
                        }
                    ]
                },
            )
            evaluated = remediation.advance(
                created.session_id,
                worker_id="repair-d1-evaluate",
            )
            evaluations = remediation_store.list_evaluations(created.session_id)
            if (
                evaluated.state is not RemediationState.SUCCEEDED
                or len(evaluations) != 1
                or evaluations[0].outcome is not EvaluationOutcome.VERIFIED_SUCCESS
            ):
                raise RuntimeError("D1 formal repair did not pass evidence-bound evaluation")

            restarted = RemediationService(
                run_store=RunStore(database),
                remediation_store=RemediationStore(database),
                advice_service=RepairAdvice(source_run.run_id),
                contract_store=ContractStore(database),
                evidence_store=EvidenceStore(temporary_root / "evidence"),
                project_agent_service=project_service,
            )
            replayed = restarted.advance(
                created.session_id,
                worker_id="repair-d1-restarted",
            )
            if (
                replayed.state is not RemediationState.SUCCEEDED
                or len(RemediationStore(database).list_executions(created.session_id)) != 1
                or len(RemediationStore(database).list_evaluations(created.session_id)) != 1
            ):
                raise RuntimeError("D1 restart duplicated repair execution or evaluation")
            report = {
                "schema": "pilot107.agent-repair-live-smoke/v1",
                "failed_run_code_repair": True,
                "source_run": {
                    "run_id": source_run.run_id,
                    "job_id": source_run.job_id,
                    "state": source_run.state.value,
                    "exit_code": source_run.exit_code,
                },
                "project_origin": repair.project.origin.value,
                "source_unchanged_before_approval": True,
                "reviewable_files": ["train.py"],
                "sandbox_succeeded": True,
                "publication_state": publication.state.value,
                "validation_run_id": validation_run.run_id,
                "formal_run": {
                    "run_id": formal_run.run_id,
                    "job_id": formal_run.job_id,
                    "state": formal_run.state.value,
                    "resource_plan": formal_run.resource_plan,
                },
                "runtime_watch_state": watch_store.get_watch_for_run(
                    formal_run.run_id,
                    owner="alice",
                ).state.value,
                "remediation_state": evaluated.state.value,
                "evaluation_outcome": evaluations[0].outcome.value,
                "restart_idempotent": True,
                "scientific_validity_inferred_from_scheduler": False,
            }
            print(json.dumps(report, sort_keys=True))
    finally:
        executor.run(
            ["rm", "-rf", source_root],
            user="alice",
            timeout_seconds=20,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
