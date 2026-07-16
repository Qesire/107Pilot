from __future__ import annotations

import json
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
from pilot107.core.states import CapsuleState, CollectionState, RunState
from pilot107.worker.capsule import RawCapsuleService, verify_raw_capsule
from pilot107.worker.evidence import DockerSlurmEvidenceCollector, EvidenceStore
from pilot107.worker.runtime_worker import RuntimeReconcileWorker


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    compose_dir = root / "simulator" / "compose"
    runtime_dir = root / "data" / "phase0"
    store = RunStore(runtime_dir / "pilot107.db")
    evidence_store = EvidenceStore(runtime_dir / "evidence")
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
        worker_id="smoke-capsule-worker",
    )

    run = service.submit(
        RunSubmitRequest(
            owner="alice",
            workdir=Path("/public/home/alice"),
            script=(
                "#!/bin/bash\n"
                "hostname\n"
                "mkdir -p pilot107-capsule-output\n"
                "echo capsule-output-ok > pilot107-capsule-output/result.txt\n"
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

    final_run = run
    task_errors: list[str] = []
    for _ in range(40):
        result = worker.tick()
        task_errors = [error.message for error in result.task_errors]
        final_run = store.get_run(run.run_id)
        if (
            final_run.state in {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}
            and final_run.collection_state == CollectionState.SUCCEEDED
        ):
            break
        time.sleep(1)

    if final_run.collection_state != CollectionState.SUCCEEDED:
        print(
            f"evidence did not collect: run={final_run}; task_errors={task_errors}", file=sys.stderr
        )
        return 1

    capsule = RawCapsuleService(
        store=store,
        evidence_store=evidence_store,
        capsule_root=runtime_dir / "capsules",
    ).build_raw_capsule(run.run_id)
    verify = verify_raw_capsule(capsule.capsule_dir)
    stored = store.get_run(run.run_id)
    if not verify.valid or stored.capsule_state != CapsuleState.READY:
        print(f"unexpected capsule result: verify={verify}; stored={stored}", file=sys.stderr)
        return 1

    manifest = json.loads((capsule.capsule_dir / "manifest.json").read_text(encoding="utf-8"))
    paths = {item["logical_path"] for item in manifest["files"]}
    required = {
        "submission/user_script.original.sh",
        "submission/execution_wrapper.generated.sh",
        "slurm/accounting.json",
        "environment/summary.json",
        "outputs/inventory.json",
        "derived/result_summary.v1.json",
        "logs/stdout.tail.json",
    }
    missing = required - paths
    if missing:
        print(f"capsule manifest missing paths: {sorted(missing)}", file=sys.stderr)
        return 1

    print(
        f"capsule smoke {run.run_id} capsule={capsule.capsule_id} "
        f"files={capsule.files_copied} checked={verify.checked_files} state={stored.capsule_state}"
    )
    print(f"capsule_dir={capsule.capsule_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
