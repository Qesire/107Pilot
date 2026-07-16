from __future__ import annotations

import json
import sys
from pathlib import Path

from pilot107.adapters.slurm import (
    DockerComposeExecutor,
    DockerComposeTarget,
    DockerSimulatorCommandBackend,
)
from pilot107.core.resources import ResourcePlan
from pilot107.core.run_service import RunService, RunSubmitRequest
from pilot107.core.run_store import RunStore
from pilot107.core.states import CollectionState, RunState
from pilot107.worker.service import build_worker_service, config_from_env


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    compose_dir = root / "simulator" / "compose"
    runtime_dir = root / "data" / "phase0"
    health_path = runtime_dir / "worker-service-health.json"

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
    run = service.submit(
        RunSubmitRequest(
            owner="alice",
            workdir=Path("/public/home/alice"),
            script=(
                "#!/bin/bash\n"
                "hostname\n"
                "echo worker-service\n"
                "mkdir -p pilot107-worker-service-output\n"
                "echo ok > pilot107-worker-service-output/result.txt\n"
            ),
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

    worker_service = build_worker_service(
        config_from_env(
            {
                "PILOT107_DB_PATH": str(runtime_dir / "pilot107.db"),
                "PILOT107_EVIDENCE_ROOT": str(runtime_dir / "evidence"),
                "PILOT107_WORKER_BACKEND": "docker-compose-command",
                "PILOT107_WORKER_ID": "smoke-worker-service",
                "PILOT107_WORKER_INTERVAL_SECONDS": "1",
                "PILOT107_WORKER_BATCH_SIZE": "20",
                "PILOT107_COMMAND_TIMEOUT_SECONDS": "20",
                "PILOT107_COMPOSE_FILE": str(compose_dir / "compose.yml"),
                "PILOT107_COMPOSE_ENV_FILE": str(compose_dir / ".env.example"),
                "PILOT107_COMPOSE_WORKDIR": str(compose_dir),
                "PILOT107_ALLOWED_ROOTS": "/public/home/alice",
                "PILOT107_WORKER_HEALTH_PATH": str(health_path),
            },
            project_root=root,
        )
    )
    result = worker_service.run_ticks(max_ticks=40, stop_when_idle=True)
    final_run = store.get_run(run.run_id)

    if (
        final_run.state != RunState.SUCCEEDED
        or final_run.exit_code != "0:0"
        or final_run.collection_state != CollectionState.SUCCEEDED
    ):
        print(
            f"unexpected worker service final run: {final_run}; result={result}",
            file=sys.stderr,
        )
        return 1

    health = json.loads(health_path.read_text(encoding="utf-8"))
    if (
        health["worker_id"] != "smoke-worker-service"
        or health["backend"] != "docker-compose-command"
    ):
        print(f"unexpected worker health: {health}", file=sys.stderr)
        return 1

    print(
        f"worker service smoke {final_run.run_id} job={final_run.job_id} "
        f"state={final_run.state} collection={final_run.collection_state} "
        f"ticks_checked={result.checked} tasks={result.tasks_succeeded}/{result.tasks_checked}"
    )
    print("health=" + str(health_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
