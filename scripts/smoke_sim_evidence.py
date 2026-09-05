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
from pilot107.core.states import CollectionState, RunState
from pilot107.worker.evidence import DockerSlurmEvidenceCollector, EvidenceStore
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
        worker_id="smoke-evidence-worker",
    )

    run = service.submit(
        RunSubmitRequest(
            owner="alice",
            workdir=Path("/public/home/alice"),
            script=(
                "#!/bin/bash\n"
                "hostname\n"
                "echo evidence-stdout\n"
                "echo evidence-stderr >&2\n"
                "mkdir -p pilot107-smoke-output\n"
                "echo output-inventory-ok > pilot107-smoke-output/result.txt\n"
                "sleep 3\n"
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
    for _ in range(60):
        result = worker.tick()
        task_errors = [error.message for error in result.task_errors]
        final_run = store.get_run(run.run_id)
        if (
            final_run.state in {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}
            and final_run.collection_state == CollectionState.SUCCEEDED
        ):
            break
        time.sleep(1)

    run_root = evidence_store.run_root(run.run_id)
    required = [
        run_root / "submission" / "slurm_submit_response.json",
        run_root / "submission" / "user_script.original.sh",
        run_root / "submission" / "submitted_script.resolved.sh",
        run_root / "submission" / "execution_wrapper.generated.sh",
        run_root / "slurm" / "runtime_status.json",
        run_root / "slurm" / "accounting.json",
        run_root / "slurm" / "job_detail.json",
        run_root / "logs" / "stdout.tail.json",
        run_root / "logs" / "stderr.tail.json",
        run_root / "environment" / "summary.json",
        run_root / "outputs" / "inventory.json",
        run_root / "derived" / "result_summary.v1.json",
        run_root / "manifest" / "manifest.json",
    ]
    missing = [str(path) for path in required if not path.exists()]
    tasks = store.list_collection_tasks(run.run_id)
    task_states = {task["task_type"]: task["state"] for task in tasks}

    if (
        final_run.state != RunState.SUCCEEDED
        or final_run.exit_code != "0:0"
        or final_run.collection_state != CollectionState.SUCCEEDED
        or missing
        or set(task_states.values()) != {"succeeded"}
    ):
        print(
            "unexpected evidence smoke result: "
            f"run={final_run}; task_states={task_states}; missing={missing}; "
            f"task_errors={task_errors}",
            file=sys.stderr,
        )
        return 1

    manifest = json.loads((run_root / "manifest" / "manifest.json").read_text(encoding="utf-8"))
    manifest_paths = {artifact["logical_path"] for artifact in manifest["artifacts"]}
    expected_manifest_paths = {
        "submission/slurm_submit_response.json",
        "submission/user_script.original.sh",
        "submission/submitted_script.resolved.sh",
        "submission/execution_wrapper.generated.sh",
        "slurm/runtime_status.json",
        "slurm/accounting.json",
        "slurm/job_detail.json",
        "logs/stdout.tail.json",
        "logs/stderr.tail.json",
        "environment/summary.json",
        "outputs/inventory.json",
        "derived/result_summary.v1.json",
    }
    if not expected_manifest_paths.issubset(manifest_paths):
        print(
            "manifest missing expected paths: "
            f"{sorted(expected_manifest_paths - manifest_paths)}; "
            f"manifest_paths={sorted(manifest_paths)}",
            file=sys.stderr,
        )
        return 1
    inventory = json.loads((run_root / "outputs" / "inventory.json").read_text(encoding="utf-8"))
    inventory_paths = {item["relative_path"] for item in inventory["files"]}
    if "pilot107-smoke-output/result.txt" not in inventory_paths:
        print(
            f"inventory missing generated output: inventory_paths={sorted(inventory_paths)}",
            file=sys.stderr,
        )
        return 1
    indexed_paths = {obj.logical_path for obj in store.list_evidence_objects(run.run_id)}
    if not expected_manifest_paths.issubset(indexed_paths):
        print(
            "evidence index missing expected paths: "
            f"{sorted(expected_manifest_paths - indexed_paths)}; "
            f"indexed_paths={sorted(indexed_paths)}",
            file=sys.stderr,
        )
        return 1
    runtime_status = json.loads(
        (run_root / "slurm" / "runtime_status.json").read_text(encoding="utf-8")
    )
    runtime_job = runtime_status.get("job") or {}
    if (
        runtime_status.get("availability") != "known"
        or runtime_job.get("owner") != "alice"
        or runtime_job.get("partition") != "Students"
        or runtime_job.get("state") not in {"PENDING", "RUNNING"}
    ):
        print(f"unexpected runtime status: {runtime_status}", file=sys.stderr)
        return 1
    accounting = json.loads((run_root / "slurm" / "accounting.json").read_text(encoding="utf-8"))
    records = accounting.get("records") or []
    if (
        not records
        or records[0].get("owner") != "alice"
        or records[0].get("account") != "students"
        or records[0].get("qos") != "qos_stu_medium_2gpu"
    ):
        print(f"unexpected terminal accounting: {accounting}", file=sys.stderr)
        return 1
    summary = json.loads(
        (run_root / "derived" / "result_summary.v1.json").read_text(encoding="utf-8")
    )
    if summary["run_state"] != "SUCCEEDED" or summary["outputs"]["file_count"] < 1:
        print(f"unexpected result summary: {summary}", file=sys.stderr)
        return 1
    print(
        f"evidence smoke {final_run.run_id} job={final_run.job_id} "
        f"state={final_run.state} collection={final_run.collection_state} "
        f"runtime={runtime_job.get('state')}:{runtime_job.get('reason')} "
        f"artifacts={len(manifest['artifacts'])} objects={len(indexed_paths)}"
    )
    print("tasks=" + ",".join(f"{name}:{state}" for name, state in sorted(task_states.items())))
    print(f"evidence_root={run_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
