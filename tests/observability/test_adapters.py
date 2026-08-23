from __future__ import annotations

from dataclasses import dataclass, field

from pilot107.adapters.slurm import CommandResult
from pilot107.observability.adapters import (
    FallbackObservationAdapter,
    RunObservationTarget,
    SlurmCliObservationAdapter,
    SourceCollection,
    SourceRunObservation,
)
from pilot107.observability.model import ObservedMeasure, ResourceMeasureSet


@dataclass
class RecordingExecutor:
    results: list[CommandResult]
    calls: list[tuple[list[str], str | None, float]] = field(default_factory=list)

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        user: str | None = None,
        stdin: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> CommandResult:
        del stdin
        self.calls.append((list(argv), user, timeout_seconds))
        return self.results.pop(0)


def _target(job_id: str, *, run_id: str = "run1") -> RunObservationTarget:
    return RunObservationTarget(
        connection_id="connection1",
        owner="alice",
        run_id=run_id,
        job_id=job_id,
        attempt=0,
    )


def test_sstat_batches_targets_and_marks_missing_step_as_not_collected() -> None:
    executor = RecordingExecutor(
        [
            CommandResult(
                0,
                "101.batch|1|cpu=1,mem=2G|00:00:02|1024M|4096|512\n",
                "",
            )
        ]
    )
    adapter = SlurmCliObservationAdapter(
        executor=executor,
        slurm_user="alice",
        timeout_seconds=7,
    )

    batch = adapter.collect_active_runs(
        "connection1",
        (_target("101"), _target("102", run_id="run2")),
    )

    assert executor.calls == [
        (
            [
                "sstat",
                "-nP",
                "--allsteps",
                "-j",
                "101,102",
                "-o",
                    "JobID,NTasks,AllocTRES,AveCPU,MaxRSS,TRESUsageInTot,TRESUsageOutTot",
            ],
            "alice",
            7,
        )
    ]
    assert batch.command_count == 1
    first, missing = batch.run_observations
    assert first.measures.max_rss is not None
    assert first.measures.max_rss.value == 1024 * 1024 * 1024
    assert first.measures.max_rss.unit == "bytes"
    assert first.measures.max_rss.source_operation == "sstat"
    assert first.measures.gpu_utilization is not None
    assert first.measures.gpu_utilization.availability == "unsupported"
    assert first.measures.gpu_utilization.value is None
    assert missing.measures.max_rss is not None
    assert missing.measures.max_rss.availability == "not_collected"
    assert missing.measures.max_rss.value is None


def test_capability_uses_fixed_scontrol_probe_and_publishes_typed_facts() -> None:
    executor = RecordingExecutor(
        [
            CommandResult(
                0,
                "JobAcctGatherFrequency  = 5\n"
                "JobAcctGatherType       = jobacct_gather/linux\n"
                "AccountingStorageType   = accounting_storage/slurmdbd\n"
                "SelectType              = select/cons_tres\n"
                "SLURM_VERSION           = 25.11.2\n",
                "",
            )
        ]
    )
    adapter = SlurmCliObservationAdapter(executor=executor, slurm_user="alice")

    batch = adapter.collect_capability("connection1")

    assert executor.calls[0][0] == ["scontrol", "show", "config"]
    assert batch.command_count == 1
    assert batch.partial is False
    assert batch.platform_measures is not None
    facts = batch.platform_measures.as_dict()
    assert facts["jobacct_gather_type"].value == "jobacct_gather/linux"
    assert facts["slurm_version"].value == "25.11.2"


def test_platform_account_collects_partition_and_owner_job_counts() -> None:
    executor = RecordingExecutor(
        [
            CommandResult(
                0,
                "Students*|128|1024000|gpu:A100:8|idle\n"
                "debug|128|1024000|gpu:A100:8|alloc\n",
                "",
            ),
            CommandResult(
                0,
                "101|RUNNING|node1|Students|job-a\n"
                "102|PENDING|Resources|Students|job-b\n",
                "",
            ),
        ]
    )
    adapter = SlurmCliObservationAdapter(executor=executor, slurm_user="alice")

    batch = adapter.collect_platform_account("connection1", ("alice",))

    assert [call[0] for call in executor.calls] == [
        ["sinfo", "-h", "-o", "%P|%c|%m|%G|%T"],
        ["squeue", "-h", "-u", "alice", "-o", "%i|%T|%R|%P|%j"],
    ]
    assert batch.command_count == 2
    assert batch.partial is False
    assert batch.platform_measures is not None
    platform = batch.platform_measures.as_dict()
    assert platform["partitions_total"].value == 2
    assert platform["partitions_idle"].value == 1
    account = batch.account_observations[0].measures.as_dict()
    assert account["jobs_running"].value == 1
    assert account["jobs_pending"].value == 1


def test_account_pulse_never_relabels_one_cluster_user_as_another_owner() -> None:
    executor = RecordingExecutor(
        [
            CommandResult(0, "Students*|128|1024000|gpu:A100:8|idle\n", ""),
            CommandResult(0, "101|RUNNING|node1|Students|job-a\n", ""),
        ]
    )
    adapter = SlurmCliObservationAdapter(
        executor=executor,
        slurm_user="cluster-alice",
        observation_owner="alice",
    )

    batch = adapter.collect_platform_account("connection1", ("alice", "bob"))

    assert [item.owner for item in batch.account_observations] == ["alice"]
    assert batch.partial is True
    assert "ACCOUNT_TARGET_UNMAPPED" in batch.warnings


def test_failed_sstat_returns_unavailable_measures_instead_of_zero() -> None:
    executor = RecordingExecutor([CommandResult(1, "", "accounting disabled")])
    adapter = SlurmCliObservationAdapter(executor=executor, slurm_user="alice")

    batch = adapter.collect_active_runs("connection1", (_target("101"),))

    measure = batch.run_observations[0].measures.max_rss
    assert measure is not None
    assert measure.availability == "not_collected"
    assert measure.value is None
    assert batch.partial is True
    assert batch.warnings == ("SSTAT_FAILED",)


def test_sstat_permission_denied_is_distinct_from_not_collected() -> None:
    executor = RecordingExecutor([CommandResult(1, "", "Permission denied")])
    adapter = SlurmCliObservationAdapter(executor=executor, slurm_user="alice")

    batch = adapter.collect_active_runs("connection1", (_target("101"),))

    measure = batch.run_observations[0].measures.max_rss
    assert measure is not None
    assert measure.availability == "permission_denied"
    assert measure.value is None
    assert batch.error_code == "active_run:PERMISSION_DENIED"


def test_sacct_preserves_terminal_used_and_allocated_scopes() -> None:
    executor = RecordingExecutor(
        [
            CommandResult(
                0,
                "101|COMPLETED|0:0|120|300|cpu=2,mem=4G|2|00:03:00.018|\n"
                "101.batch|COMPLETED|0:0|120|300|cpu=2,mem=4G|2|"
                "00:03:00.018|2048M\n",
                "",
            )
        ]
    )
    adapter = SlurmCliObservationAdapter(executor=executor, slurm_user="alice")

    batch = adapter.collect_terminal_runs("connection1", (_target("101"),))

    assert executor.calls[0][0] == [
        "sacct",
        "-nP",
        "-j",
        "101",
        "-o",
        "JobIDRaw,State,ExitCode,ElapsedRaw,TimelimitRaw,AllocTRES,AllocCPUS,TotalCPU,MaxRSS",
    ]
    observation = batch.run_observations[0]
    assert observation.measures.total_cpu is not None
    assert observation.measures.total_cpu.value == 180.018
    assert observation.measures.max_rss is not None
    assert observation.measures.max_rss.value == 2048 * 1024**2
    assert observation.measures.elapsed is not None
    assert observation.measures.elapsed.value == 120
    assert observation.allocated is not None
    assert observation.allocated.allocated_cpus is not None
    assert observation.allocated.allocated_cpus.value == 2
    assert observation.allocated.allocated_memory is not None
    assert observation.allocated.allocated_memory.value == 4 * 1024**3


def test_sacct_keeps_allocation_row_when_last_maxrss_field_is_empty() -> None:
    executor = RecordingExecutor(
        [
            CommandResult(
                0,
                "101|COMPLETED|0:0|120|300|cpu=2,mem=4G|2|00:03:00|\n",
                "",
            )
        ]
    )
    adapter = SlurmCliObservationAdapter(executor=executor, slurm_user="alice")

    batch = adapter.collect_terminal_runs("connection1", (_target("101"),))

    observation = batch.run_observations[0]
    assert observation.measures.total_cpu is not None
    assert observation.measures.total_cpu.value == 180
    assert observation.measures.elapsed is not None
    assert observation.measures.elapsed.value == 120
    assert observation.measures.max_rss is not None
    assert observation.measures.max_rss.availability == "not_collected"


class FixedActiveAdapter:
    def __init__(self, measure: ObservedMeasure, *, warning: str) -> None:
        self.measure = measure
        self.warning = warning
        self.calls = 0

    def collect_active_runs(
        self, connection_id: str, targets: tuple[RunObservationTarget, ...]
    ) -> SourceCollection:
        del connection_id
        self.calls += 1
        return SourceCollection(
            run_observations=(
                SourceRunObservation(
                    target=targets[0],
                    measures=ResourceMeasureSet(max_rss=self.measure),
                ),
            ),
            command_count=1,
            partial=self.measure.value is None,
            warnings=(self.warning,),
        )


def _source_measure(
    value: int | None, *, adapter: str, availability: str
) -> ObservedMeasure:
    return ObservedMeasure(
        value=value,
        unit="bytes",
        availability=availability,
        source_adapter=adapter,
        source_operation="active_resources",
        captured_at="2026-08-19T00:00:00Z",
        quality="verified" if value is not None else "unavailable",
        coverage=1.0 if value is not None else None,
        warning=None if value is not None else "field absent",
    )


def test_fallback_adapter_fills_missing_field_and_preserves_provenance() -> None:
    primary = FixedActiveAdapter(
        _source_measure(None, adapter="slurm_rest", availability="not_collected"),
        warning="REST_FIELD_MISSING",
    )
    fallback = FixedActiveAdapter(
        _source_measure(4096, adapter="slurm_cli", availability="available"),
        warning="CLI_FALLBACK",
    )
    adapter = FallbackObservationAdapter((primary, fallback))

    batch = adapter.collect_active_runs("connection1", (_target("101"),))

    measure = batch.run_observations[0].measures.max_rss
    assert measure is not None
    assert measure.value == 4096
    assert measure.source_adapter == "slurm_cli"
    assert batch.command_count == 2
    assert batch.warnings == ("REST_FIELD_MISSING", "CLI_FALLBACK")
    assert primary.calls == fallback.calls == 1
