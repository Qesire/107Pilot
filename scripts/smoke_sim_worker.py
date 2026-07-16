from __future__ import annotations

import sys
import time
from pathlib import Path

from pilot107.adapters.slurm import (
    DockerComposeExecutor,
    DockerComposeTarget,
    DockerSimulatorCommandBackend,
)
from pilot107.core.resources import ResourcePlan
from pilot107.core.run_service import RunService, RunSubmitRequest
from pilot107.core.run_store import RunStore
from pilot107.core.states import RunState
from pilot107.worker.runtime_worker import RuntimeReconcileWorker


def main() -> int:
    root = Path(__file__).resolve().parents[1]
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
    worker = RuntimeReconcileWorker(service=service, batch_size=10)

    run = service.submit(
        RunSubmitRequest(
            owner="alice",
            workdir=Path("/public/home/alice"),
            script="#!/bin/bash\nhostname\nsleep 1\necho worker-done\n",
            resource_plan=ResourcePlan(
                partition="Students",
                qos="qos_stu_medium_2gpu",
                nodes=1,
                ntasks=1,
                cpus_per_task=1,
                time_limit="00:05:00",
            ),
        )
    )

    final_run = run
    last_errors: list[str] = []
    checked_total = 0
    for _ in range(20):
        result = worker.tick()
        checked_total += result.checked
        last_errors = [error.message for error in result.errors]
        final_run = store.get_run(run.run_id)
        if final_run.state in {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}:
            break
        time.sleep(1)

    if final_run.state != RunState.SUCCEEDED or final_run.exit_code != "0:0":
        print(
            f"unexpected worker final run: {final_run}; "
            f"checked_total={checked_total}; last_errors={last_errors}",
            file=sys.stderr,
        )
        return 1

    events = [event.event_type for event in store.list_events(final_run.run_id)]
    tasks = [task["task_type"] for task in store.list_collection_tasks(final_run.run_id)]
    print(
        f"worker smoke {final_run.run_id} job={final_run.job_id} "
        f"state={final_run.state} exit={final_run.exit_code} checked={checked_total}"
    )
    print("events=" + ",".join(events))
    print("tasks=" + ",".join(tasks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
