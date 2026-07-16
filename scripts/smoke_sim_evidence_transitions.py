from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from pilot107.adapters.slurm import (
    DockerComposeExecutor,
    DockerComposeTarget,
    DockerSimulatorCommandBackend,
)
from pilot107.core.resources import ResourcePlan
from pilot107.core.run_service import RunService, RunSubmitRequest
from pilot107.core.run_store import RunRecord, RunStore
from pilot107.core.states import CollectionState, RunState
from pilot107.worker.evidence import DockerSlurmEvidenceCollector, EvidenceStore
from pilot107.worker.runtime_worker import RuntimeReconcileWorker


@dataclass(frozen=True)
class RuntimeStack:
    store: RunStore
    service: RunService
    worker: RuntimeReconcileWorker
    evidence_store: EvidenceStore


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    stack = _stack(root)
    try:
        failed = _failure_case(stack)
        cancelled = _cancel_case(stack)
    except Exception as exc:
        print(f"evidence transition smoke failed: {exc}", file=sys.stderr)
        return 1

    print(
        "evidence transition smoke "
        f"failed={failed.run_id}:{failed.job_id}:{failed.state}:{failed.exit_code} "
        f"cancelled={cancelled.run_id}:{cancelled.job_id}:{cancelled.state}:{cancelled.exit_code}"
    )
    return 0


def _stack(root: Path) -> RuntimeStack:
    compose_dir = root / "simulator" / "compose"
    runtime_dir = root / "data" / "phase0"
    store = RunStore(runtime_dir / "pilot107.db")
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
        timeout_seconds=20.0,
    )
    evidence_store = EvidenceStore(runtime_dir / "evidence")
    collector = DockerSlurmEvidenceCollector(
        store=evidence_store,
        executor=executor,
        allowed_roots=["/public/home/alice"],
        run_store=store,
        timeout_seconds=20.0,
    )
    service = RunService(store=store, backend=backend)
    worker = RuntimeReconcileWorker(
        service=service,
        batch_size=20,
        task_handler=collector,
        worker_id="smoke-evidence-transitions-worker",
    )
    return RuntimeStack(
        store=store,
        service=service,
        worker=worker,
        evidence_store=evidence_store,
    )


def _plan() -> ResourcePlan:
    return ResourcePlan(
        partition="Students",
        qos="qos_stu_medium_2gpu",
        nodes=1,
        ntasks=1,
        cpus_per_task=1,
        time_limit="00:05:00",
    )


def _submit(stack: RuntimeStack, *, script: str) -> RunRecord:
    return stack.service.submit(
        RunSubmitRequest(
            owner="alice",
            workdir=Path("/public/home/alice"),
            script=script,
            resource_plan=_plan(),
        )
    )


def _failure_case(stack: RuntimeStack) -> RunRecord:
    run = _submit(
        stack,
        script="#!/bin/bash\nhostname\necho failed-stdout\necho failed-stderr >&2\nexit 42\n",
    )
    final_run = _wait_for_collected(stack, run.run_id)
    if final_run.state != RunState.FAILED or not (final_run.exit_code or "").startswith("42:"):
        raise RuntimeError(f"unexpected failed run: {final_run}")
    _assert_evidence(stack, final_run, expected_state=RunState.FAILED, expected_exit_prefix="42:")
    return final_run


def _cancel_case(stack: RuntimeStack) -> RunRecord:
    run = _submit(
        stack,
        script="#!/bin/bash\nhostname\necho cancel-started\nsleep 60\necho should-not-print\n",
    )
    for _ in range(10):
        current = stack.service.reconcile_once(run.run_id)
        if current.state in {RunState.PENDING, RunState.RUNNING}:
            break
        time.sleep(1)
    cancelled = stack.service.cancel(run.run_id)
    if cancelled.state != RunState.CANCELLED:
        raise RuntimeError(f"unexpected cancel response: {cancelled}")
    final_run = _wait_for_collected(stack, run.run_id)
    if final_run.state != RunState.CANCELLED:
        raise RuntimeError(f"unexpected cancelled run: {final_run}")
    _assert_evidence(stack, final_run, expected_state=RunState.CANCELLED, expected_exit_prefix=None)
    return final_run


def _wait_for_collected(stack: RuntimeStack, run_id: str, *, max_ticks: int = 40) -> RunRecord:
    last_errors: list[str] = []
    for _ in range(max_ticks):
        result = stack.worker.tick()
        last_errors = [error.message for error in result.task_errors]
        run = stack.store.get_run(run_id)
        if (
            run.state in {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}
            and run.collection_state == CollectionState.SUCCEEDED
        ):
            return run
        time.sleep(1)
    run = stack.store.get_run(run_id)
    raise RuntimeError(f"run did not collect: {run}; last_errors={last_errors}")


def _assert_evidence(
    stack: RuntimeStack,
    run: RunRecord,
    *,
    expected_state: RunState,
    expected_exit_prefix: str | None,
) -> None:
    run_root = stack.evidence_store.run_root(run.run_id)
    required = [
        run_root / "submission" / "slurm_submit_response.json",
        run_root / "submission" / "user_script.original.sh",
        run_root / "submission" / "submitted_script.resolved.sh",
        run_root / "submission" / "execution_wrapper.generated.sh",
        run_root / "slurm" / "accounting.json",
        run_root / "slurm" / "job_detail.json",
        run_root / "logs" / "stdout.tail.json",
        run_root / "logs" / "stderr.tail.json",
        run_root / "environment" / "summary.json",
        run_root / "run" / "request" / "resource-plan.json",
        run_root / "run" / "request" / "submitted-script.sbatch",
        run_root / "run" / "request" / "sbatch-argv.json",
        run_root / "run" / "environment" / "basic.json",
        run_root / "run" / "timeline" / "events.jsonl",
        run_root / "outputs" / "inventory.json",
        run_root / "derived" / "result_summary.v1.json",
        run_root / "manifest" / "manifest.json",
    ]
    missing = [path.relative_to(run_root).as_posix() for path in required if not path.exists()]
    if missing:
        raise RuntimeError(f"missing evidence for {run.run_id}: {missing}")

    tasks = stack.store.list_collection_tasks(run.run_id)
    task_states = {task["task_type"]: task["state"] for task in tasks}
    if set(task_states.values()) != {"succeeded"}:
        raise RuntimeError(f"unexpected task states for {run.run_id}: {task_states}")

    manifest = json.loads((run_root / "manifest" / "manifest.json").read_text(encoding="utf-8"))
    if manifest["run_state"] != expected_state.value:
        raise RuntimeError(f"unexpected manifest state for {run.run_id}: {manifest['run_state']}")
    if expected_exit_prefix and not str(manifest["exit_code"]).startswith(expected_exit_prefix):
        raise RuntimeError(f"unexpected manifest exit for {run.run_id}: {manifest['exit_code']}")
    basic_environment = json.loads(
        (run_root / "run" / "environment" / "basic.json").read_text(encoding="utf-8")
    )
    if not basic_environment.get("python_version") or not basic_environment.get("python_path"):
        raise RuntimeError(
            f"runtime basic probe missing Python facts for {run.run_id}: {basic_environment}"
        )
    manifest_paths = {artifact["logical_path"] for artifact in manifest["artifacts"]}
    expected_paths = {path.relative_to(run_root).as_posix() for path in required[:-1]}
    if not expected_paths.issubset(manifest_paths):
        raise RuntimeError(
            f"manifest missing paths for {run.run_id}: {sorted(expected_paths - manifest_paths)}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
