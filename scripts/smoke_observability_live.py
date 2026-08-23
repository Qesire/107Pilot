from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

from pilot107.adapters.slurm import (
    DockerComposeExecutor,
    DockerComposeTarget,
    DockerSimulatorCommandBackend,
)
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.core.resources import ResourcePlan
from pilot107.core.run_service import RunService, RunSubmitRequest
from pilot107.core.run_store import RunStore
from pilot107.core.states import TERMINAL_RUN_STATES
from pilot107.observability.adapters import (
    RunObservationTarget,
    SlurmCliObservationAdapter,
)
from pilot107.observability.collector import (
    ObservabilityCollector,
    ObservabilityCollectorPolicy,
)
from pilot107.observability.store import SQLiteObservabilityStore


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
    config = executor.run(
        ["scontrol", "show", "config"],
        user="alice",
        timeout_seconds=20,
    )
    if config.returncode != 0 or "jobacct_gather/linux" not in config.stdout:
        print("jobacct_gather/linux is not active", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="pilot107-observability-") as temporary:
        db_path = Path(temporary) / "observability.db"
        run_store = RunStore(db_path)
        backend = DockerSimulatorCommandBackend(
            executor=executor,
            allowed_roots=["/public/home/alice"],
            timeout_seconds=20,
        )
        run_service = RunService(store=run_store, backend=backend)
        observation_store = SQLiteObservabilityStore(db_path)
        collector = ObservabilityCollector(
            store=observation_store,
            control_repository=SQLiteControlRepository(db_path),
            adapter=SlurmCliObservationAdapter(
                executor=executor,
                slurm_user="alice",
                timeout_seconds=20,
            ),
            worker_id="observability-live-smoke",
            policy=ObservabilityCollectorPolicy(
                capability_interval_seconds=300,
                platform_interval_seconds=5,
                active_run_interval_seconds=1,
                minimum_interval_seconds=1,
                max_commands_per_minute=120,
                max_concurrent_requests=1,
                command_deadline_seconds=20,
                batch_size=20,
                failure_backoff_seconds=2,
                lease_seconds=45,
            ),
        )
        run = run_service.submit(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script=(
                    "#!/bin/bash\n"
                    "python3 -c 'import time; "
                    "payload=bytearray(32*1024*1024); "
                    "end=time.time()+12; value=0; "
                    "exec(\"while time.time() < end:\\n value += sum(range(20000))\"); "
                    "print(len(payload), value)'\n"
                ),
                resource_plan=ResourcePlan(
                    partition="Students",
                    qos="qos_stu_medium_2gpu",
                    nodes=1,
                    ntasks=1,
                    cpus_per_task=1,
                    time_limit="00:02:00",
                ),
            )
        )
        if run.job_id is None:
            print("smoke Run has no Slurm job ID", file=sys.stderr)
            return 1

        deadline = time.monotonic() + 90
        cycle_statuses: list[tuple[str, str]] = []
        diagnostics: list[dict[str, object]] = []
        while time.monotonic() < deadline:
            run = run_service.reconcile_once(run.run_id)
            collector.observe_run(
                RunObservationTarget(
                    connection_id="default",
                    owner=run.owner,
                    run_id=run.run_id,
                    job_id=run.job_id or "",
                    attempt=run.attempt,
                ),
                state=run.state.value,
            )
            tick = collector.tick("default")
            cycle_statuses.extend((cycle.lane, cycle.status) for cycle in tick.cycles)
            diagnostics.append(
                {
                    "run_state": run.state.value,
                    "tick_errors": list(tick.errors),
                    "cycles": [
                        {
                            "lane": cycle.lane,
                            "status": cycle.status,
                            "warnings": list(cycle.warnings),
                        }
                        for cycle in tick.cycles
                    ],
                    "samples": [
                        {
                            "warnings": list(sample.warnings),
                            "max_rss": (
                                None
                                if sample.measures.max_rss is None
                                else sample.measures.max_rss.__dict__
                            ),
                        }
                        for sample in tick.run_samples
                    ],
                }
            )
            try:
                summary = observation_store.get_summary(run.run_id, owner=run.owner)
            except KeyError:
                summary = None
            if run.state in TERMINAL_RUN_STATES and summary is not None:
                break
            time.sleep(1)
        else:
            print(json.dumps({"diagnostics": diagnostics}, sort_keys=True), file=sys.stderr)
            print(f"observability smoke timed out in state {run.state}", file=sys.stderr)
            return 1

        samples = observation_store.list_run_samples(run.run_id, owner=run.owner)
        available_rss = [
            sample.measures.max_rss
            for sample in samples
            if sample.measures.max_rss is not None
            and sample.measures.max_rss.availability == "available"
        ]
        if not available_rss or int(available_rss[-1].value or 0) <= 0:
            print(json.dumps({"diagnostics": diagnostics}, sort_keys=True), file=sys.stderr)
            print("live sstat never produced a positive MaxRSS", file=sys.stderr)
            return 1
        assert summary is not None
        total_cpu = summary.used.total_cpu
        terminal_rss = summary.used.max_rss
        gpu = summary.used.gpu_utilization
        if total_cpu is None or total_cpu.availability != "available":
            print("terminal sacct did not produce TotalCPU", file=sys.stderr)
            return 1
        if (
            terminal_rss is None
            or terminal_rss.availability != "available"
            or int(terminal_rss.value or 0) <= 0
        ):
            print("terminal sacct steps did not produce MaxRSS", file=sys.stderr)
            return 1
        if gpu is None or gpu.availability != "unsupported" or gpu.value is not None:
            print("GPU missingness was not preserved as unsupported", file=sys.stderr)
            return 1

        report = {
            "schema": "pilot107.observability-live-smoke/v1",
            "run_id": run.run_id,
            "job_id": run.job_id,
            "run_state": run.state.value,
            "sample_count": len(samples),
            "max_rss_bytes": available_rss[-1].value,
            "terminal_max_rss_bytes": terminal_rss.value,
            "total_cpu_seconds": total_cpu.value,
            "gpu_availability": gpu.availability,
            "cycle_statuses": cycle_statuses,
            "jobacct_gather_type": "jobacct_gather/linux",
        }
        print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
