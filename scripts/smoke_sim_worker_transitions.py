from __future__ import annotations

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
from pilot107.core.states import RunState
from pilot107.worker.runtime_worker import RuntimeReconcileWorker


@dataclass(frozen=True)
class RuntimeStack:
    store: RunStore
    service: RunService
    worker: RuntimeReconcileWorker


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
    service = RunService(store=store, backend=backend)
    worker = RuntimeReconcileWorker(service=service, batch_size=20)
    return RuntimeStack(store=store, service=service, worker=worker)


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


def _wait_for_terminal(stack: RuntimeStack, run_id: str, *, max_ticks: int = 30) -> RunRecord:
    last_errors: list[str] = []
    for _ in range(max_ticks):
        result = stack.worker.tick()
        last_errors = [error.message for error in result.errors]
        run = stack.store.get_run(run_id)
        if run.state in {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}:
            return run
        time.sleep(1)
    run = stack.store.get_run(run_id)
    raise RuntimeError(f"run did not reach terminal state: {run}; last_errors={last_errors}")


def _task_types(stack: RuntimeStack, run_id: str) -> set[str]:
    return {task["task_type"] for task in stack.store.list_collection_tasks(run_id)}


def _assert_terminal_tasks(stack: RuntimeStack, run_id: str) -> None:
    task_types = _task_types(stack, run_id)
    expected = {"runtime_status", "terminal_accounting", "logs_finalize"}
    missing = expected - task_types
    if missing:
        raise RuntimeError(f"missing terminal tasks for {run_id}: {sorted(missing)}")


def _failure_case(stack: RuntimeStack) -> RunRecord:
    run = _submit(
        stack,
        script="#!/bin/bash\nhostname\necho intentional-failure\nexit 42\n",
    )
    final_run = _wait_for_terminal(stack, run.run_id)
    if final_run.state != RunState.FAILED or not (final_run.exit_code or "").startswith("42:"):
        raise RuntimeError(f"unexpected failure case result: {final_run}")
    _assert_terminal_tasks(stack, final_run.run_id)
    return final_run


def _cancel_case(stack: RuntimeStack) -> RunRecord:
    run = _submit(
        stack,
        script="#!/bin/bash\nhostname\nsleep 60\necho should-not-print\n",
    )
    # Give Slurm one polling chance to materialize the job before cancelling.
    for _ in range(10):
        current = stack.service.reconcile_once(run.run_id)
        if current.state in {RunState.PENDING, RunState.RUNNING}:
            break
        time.sleep(1)
    final_run = stack.service.cancel(run.run_id)
    if final_run.state != RunState.CANCELLED:
        raise RuntimeError(f"unexpected cancel case result: {final_run}")
    _assert_terminal_tasks(stack, final_run.run_id)
    return final_run


def _restart_recovery_case(root: Path) -> RunRecord:
    first_stack = _stack(root)
    run = _submit(
        first_stack,
        script="#!/bin/bash\nhostname\nsleep 1\necho restart-recovered\n",
    )

    # Simulate process restart: discard objects and recreate store/service/worker
    # from the same SQLite database before any reconcile happens.
    restarted_stack = _stack(root)
    final_run = _wait_for_terminal(restarted_stack, run.run_id)
    if final_run.state != RunState.SUCCEEDED or final_run.exit_code != "0:0":
        raise RuntimeError(f"unexpected restart recovery result: {final_run}")
    _assert_terminal_tasks(restarted_stack, final_run.run_id)
    return final_run


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    stack = _stack(root)
    try:
        failed = _failure_case(stack)
        cancelled = _cancel_case(stack)
        recovered = _restart_recovery_case(root)
    except Exception as exc:
        print(f"worker transition smoke failed: {exc}", file=sys.stderr)
        return 1

    print(
        "worker transition smoke "
        f"failed={failed.run_id}:{failed.job_id}:{failed.state}:{failed.exit_code} "
        f"cancelled={cancelled.run_id}:{cancelled.job_id}:{cancelled.state}:{cancelled.exit_code} "
        f"recovered={recovered.run_id}:{recovered.job_id}:{recovered.state}:{recovered.exit_code}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
