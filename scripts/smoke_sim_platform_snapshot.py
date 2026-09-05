#!/usr/bin/env python3
"""Live Docker smoke for owner-scoped PlatformSnapshot ingestion and reads."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from pilot107.adapters.platform_cli import ExecutorPlatformCliCollector
from pilot107.adapters.slurm import (
    DockerComposeExecutor,
    DockerComposeTarget,
    DockerSimulatorCommandBackend,
    SlurmBackendError,
)
from pilot107.api.http_app import build_api
from pilot107.core.platform_snapshot import ObservationSourceType
from pilot107.core.platform_snapshot_store import (
    PlatformSnapshotStore,
    SnapshotCollectionStatus,
)
from pilot107.core.resources import ResourcePlan
from pilot107.core.run_service import RunService, RunSubmitRequest
from pilot107.core.run_store import RunStore
from pilot107.core.states import RunState
from pilot107.core.user_entitlement import EntitlementDataQuality
from pilot107.core.user_entitlement_store import UserEntitlementStore
from pilot107.services.platform_compute_probe import (
    compute_runtime_probe_script,
    store_compute_runtime_probe_output,
)
from pilot107.services.platform_snapshot_service import PlatformSnapshotService
from pilot107.services.user_entitlement_service import UserEntitlementService


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    compose_root = root / "simulator" / "compose"
    executor = DockerComposeExecutor(
        DockerComposeTarget(
            compose_file=compose_root / "compose.yml",
            env_file=compose_root / ".env.example",
            workdir=compose_root,
            service="login-node-sim",
        )
    )
    collector = ExecutorPlatformCliCollector(
        executor=executor,
        user="alice",
        cwd="/public/home/alice",
    )
    with tempfile.TemporaryDirectory(prefix="pilot107-platform-smoke-") as temporary:
        runtime_root = Path(temporary)
        db_path = runtime_root / "pilot107.db"
        store = PlatformSnapshotStore(db_path)
        entitlement_store = UserEntitlementStore(db_path)
        record = PlatformSnapshotService(collector=collector).collect_and_store_login_snapshot(
            store=store,
            owner="alice",
            username="alice",
            home="/public/home/alice",
            source_type=ObservationSourceType.SIMULATOR,
            source_name="docker-slurm-25.11-login-node",
            ttl_seconds=300,
        )

        command_names = {
            str(command["name"])
            for command in record.payload.get("command_results", [])
        }
        required_commands = {
            "hostname",
            "pwd",
            "whoami",
            "python_version",
            "conda_env_list_json",
            "scontrol_show_part",
            "scontrol_show_nodes",
            "sinfo_pipe",
            "squeue_user_pipe",
        }
        if not required_commands.issubset(command_names):
            raise RuntimeError(f"platform commands missing: {required_commands - command_names}")
        partition_names = {
            str(partition["name"])
            for partition in record.payload.get("partitions", [])
        }
        if "Students" not in partition_names:
            raise RuntimeError(f"Students partition missing: {sorted(partition_names)}")

        encoded = json.dumps(record.payload, ensure_ascii=False, sort_keys=True)
        if "alice" in encoded or "/public/home/alice" in encoded:
            raise RuntimeError("raw platform snapshot retained owner identity or home path")

        entitlement = UserEntitlementService(collector=collector).collect_and_store(
            store=entitlement_store,
            owner="alice",
            username="alice",
            source_type=ObservationSourceType.SIMULATOR,
            source_name="docker-slurm-25.11-association",
            ttl_seconds=300,
        )
        if entitlement.data_quality != EntitlementDataQuality.AUTHORITATIVE:
            raise RuntimeError(f"entitlement is not authoritative: {entitlement.payload}")
        if entitlement.payload.get("default_account") != "students":
            raise RuntimeError(f"unexpected default account: {entitlement.payload}")
        association_qos = {
            qos
            for association in entitlement.payload["associations"]
            for qos in association["qos"]
        }
        if "qos_stu_medium_2gpu" not in association_qos:
            raise RuntimeError(f"student QoS missing from entitlement: {association_qos}")
        if "qos_gpu-a100" in association_qos:
            raise RuntimeError(f"unexpected GPU partition QoS entitlement: {association_qos}")

        api = build_api(
            db_path=db_path,
            evidence_root=runtime_root / "evidence",
            auth_required=True,
        )
        listed = api.handle_get(
            "/api/v1/platform/snapshots?limit=10",
            headers={"X-Pilot107-User": "alice"},
        )
        detail = api.handle_get(
            f"/api/v1/platform/snapshots/{record.snapshot_id}",
            headers={"X-Pilot107-User": "alice"},
        )
        forbidden = api.handle_get(
            f"/api/v1/platform/snapshots/{record.snapshot_id}",
            headers={"X-Pilot107-User": "bob"},
        )
        if listed.status != 200 or len(listed.payload.get("items", [])) != 1:
            raise RuntimeError(f"unexpected platform list response: {listed.payload}")
        if detail.status != 200:
            raise RuntimeError(f"unexpected platform detail response: {detail.payload}")
        safe_commands = detail.payload["snapshot"]["command_results"]
        if any(
            key in command
            for command in safe_commands
            for key in ("argv", "stdout", "stderr")
        ):
            raise RuntimeError("safe platform API exposed command argv or output")
        if forbidden.status != 404:
            raise RuntimeError(f"cross-owner detail did not return 404: {forbidden.payload}")
        entitlement_detail = api.handle_get(
            f"/api/v1/platform/entitlements/{entitlement.snapshot_id}",
            headers={"X-Pilot107-User": "alice"},
        )
        if entitlement_detail.status != 200:
            raise RuntimeError(f"entitlement API failed: {entitlement_detail.payload}")
        entitlement_command = entitlement_detail.payload["snapshot"]["command_results"][0]
        if any(key in entitlement_command for key in ("argv", "stdout", "stderr")):
            raise RuntimeError("safe entitlement API exposed command argv or output")

        allowed_contract = api.contract_service.create(
            owner="alice",
            payload=_contract_payload("qos_stu_medium_2gpu"),
        )
        allowed_findings = api.contract_service.preflight(allowed_contract).findings
        if "ENTITLEMENT.QOS_CONFIRMED" not in {item.code for item in allowed_findings}:
            raise RuntimeError(f"allowed entitlement was not confirmed: {allowed_findings}")
        denied_contract = api.contract_service.create(
            owner="alice",
            payload=_contract_payload("qos_gpu-a100"),
        )
        denied = api.contract_service.preflight(denied_contract)
        if denied.status != "BLOCK" or "ENTITLEMENT.QOS_NOT_ALLOWED" not in {
            item.code for item in denied.findings
        }:
            raise RuntimeError(f"ungranted QoS was not blocked: {denied.findings}")

        failed_commands = [
            command["name"]
            for command in record.payload["command_results"]
            if command["returncode"] != 0
        ]
        expected_status = (
            SnapshotCollectionStatus.PARTIAL
            if failed_commands
            else SnapshotCollectionStatus.COMPLETE
        )
        if record.collection_status != expected_status:
            raise RuntimeError(
                f"collection status mismatch: {record.collection_status} {failed_commands}"
            )

        backend = DockerSimulatorCommandBackend(
            executor=executor,
            allowed_roots=["/public/home/alice"],
            timeout_seconds=20.0,
        )
        run_service = RunService(store=RunStore(db_path), backend=backend)
        probe_run = run_service.submit(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script=compute_runtime_probe_script(),
                resource_plan=ResourcePlan(
                    partition="Students",
                    qos="qos_stu_medium_2gpu",
                    nodes=1,
                    ntasks=1,
                    cpus_per_task=1,
                    gpus_per_node=1,
                    gpus_total=1,
                    time_limit="00:05:00",
                ),
            )
        )
        final_run = probe_run
        for _ in range(30):
            try:
                final_run = run_service.reconcile_once(probe_run.run_id)
            except SlurmBackendError:
                time.sleep(1)
                continue
            if final_run.state in {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}:
                break
            time.sleep(1)
        if final_run.state != RunState.SUCCEEDED or final_run.job_id is None:
            raise RuntimeError(f"compute probe run failed: {final_run}")
        output_path = f"/public/home/alice/slurm-{final_run.job_id}.out"
        output = executor.run(["cat", output_path], user="alice", timeout_seconds=10.0)
        if output.returncode != 0:
            raise RuntimeError(f"compute probe output unavailable: {output.stderr}")
        compute_record = store_compute_runtime_probe_output(
            store=store,
            owner="alice",
            job_id=final_run.job_id,
            output=output.stdout,
            source_type=ObservationSourceType.SIMULATOR,
            source_name="docker-slurm-25.11-allocated-gpu-job",
        )
        compute_detail = api.handle_get(
            f"/api/v1/platform/snapshots/{compute_record.snapshot_id}",
            headers={"X-Pilot107-User": "alice"},
        )
        if compute_detail.status != 200:
            raise RuntimeError(f"compute snapshot API failed: {compute_detail.payload}")
        gpu_availability = compute_record.payload["runtime_limitations"][0]["availability"]
        print(
            "platform snapshot smoke "
            f"snapshot={record.snapshot_id} status={record.collection_status.value} "
            f"partitions={len(partition_names)} nodes={len(record.payload.get('nodes', []))} "
            f"failed_commands={','.join(failed_commands) or 'none'} "
            f"entitlement_qos={len(association_qos)} "
            f"compute_job={final_run.job_id} gpu_runtime={gpu_availability}"
        )
    return 0


def _contract_payload(qos: str) -> dict:
    return {
        "recipe_version_id": "recipe_python_cpu@1.0.0",
        "project": {"workdir": "/public/home/alice"},
        "entry": {"command": "echo entitlement-check", "expected_outputs": []},
        "resources": {
            "partition": "Students",
            "qos": qos,
            "nodes": 1,
            "ntasks": 1,
            "cpus_per_task": 1,
            "time_limit": "00:05:00",
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
