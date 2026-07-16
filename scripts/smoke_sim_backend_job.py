from __future__ import annotations

import sys
import time
from pathlib import Path

from pilot107.adapters.slurm import (
    DockerComposeExecutor,
    DockerComposeTarget,
    DockerSimulatorCommandBackend,
    SlurmBackendError,
    SubmitIntent,
)
from pilot107.core.resources import ResourcePlan
from pilot107.core.states import RunState


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
        timeout_seconds=20.0,
    )
    receipt = backend.submit(
        SubmitIntent(
            user="alice",
            workdir=Path("/public/home/alice"),
            script="#!/bin/bash\nhostname\nsleep 1\necho done\n",
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

    snapshot = None
    last_error: Exception | None = None
    for _ in range(20):
        try:
            snapshot = backend.get_job(user="alice", job_id=receipt.job_id)
        except SlurmBackendError as exc:
            last_error = exc
            time.sleep(1)
            continue
        if snapshot.run_state in {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}:
            break
        time.sleep(1)

    if snapshot is None:
        print(f"job {receipt.job_id} did not produce a snapshot: {last_error}", file=sys.stderr)
        return 1
    if snapshot.run_state != RunState.SUCCEEDED or snapshot.exit_code != "0:0":
        print(f"unexpected final state: {snapshot}", file=sys.stderr)
        return 1

    stdout_path = snapshot.stdout_path or Path(f"/public/home/alice/slurm-{receipt.job_id}.out")
    stdout = executor.run(["cat", str(stdout_path)], user="alice", timeout_seconds=10.0)
    if stdout.returncode != 0:
        print(stdout.stderr, file=sys.stderr)
        return 1

    print(
        f"backend smoke job {receipt.job_id} {snapshot.owner} "
        f"{snapshot.run_state} {snapshot.exit_code}"
    )
    print(stdout.stdout.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
