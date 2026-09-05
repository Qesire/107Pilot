"""D1 smoke for approval-bound formal Agent Runs on Docker Slurm."""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pilot107.adapters.slurm import (
    DockerComposeExecutor,
    DockerComposeTarget,
    DockerSimulatorCommandBackend,
)
from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.publisher import WorkspacePublicationState, WorkspacePublisher
from pilot107.agent.sandbox import SandboxExecutor
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.agent.workspace import (
    AgentWorkspaceRecord,
    WorkspaceApproval,
    WorkspaceChangeSet,
    WorkspaceChangeSetState,
    WorkspaceFileChange,
    WorkspaceSnapshot,
)
from pilot107.core.contracts import ContractService, ContractStore, RecipeCatalog
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.core.evidence_binding import EvidenceBinder
from pilot107.core.identity import UserIdentity
from pilot107.core.paths import SafePath
from pilot107.core.run_service import RunService
from pilot107.core.run_store import RunRecord, RunStore
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
from pilot107.worker.evidence import (
    EvidenceCapability,
    EvidencePolicy,
    EvidenceRoot,
    EvidenceStore,
    FileStat,
    OutputInventory,
    TextTail,
)
from pilot107.worker.runtime_worker import RuntimeReconcileWorker


def _run_checked(
    executor: DockerComposeExecutor,
    argv: list[str],
    *,
    user: str = "alice",
    stdin: str | None = None,
    timeout: float = 20,
) -> str:
    result = executor.run(
        argv,
        user=user,
        stdin=stdin,
        timeout_seconds=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"command failed: {argv[0]}")
    return result.stdout


class ComposePublicationRelay:
    def __init__(self, executor: DockerComposeExecutor) -> None:
        self.executor = executor

    def path_sha256(self, *, path: str, owner: str, timeout_seconds: float = 30.0) -> str | None:
        result = self.executor.run(
            ["sha256sum", path],
            user=owner,
            timeout_seconds=timeout_seconds,
        )
        if result.returncode != 0:
            return None
        return result.stdout.split()[0]

    def make_dir(self, *, path: str, owner: str, timeout_seconds: float = 30.0) -> None:
        _run_checked(
            self.executor,
            ["mkdir", "-p", path],
            user=owner,
            timeout=timeout_seconds,
        )

    def write_bytes_chunk(
        self,
        *,
        path: str,
        data_b64: str,
        offset: int,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> int:
        program = (
            "import base64,pathlib,sys;"
            "p=pathlib.Path(sys.argv[1]);d=base64.b64decode(sys.argv[2],validate=True);"
            "m='wb' if sys.argv[3]=='0' else 'ab';"
            "f=p.open(m);f.write(d);f.close();print(p.stat().st_size)"
        )
        size = _run_checked(
            self.executor,
            ["python3", "-c", program, path, data_b64, str(offset)],
            user=owner,
            timeout=timeout_seconds,
        )
        return int(size.strip())

    def compare_and_swap_file(
        self,
        *,
        staged_path: str,
        target_path: str,
        expected_sha256: str | None,
        desired_sha256: str,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> str:
        program = """
import hashlib, os, pathlib, sys
staged, target = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
expected = None if sys.argv[3] == '-' else sys.argv[3]
desired = sys.argv[4]
digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
current = digest(target)
if current == desired:
    print('already_committed')
elif current != expected:
    print('conflict')
elif digest(staged) != desired:
    print('staged_digest_mismatch')
else:
    os.replace(staged, target)
    print('committed')
""".strip()
        return _run_checked(
            self.executor,
            [
                "python3",
                "-c",
                program,
                staged_path,
                target_path,
                expected_sha256 or "-",
                desired_sha256,
            ],
            user=owner,
            timeout=timeout_seconds,
        ).strip()

    def compare_and_delete_file(
        self,
        *,
        target_path: str,
        expected_sha256: str,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> str:
        program = """
import hashlib, pathlib, sys
p=pathlib.Path(sys.argv[1]); expected=sys.argv[2]
if not p.exists(): print('already_committed')
elif hashlib.sha256(p.read_bytes()).hexdigest()!=expected: print('conflict')
else: p.unlink(); print('committed')
""".strip()
        return _run_checked(
            self.executor,
            ["python3", "-c", program, target_path, expected_sha256],
            user=owner,
            timeout=timeout_seconds,
        ).strip()

    def remove_path(self, *, path: str, owner: str, timeout_seconds: float = 30.0) -> None:
        _run_checked(
            self.executor,
            ["rm", "-rf", path],
            user=owner,
            timeout=timeout_seconds,
        )


class ComposeEvidenceTransport:
    def __init__(self, executor: DockerComposeExecutor, root: str) -> None:
        self.executor = executor
        self.root = root

    def probe(self, identity: UserIdentity) -> EvidenceCapability:
        return EvidenceCapability(
            transport="compose-d1",
            can_stat=True,
            can_tail=True,
            can_inventory=False,
            can_copy_selected=False,
            authorized_roots=(self.root,),
            max_single_read_bytes=256 * 1024,
            notes=(f"identity={identity.username}",),
        )

    def stat(self, identity: UserIdentity, path: SafePath) -> FileStat:
        program = """
import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1])
if not p.is_file(): raise SystemExit(3)
s=p.stat(); prefix=hashlib.sha256(p.read_bytes()[:4096]).hexdigest()
print(json.dumps({'size':s.st_size,'mtime':s.st_mtime,'identity':f'{s.st_dev}:{s.st_ino}','prefix':prefix}))
""".strip()
        result = self.executor.run(
            ["python3", "-c", program, str(path.resolved)],
            user=identity.username,
            timeout_seconds=20,
        )
        if result.returncode == 3:
            raise FileNotFoundError(str(path.resolved))
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "compose stat failed")
        value = json.loads(result.stdout)
        return FileStat(
            path=str(path.resolved),
            kind="regular file",
            size_bytes=int(value["size"]),
            mtime_epoch=float(value["mtime"]),
            owner_readable=True,
            file_identity=str(value["identity"]),
            prefix_sha256=str(value["prefix"]),
        )

    def read_bytes_range(
        self,
        identity: UserIdentity,
        path: SafePath,
        offset: int,
        length: int,
    ) -> bytes:
        program = (
            "import base64,pathlib,sys;f=pathlib.Path(sys.argv[1]).open('rb');"
            "f.seek(int(sys.argv[2]));print(base64.b64encode(f.read(int(sys.argv[3]))).decode())"
        )
        value = _run_checked(
            self.executor,
            ["python3", "-c", program, str(path.resolved), str(offset), str(length)],
            user=identity.username,
        )
        return base64.b64decode(value.strip())

    def prepare_run_root(
        self, identity: UserIdentity, run_id: str, policy: EvidencePolicy
    ) -> EvidenceRoot:
        del identity, run_id, policy
        raise NotImplementedError

    def read_text_tail(self, identity: UserIdentity, path: SafePath, max_bytes: int) -> TextTail:
        del identity, path, max_bytes
        raise NotImplementedError

    def inventory(
        self, identity: UserIdentity, root: SafePath, policy: EvidencePolicy
    ) -> OutputInventory:
        del identity, root, policy
        raise NotImplementedError


@dataclass
class SmokeContext:
    database: Path
    project_store: SQLiteProjectStore
    contract_service: ContractService
    run_store: RunStore
    run_service: RunService
    evidence_store: EvidenceStore
    evidence_binder: EvidenceBinder
    session_service: AgentSessionService
    watch_store: SQLiteRuntimeWatchStore
    watch_service: RuntimeWatchService
    project_service: ProjectAgentService
    project_id: str
    workspace_id: str
    change_set_id: str
    validation_contract_id: str
    validation_run_id: str
    validation_ref: str
    target_root: str


def _contract_payload(target_root: str, *, name: str, command: str) -> dict[str, object]:
    return {
        "recipe_version_id": "recipe_python_cpu@1.0.0",
        "project": {"name": name, "workdir": target_root},
        "entry": {"command": command, "expected_outputs": ["result.txt"]},
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


def _wait_run(service: RunService, run_id: str, *, timeout: float = 90) -> RunRecord:
    deadline = time.monotonic() + timeout
    states: list[str] = []
    while time.monotonic() < deadline:
        run = service.reconcile_once(run_id)
        states.append(run.state.value)
        if run.state in {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}:
            return run
        time.sleep(0.4)
    raise RuntimeError(f"Run timed out: {run_id} {states}")


def _register_evidence(
    context: SmokeContext,
    run_id: str,
    logical_path: str,
    payload: dict[str, object],
) -> str:
    artifact = context.evidence_store.write_text(
        run_id=run_id,
        logical_path=logical_path,
        content=json.dumps(payload, sort_keys=True) + "\n",
        content_type="application/json",
    )
    ref = f"evidence://runs/{run_id}/{logical_path}"
    context.run_store.upsert_evidence_objects(
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


def _finish_collection(store: RunStore, run_id: str) -> None:
    while tasks := store.acquire_due_collection_tasks(
        lease_owner="a4-d1-collector",
        limit=20,
        lease_seconds=60,
    ):
        selected = [task for task in tasks if task.run_id == run_id]
        for task in selected:
            store.mark_collection_task_succeeded(
                task.task_id,
                lease_owner="a4-d1-collector",
                payload={"artifacts": [], "warnings": []},
            )
        for task in tasks:
            if task.run_id != run_id:
                store.mark_collection_task_succeeded(
                    task.task_id,
                    lease_owner="a4-d1-collector",
                    payload={"artifacts": [], "warnings": []},
                )
    state = store.get_run(run_id).collection_state
    if state is not CollectionState.SUCCEEDED:
        raise RuntimeError(f"terminal collection did not complete: {run_id} {state}")


def _wait_watch(context: SmokeContext, run: RunRecord) -> str:
    if not context.watch_service.on_run_terminal(run_id=run.run_id, owner=run.owner):
        raise RuntimeError("Runtime Watch terminal drain was not scheduled")
    context.run_store.defer_logs_finalize_for_runtime_watch(run.run_id)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = context.watch_service.tick()
        watch = context.watch_store.get_watch_for_run(run.run_id, owner=run.owner)
        if watch.state is RuntimeWatchState.STOPPED:
            break
        if result.errors:
            raise RuntimeError(f"Runtime Watch errors: {result.errors}")
        time.sleep(1.05)
    else:
        raise RuntimeError(f"Runtime Watch did not drain: {run.run_id}")
    chunks = []
    for stream in ("stdout", "stderr"):
        for segment in context.watch_store.list_segments(
            run.run_id,
            owner=run.owner,
            stream=stream,
        ):
            chunks.append(
                context.watch_store.read_segment_content(
                    segment.segment_id,
                    owner=run.owner,
                )
            )
    return b"".join(chunks).decode("utf-8", errors="replace")


def _formal_case(
    context: SmokeContext,
    *,
    key: str,
    command: str,
    expected_state: RunState,
    cancel: bool = False,
) -> tuple[RunRecord, str]:
    session, _ = context.session_service.create_session(
        owner="alice",
        request_key=f"a4-d1-{key}-session",
        profile_id="experiment_builder",
        model_profile_id="faux-default",
        source={"project_id": context.project_id, "workspace_id": context.workspace_id},
    )
    payload = _contract_payload(context.target_root, name=f"a4-d1-{key}", command=command)
    approval = context.project_service.prepare_formal_run(
        project_id=context.project_id,
        workspace_id=context.workspace_id,
        change_set_id=context.change_set_id,
        owner="alice",
        session_id=session.session_id,
        validation_contract_id=context.validation_contract_id,
        validation_run_id=context.validation_run_id,
        validation_evidence_refs=(context.validation_ref,),
        formal_contract_payload=payload,
    )
    formal = context.project_service.approve_and_submit_formal_run(
        project_id=context.project_id,
        workspace_id=context.workspace_id,
        change_set_id=context.change_set_id,
        owner="alice",
        session_id=session.session_id,
        validation_contract_id=context.validation_contract_id,
        validation_run_id=context.validation_run_id,
        validation_evidence_refs=(context.validation_ref,),
        formal_contract_payload=payload,
        approved_digest=approval.approval_digest,
    )
    if cancel:
        time.sleep(0.5)
        terminal = context.run_service.cancel(formal.run.run_id)
    else:
        terminal = _wait_run(context.run_service, formal.run.run_id)
    if terminal.state is not expected_state:
        raise RuntimeError(
            f"formal {key} state mismatch: {terminal.state.value} != {expected_state.value}"
        )
    log_text = _wait_watch(context, terminal)
    _finish_collection(context.run_store, terminal.run_id)
    _register_evidence(
        context,
        terminal.run_id,
        "derived/formal-result.json",
        {
            "scheduler_state": terminal.state.value,
            "exit_code": terminal.exit_code,
            "job_id": terminal.job_id,
            "resource_plan": terminal.resource_plan,
            "scientific_validity": "not_assessed",
        },
    )
    result = RuntimeReconcileWorker(
        service=context.run_service,
        agent_session_service=context.session_service,
        runtime_watch_service=context.watch_service,
        formal_result_evidence_binder=context.evidence_binder,
        worker_id=f"a4-d1-{key}-result",
    ).tick()
    if result.formal_results_succeeded != 1 or result.formal_result_errors:
        raise RuntimeError(f"formal result handoff failed: {result.formal_result_errors}")
    request_key = f"formal-run:{terminal.run_id}:result-explanation"
    with context.session_service.store.connect() as connection:
        row = connection.execute(
            "SELECT turn_id, message FROM agent_turns WHERE session_id = ? AND request_key = ?",
            (session.session_id, request_key),
        ).fetchone()
    if row is None or "does not establish scientific validity" not in str(row["message"]):
        raise RuntimeError("formal result explanation Turn was not created")
    return terminal, log_text


def _build_context(
    root: Path,
    executor: DockerComposeExecutor,
    backend: DockerSimulatorCommandBackend,
    target_root: str,
) -> SmokeContext:
    database = root / "pilot107.db"
    project_store = SQLiteProjectStore(database)
    contract_store = ContractStore(database)
    contract_service = ContractService(
        catalog=RecipeCatalog(
            store=contract_store,
            partition_qos={
                "Students": ("qos_stu_default", "qos_stu_medium_2gpu"),
            },
            default_partition="Students",
            default_qos="qos_stu_default",
        ),
        store=contract_store,
        partition_qos={"Students": ("qos_stu_default", "qos_stu_medium_2gpu")},
    )
    control = SQLiteControlRepository(database)
    run_store = RunStore(database)
    run_service = RunService(
        store=run_store,
        backend=backend,
        control_repository=control,
        dispatcher_id="a4-d1-run",
        submission_retry_delay_seconds=0,
    )
    evidence_store = EvidenceStore(root / "evidence")
    evidence_binder = EvidenceBinder(store=run_store, evidence_root=evidence_store.root)
    session_service = AgentSessionService(
        store=SQLiteAgentSessionStore(database),
        control_repository=control,
    )
    local_root = root / "workspace"
    local_root.mkdir()
    content = b"print('published-formal-code')\n"
    (local_root / "main.py").write_bytes(content)
    project = project_store.create_project(
        owner="alice",
        origin="blank",
        goal="D1 approval-bound formal run",
        request_key="a4-d1-project",
    )
    workspace_id = "workspace-a4-d1"
    snapshot_digest = hashlib.sha256(b"blank-a4-d1").hexdigest()
    project_store.save_workspace(
        AgentWorkspaceRecord(
            workspace_id=workspace_id,
            project_id=project.project_id,
            owner="alice",
            local_root=str(local_root),
            snapshot=WorkspaceSnapshot(
                source_ref="/__pilot107_blank__",
                digest=snapshot_digest,
                entries=(),
                captured_at=datetime.now(UTC).isoformat(),
            ),
            created_at=datetime.now(UTC).isoformat(),
            updated_at=datetime.now(UTC).isoformat(),
        )
    )
    change_set_id = "changeset-a4-d1"
    change_digest = hashlib.sha256(content + snapshot_digest.encode()).hexdigest()
    project_store.save_change_set(
        WorkspaceChangeSet(
            change_set_id=change_set_id,
            project_id=project.project_id,
            workspace_id=workspace_id,
            owner="alice",
            base_snapshot_digest=snapshot_digest,
            digest=change_digest,
            state=WorkspaceChangeSetState.APPROVED,
            version=1,
            files=(
                WorkspaceFileChange(
                    path="main.py",
                    operation="create",
                    before_sha256=None,
                    after_sha256=hashlib.sha256(content).hexdigest(),
                    diff_sha256=hashlib.sha256(b"create main.py").hexdigest(),
                    size_bytes=len(content),
                ),
            ),
            sandbox_results=(),
            approval=WorkspaceApproval(
                actor="alice",
                approved_digest=change_digest,
                approved_at=datetime.now(UTC).isoformat(),
            ),
            created_at=datetime.now(UTC).isoformat(),
            updated_at=datetime.now(UTC).isoformat(),
        ),
        diff_text="--- /dev/null\n+++ b/main.py\n",
    )
    relay = ComposePublicationRelay(executor)
    _run_checked(executor, ["mkdir", "-p", target_root])
    publisher = WorkspacePublisher(
        store=project_store,
        relay=relay,
        owner_roots=("/public/home/{owner}",),
    )
    publisher.prepare(change_set_id, actor="alice", target_root=target_root)
    publication = publisher.publish(change_set_id, actor="alice")
    if publication.state is not WorkspacePublicationState.PUBLISHED:
        raise RuntimeError(f"blank project publication failed: {publication.state}")

    validation_contract = contract_service.create(
        owner="alice",
        payload=_contract_payload(
            target_root,
            name="a4-d1-validation",
            command="printf 'validation-ok\\n' > result.txt",
        ),
    )
    validation_run = run_service.submit(contract_service.to_submit_request(validation_contract))
    validation_run = _wait_run(run_service, validation_run.run_id)
    if validation_run.state is not RunState.SUCCEEDED:
        raise RuntimeError("D1 validation Run failed")

    watch_store = SQLiteRuntimeWatchStore(database, segment_root=root / "watch-segments")

    def terminal_handoff(run_id: str) -> None:
        run_store.release_logs_finalize_after_runtime_watch(run_id)
        run = run_store.get_run(run_id)
        if run.lineage_reason == "agent_formal_run" and run.contract_id is not None:
            session_service.enqueue_formal_result_handoff(
                run=run,
                contract=contract_service.get(run.contract_id),
            )

    watch_service = RuntimeWatchService(
        store=watch_store,
        transport_for_connection=lambda _connection_id: ComposeEvidenceTransport(
            executor, "/public/home/alice"
        ),
        source_resolver=RunStoreRuntimeLogSourceResolver(
            run_store=run_store,
            allowed_roots=("/public/home/alice",),
        ),
        worker_id="a4-d1-watch",
        policy=RuntimeWatchPolicy(active_poll_seconds=1, quiet_poll_seconds=1),
        on_terminal_drained=terminal_handoff,
        default_connection_id="d1",
    )
    project_service = ProjectAgentService(
        store=project_store,
        workspace_root=root / "workspaces",
        sandbox=SandboxExecutor(store=project_store),
        publisher=publisher,
        contract_service=contract_service,
        run_service=run_service,
        runtime_watch_service=watch_service,
        agent_session_service=session_service,
        evidence_binder=evidence_binder,
    )
    context = SmokeContext(
        database=database,
        project_store=project_store,
        contract_service=contract_service,
        run_store=run_store,
        run_service=run_service,
        evidence_store=evidence_store,
        evidence_binder=evidence_binder,
        session_service=session_service,
        watch_store=watch_store,
        watch_service=watch_service,
        project_service=project_service,
        project_id=project.project_id,
        workspace_id=workspace_id,
        change_set_id=change_set_id,
        validation_contract_id=validation_contract.contract_id,
        validation_run_id=validation_run.run_id,
        validation_ref="",
        target_root=target_root,
    )
    context.validation_ref = _register_evidence(
        context,
        validation_run.run_id,
        "validation/result.json",
        {"checks": "passed", "job_id": validation_run.job_id},
    )
    return context


def _assert_publish_conflict(
    root: Path,
    executor: DockerComposeExecutor,
    target_root: str,
) -> None:
    store = SQLiteProjectStore(root / "conflict.db")
    relay = ComposePublicationRelay(executor)
    _run_checked(executor, ["mkdir", "-p", target_root])
    _run_checked(executor, ["tee", f"{target_root}/main.py"], stdin="old\n")
    before = b"old\n"
    after = b"approved\n"
    local = root / "conflict-workspace"
    local.mkdir()
    (local / "main.py").write_bytes(after)
    project = store.create_project(
        owner="alice", origin="existing", goal="conflict", request_key="conflict"
    )
    workspace_id = "workspace-conflict"
    snapshot = "c" * 64
    store.save_workspace(
        AgentWorkspaceRecord(
            workspace_id=workspace_id,
            project_id=project.project_id,
            owner="alice",
            local_root=str(local),
            snapshot=WorkspaceSnapshot(
                source_ref=target_root,
                digest=snapshot,
                entries=(),
                captured_at=datetime.now(UTC).isoformat(),
            ),
            created_at=datetime.now(UTC).isoformat(),
            updated_at=datetime.now(UTC).isoformat(),
        )
    )
    digest = hashlib.sha256(after).hexdigest()
    change = WorkspaceChangeSet(
        change_set_id="changeset-conflict",
        project_id=project.project_id,
        workspace_id=workspace_id,
        owner="alice",
        base_snapshot_digest=snapshot,
        digest=digest,
        state=WorkspaceChangeSetState.APPROVED,
        version=1,
        files=(
            WorkspaceFileChange(
                path="main.py",
                operation="modify",
                before_sha256=hashlib.sha256(before).hexdigest(),
                after_sha256=hashlib.sha256(after).hexdigest(),
                diff_sha256="d" * 64,
                size_bytes=len(after),
            ),
        ),
        sandbox_results=(),
        approval=WorkspaceApproval(
            actor="alice",
            approved_digest=digest,
            approved_at=datetime.now(UTC).isoformat(),
        ),
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
    )
    store.save_change_set(change, diff_text="modify")
    publisher = WorkspacePublisher(
        store=store,
        relay=relay,
        owner_roots=("/public/home/{owner}",),
    )
    publisher.prepare(change.change_set_id, actor="alice")
    _run_checked(executor, ["tee", f"{target_root}/main.py"], stdin="external\n")
    result = publisher.publish(change.change_set_id, actor="alice")
    if result.state is not WorkspacePublicationState.CONFLICTED:
        raise RuntimeError("live publication conflict did not fail closed")
    remote = _run_checked(executor, ["cat", f"{target_root}/main.py"])
    if remote != "external\n":
        raise RuntimeError("live publication conflict overwrote external content")


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
    target_root = f"/public/home/alice/pilot107-a4-d1-{suffix}"
    conflict_root = f"/public/home/alice/pilot107-a4-conflict-{suffix}"
    try:
        with tempfile.TemporaryDirectory(prefix="pilot107-a4-d1-") as temporary:
            temporary_root = Path(temporary)
            context = _build_context(temporary_root, executor, backend, target_root)
            _assert_publish_conflict(temporary_root, executor, conflict_root)

            succeeded, success_logs = _formal_case(
                context,
                key="success",
                command="printf 'formal-success\\n' | tee result.txt",
                expected_state=RunState.SUCCEEDED,
            )
            failed, failure_logs = _formal_case(
                context,
                key="failure",
                command="printf 'formal-exit-42\\n'; exit 42",
                expected_state=RunState.FAILED,
            )
            cancelled, _ = _formal_case(
                context,
                key="cancel",
                command="printf 'formal-cancel-started\\n'; sleep 30",
                expected_state=RunState.CANCELLED,
                cancel=True,
            )
            if "formal-success" not in success_logs:
                raise RuntimeError("incremental Runtime Watch missed success stdout")
            if "formal-exit-42" not in failure_logs:
                raise RuntimeError("incremental Runtime Watch missed failure stdout")

            restarted_sessions = AgentSessionService(
                store=SQLiteAgentSessionStore(context.database),
                control_repository=SQLiteControlRepository(context.database),
            )
            restarted_watch = RuntimeWatchService(
                store=SQLiteRuntimeWatchStore(
                    context.database,
                    segment_root=temporary_root / "watch-segments",
                ),
                transport_for_connection=lambda _connection_id: ComposeEvidenceTransport(
                    executor, "/public/home/alice"
                ),
                source_resolver=RunStoreRuntimeLogSourceResolver(
                    run_store=RunStore(context.database),
                    allowed_roots=("/public/home/alice",),
                ),
                worker_id="a4-d1-restarted-watch",
            )
            replay = RuntimeReconcileWorker(
                service=RunService(store=RunStore(context.database), backend=backend),
                agent_session_service=restarted_sessions,
                runtime_watch_service=restarted_watch,
                formal_result_evidence_binder=EvidenceBinder(
                    store=RunStore(context.database),
                    evidence_root=context.evidence_store.root,
                ),
                worker_id="a4-d1-restarted-result",
            ).tick()
            if replay.formal_results_succeeded != 0:
                raise RuntimeError("Worker restart duplicated a formal result explanation")

            report = {
                "schema": "pilot107.agent-a4-live-smoke/v1",
                "blank_project_gold_path": True,
                "publish_conflict": True,
                "success_run": {
                    "run_id": succeeded.run_id,
                    "job_id": succeeded.job_id,
                    "state": succeeded.state.value,
                },
                "failed_run": {
                    "run_id": failed.run_id,
                    "job_id": failed.job_id,
                    "state": failed.state.value,
                    "exit_code": failed.exit_code,
                },
                "cancelled_run": {
                    "run_id": cancelled.run_id,
                    "job_id": cancelled.job_id,
                    "state": cancelled.state.value,
                },
                "runtime_watch_incremental_logs": True,
                "terminal_evidence_bound": True,
                "result_explanations": 3,
                "scientific_validity_not_inferred": True,
                "browser_store_reconnect_recovered": True,
                "worker_restart_recovered": True,
            }
            print(json.dumps(report, sort_keys=True))
    finally:
        for path in (target_root, conflict_root):
            executor.run(
                ["rm", "-rf", path],
                user="alice",
                timeout_seconds=20,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
