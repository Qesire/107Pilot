from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

from pilot107.adapters.slurm import (
    DockerComposeExecutor,
    DockerComposeTarget,
    SlurmTransportError,
)
from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.core.run_store import RunRecord, RunStore
from pilot107.core.states import (
    CapsuleState,
    CollectionState,
    DiagnosisState,
    ResultStatus,
    RunState,
)
from pilot107.worker.evidence import DockerSlurmEvidenceCollector, EvidenceStore


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    compose_dir = root / "simulator" / "compose"
    runtime_dir = root / "data" / "phase0"
    suffix = uuid4().hex[:10]
    executor = DockerComposeExecutor(
        DockerComposeTarget(
            compose_file=compose_dir / "compose.yml",
            env_file=compose_dir / ".env.example",
            workdir=compose_dir,
        )
    )
    evidence_store = EvidenceStore(runtime_dir / "evidence")
    store = RunStore(runtime_dir / f"permission-{suffix}.db")
    collector = DockerSlurmEvidenceCollector(
        store=evidence_store,
        executor=executor,
        allowed_roots=["/public/home/alice"],
        run_store=store,
        timeout_seconds=20.0,
    )

    try:
        alice_run = _alice_allowed_case(executor, collector, store, suffix)
        _bob_denied_case(executor, collector, store, suffix)
        _symlink_denied_case(executor, collector, store, suffix)
        _cross_run_query_case(runtime_dir, evidence_store, suffix)
    except Exception as exc:
        print(f"evidence permission smoke failed: {exc}", file=sys.stderr)
        return 1

    print(f"evidence permission smoke alice evidence ok run={alice_run.run_id}")
    print("bob path denied")
    print("symlink escape denied")
    print("cross-run query isolated")
    return 0


def _alice_allowed_case(
    executor: DockerComposeExecutor,
    collector: DockerSlurmEvidenceCollector,
    store: RunStore,
    suffix: str,
) -> RunRecord:
    job_id = f"aliceperm{suffix}"
    _write_container_file(
        executor,
        path=f"/public/home/alice/slurm-{job_id}.out",
        content="alice allowed stdout\n",
        owner="alice",
    )
    _write_container_file(
        executor,
        path=f"/public/home/alice/slurm-{job_id}.err",
        content="alice allowed stderr\n",
        owner="alice",
    )
    run = _run_record(run_id=f"run_perm_alice_{suffix}", owner="alice", job_id=job_id)
    _persist_run(store, run)
    result = collector.collect(run=run, task_type="logs_finalize")
    logical_paths = {artifact.logical_path for artifact in result.artifacts}
    if "logs/stdout.tail.json" not in logical_paths:
        raise RuntimeError(f"alice evidence missing stdout artifact: {logical_paths}")
    return run


def _bob_denied_case(
    executor: DockerComposeExecutor,
    collector: DockerSlurmEvidenceCollector,
    store: RunStore,
    suffix: str,
) -> None:
    job_id = f"bobperm{suffix}"
    _write_container_file(
        executor,
        path=f"/public/home/bob/slurm-{job_id}.out",
        content="bob secret stdout\n",
        owner="bob",
    )
    run = _run_record(run_id=f"run_perm_bob_{suffix}", owner="bob", job_id=job_id)
    _persist_run(store, run)
    try:
        collector.collect(run=run, task_type="logs_finalize")
    except SlurmTransportError:
        return
    raise RuntimeError("bob path was not denied")


def _symlink_denied_case(
    executor: DockerComposeExecutor,
    collector: DockerSlurmEvidenceCollector,
    store: RunStore,
    suffix: str,
) -> None:
    job_id = f"linkperm{suffix}"
    _write_container_file(
        executor,
        path=f"/public/home/bob/secret-{suffix}.out",
        content="bob target through alice symlink\n",
        owner="bob",
    )
    link_path = f"/public/home/alice/slurm-{job_id}.out"
    executor.run(["rm", "-f", link_path], timeout_seconds=20.0)
    link_result = executor.run(
        ["ln", "-s", f"/public/home/bob/secret-{suffix}.out", link_path],
        user="alice",
        timeout_seconds=20.0,
    )
    if link_result.returncode != 0:
        raise RuntimeError(link_result.stderr.strip() or "failed to create symlink")
    run = _run_record(run_id=f"run_perm_link_{suffix}", owner="alice", job_id=job_id)
    _persist_run(store, run)
    try:
        collector.collect(run=run, task_type="logs_finalize")
    except SlurmTransportError:
        return
    raise RuntimeError("symlink escape was not denied")


def _cross_run_query_case(runtime_dir: Path, evidence_store: EvidenceStore, suffix: str) -> None:
    db_path = runtime_dir / f"permission-query-{suffix}.db"
    store = RunStore(db_path)
    run_a = store.create_run(
        run_id=f"run_perm_query_a_{suffix}",
        owner="alice",
        workdir="/public/home/alice",
        script="#!/bin/bash\n",
    )
    run_b = store.create_run(
        run_id=f"run_perm_query_b_{suffix}",
        owner="alice",
        workdir="/public/home/alice",
        script="#!/bin/bash\n",
    )
    evidence_store.write_text(
        run_id=run_a.run_id,
        logical_path="logs/stdout.tail.json",
        content='{"run":"a"}\n',
        content_type="application/json",
    )
    evidence_store.write_text(
        run_id=run_b.run_id,
        logical_path="logs/stdout.tail.json",
        content='{"run":"b"}\n',
        content_type="application/json",
    )
    payload = EvidenceQueryService(store=store, evidence_store=evidence_store).get_evidence_tree(
        run_a.run_id
    )
    encoded = str(payload)
    if run_b.run_id in encoded or "run\":\"b" in encoded:
        raise RuntimeError("query for run A leaked run B evidence")


def _write_container_file(
    executor: DockerComposeExecutor,
    *,
    path: str,
    content: str,
    owner: str,
) -> None:
    executor.write_text(path=path, content=content, owner=owner, timeout_seconds=20.0)
    chmod_result = executor.run(["chmod", "0600", path], timeout_seconds=20.0)
    if chmod_result.returncode != 0:
        raise RuntimeError(chmod_result.stderr.strip() or f"chmod failed for {path}")


def _run_record(*, run_id: str, owner: str, job_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        owner=owner,
        state=RunState.SUCCEEDED,
        collection_state=CollectionState.PENDING,
        diagnosis_state=DiagnosisState.PENDING,
        capsule_state=CapsuleState.PENDING,
        result_status=ResultStatus.COMPLETE,
        job_id=job_id,
        workdir=f"/public/home/{owner}",
        script="#!/bin/bash\n",
        exit_code="0:0",
        terminal_state="COMPLETED",
        submit_strategy="command",
        submit_response={},
        created_at="2026-07-10T00:00:00+00:00",
        updated_at="2026-07-10T00:00:00+00:00",
    )


def _persist_run(store: RunStore, run: RunRecord) -> None:
    store.create_run(
        run_id=run.run_id,
        owner=run.owner,
        workdir=run.workdir,
        script=run.script,
    )


if __name__ == "__main__":
    raise SystemExit(main())
