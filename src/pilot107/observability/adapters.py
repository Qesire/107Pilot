"""Bounded, read-only source adapters for resource observations."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pilot107.adapters.slurm import CommandResult, SimulatorExecutor
from pilot107.observability.model import Availability, ObservedMeasure, ResourceMeasureSet
from pilot107.observability.slurm_parser import (
    parse_sacct,
    parse_sstat,
    summarize_sacct_job,
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:+-]{1,128}$")


@dataclass(frozen=True)
class RunObservationTarget:
    connection_id: str
    owner: str
    run_id: str
    job_id: str
    attempt: int

    def __post_init__(self) -> None:
        for label, value in (
            ("connection_id", self.connection_id),
            ("owner", self.owner),
            ("run_id", self.run_id),
            ("job_id", self.job_id),
        ):
            if _SAFE_ID.fullmatch(value) is None:
                raise ValueError(f"{label} is invalid")
        if isinstance(self.attempt, bool) or self.attempt < 0:
            raise ValueError("attempt is invalid")


@dataclass(frozen=True)
class SourceAccountObservation:
    owner: str
    measures: ResourceMeasureSet
    partial: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceRunObservation:
    target: RunObservationTarget
    measures: ResourceMeasureSet
    allocated: ResourceMeasureSet | None = None
    partial: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceCollection:
    platform_measures: ResourceMeasureSet | None = None
    account_observations: tuple[SourceAccountObservation, ...] = ()
    run_observations: tuple[SourceRunObservation, ...] = ()
    command_count: int = 0
    partial: bool = False
    failed: bool = False
    error_code: str | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.command_count, bool) or self.command_count < 0:
            raise ValueError("command_count is invalid")
        if self.failed != (self.error_code is not None):
            raise ValueError("failed collections require exactly one error_code")


class ObservationSourceAdapter(Protocol):
    def estimated_command_count(self, lane: str) -> int: ...

    def collect_capability(self, connection_id: str) -> SourceCollection: ...

    def collect_platform_account(
        self, connection_id: str, owners: tuple[str, ...]
    ) -> SourceCollection: ...

    def collect_active_runs(
        self, connection_id: str, targets: tuple[RunObservationTarget, ...]
    ) -> SourceCollection: ...

    def collect_terminal_runs(
        self, connection_id: str, targets: tuple[RunObservationTarget, ...]
    ) -> SourceCollection: ...


class FallbackObservationAdapter:
    """Merge field-level facts from ordered source adapters."""

    def __init__(self, adapters: tuple[ObservationSourceAdapter, ...]) -> None:
        if not adapters:
            raise ValueError("at least one observation source adapter is required")
        self.adapters = adapters

    def estimated_command_count(self, lane: str) -> int:
        return sum(
            getattr(adapter, "estimated_command_count", lambda _lane: 1)(lane)
            for adapter in self.adapters
        )

    def collect_capability(self, connection_id: str) -> SourceCollection:
        return self._collect("collect_capability", connection_id)

    def collect_platform_account(
        self, connection_id: str, owners: tuple[str, ...]
    ) -> SourceCollection:
        return self._collect("collect_platform_account", connection_id, owners)

    def collect_active_runs(
        self, connection_id: str, targets: tuple[RunObservationTarget, ...]
    ) -> SourceCollection:
        return self._collect("collect_active_runs", connection_id, targets)

    def collect_terminal_runs(
        self, connection_id: str, targets: tuple[RunObservationTarget, ...]
    ) -> SourceCollection:
        return self._collect("collect_terminal_runs", connection_id, targets)

    def _collect(self, method: str, *arguments: object) -> SourceCollection:
        merged: SourceCollection | None = None
        for adapter in self.adapters:
            callback = getattr(adapter, method)
            current = callback(*arguments)
            merged = current if merged is None else _merge_collections(merged, current)
            if not merged.partial and not merged.failed:
                break
        assert merged is not None
        return merged


class SlurmCliObservationAdapter:
    """CLI fallback using fixed argv and pipe-delimited Slurm output."""

    _SSTAT_FIELDS = (
        "JobID,NTasks,AllocTRES,AveCPU,MaxRSS,TRESUsageInTot,TRESUsageOutTot"
    )
    _SACCT_FIELDS = (
        "JobIDRaw,State,ExitCode,ElapsedRaw,TimelimitRaw,AllocTRES,"
        "AllocCPUS,NTasks,TotalCPU,CPUTimeRAW,MaxRSS"
    )
    _SINFO_FORMAT = "%P|%c|%m|%G|%T"
    _CONFIG_FACTS = (
        ("SLURM_VERSION", "slurm_version", "version"),
        ("JobAcctGatherType", "jobacct_gather_type", "plugin"),
        ("JobAcctGatherFrequency", "jobacct_gather_frequency", "seconds"),
        ("AccountingStorageType", "accounting_storage_type", "plugin"),
        ("SelectType", "select_type", "plugin"),
    )

    def __init__(
        self,
        *,
        executor: SimulatorExecutor,
        slurm_user: str,
        observation_owner: str | None = None,
        timeout_seconds: float = 10.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if _SAFE_ID.fullmatch(slurm_user) is None:
            raise ValueError("slurm_user is invalid")
        if observation_owner is not None and _SAFE_ID.fullmatch(observation_owner) is None:
            raise ValueError("observation_owner is invalid")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.executor = executor
        self.slurm_user = slurm_user
        self.observation_owner = observation_owner or slurm_user
        self.timeout_seconds = timeout_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    def estimated_command_count(self, lane: str) -> int:
        return 2 if lane == "platform_account" else 1

    def collect_capability(self, connection_id: str) -> SourceCollection:
        _validate_connection(connection_id)
        result = self.executor.run(
            ["scontrol", "show", "config"],
            user=self.slurm_user,
            timeout_seconds=self.timeout_seconds,
        )
        captured_at = _timestamp(self._now())
        if result.returncode != 0:
            permission_denied = _permission_denied(result)
            return SourceCollection(
                command_count=1,
                partial=True,
                failed=True,
                error_code=(
                    "capability:PERMISSION_DENIED"
                    if permission_denied
                    else "capability:SCONTROL_FAILED"
                ),
                warnings=(
                    "SCONTROL_PERMISSION_DENIED"
                    if permission_denied
                    else "SCONTROL_FAILED",
                ),
            )
        config = _parse_scontrol_config(result.stdout)
        extras = tuple(
            (
                measure_name,
                _measure_or_missing(
                    config.get(config_name),
                    unit=unit,
                    operation="scontrol_config",
                    captured_at=captured_at,
                ),
            )
            for config_name, measure_name, unit in self._CONFIG_FACTS
        )
        partial = any(measure.value is None for _, measure in extras)
        return SourceCollection(
            platform_measures=ResourceMeasureSet(extras=extras),
            command_count=1,
            partial=partial,
            warnings=("SCONTROL_CONFIG_PARTIAL",) if partial else (),
        )

    def collect_platform_account(
        self, connection_id: str, owners: tuple[str, ...]
    ) -> SourceCollection:
        _validate_connection(connection_id)
        captured_at = _timestamp(self._now())
        unique_owners = tuple(sorted(set(owners)))
        account_owners = tuple(
            owner for owner in unique_owners if owner == self.observation_owner
        )
        sinfo_result = self.executor.run(
            ["sinfo", "-h", "-o", self._SINFO_FORMAT],
            user=self.slurm_user,
            timeout_seconds=self.timeout_seconds,
        )
        squeue_result = None
        if account_owners:
            squeue_result = self.executor.run(
                [
                    "squeue",
                    "-h",
                    "-u",
                    self.slurm_user,
                    "-o",
                    "%i|%T|%R|%P|%j",
                ],
                user=self.slurm_user,
                timeout_seconds=self.timeout_seconds,
            )
        command_count = 1 + int(squeue_result is not None)
        warnings: list[str] = []
        if len(account_owners) != len(unique_owners):
            warnings.append("ACCOUNT_TARGET_UNMAPPED")
        platform_measures = None
        account_observations: tuple[SourceAccountObservation, ...] = ()
        successful_commands = 0

        if sinfo_result.returncode == 0:
            successful_commands += 1
            partition_counts, malformed = _partition_counts(sinfo_result.stdout)
            platform_measures = ResourceMeasureSet(
                extras=tuple(
                    (
                        name,
                        _available(
                            value,
                            unit="partitions",
                            operation="sinfo",
                            captured_at=captured_at,
                        ),
                    )
                    for name, value in sorted(partition_counts.items())
                )
            )
            if malformed:
                warnings.append("SINFO_PARTIAL")
        else:
            warnings.append(
                "SINFO_PERMISSION_DENIED"
                if _permission_denied(sinfo_result)
                else "SINFO_FAILED"
            )

        if squeue_result is None:
            if not unique_owners:
                warnings.append("NO_ACCOUNT_TARGETS")
        elif squeue_result.returncode == 0:
            successful_commands += 1
            counts, malformed = _job_counts(squeue_result.stdout)
            measures = ResourceMeasureSet(
                extras=tuple(
                    (
                        f"jobs_{state}",
                        _available(
                            count,
                            unit="jobs",
                            operation="squeue",
                            captured_at=captured_at,
                        ),
                    )
                    for state, count in sorted(counts.items())
                )
            )
            account_observations = tuple(
                SourceAccountObservation(owner=owner, measures=measures)
                for owner in account_owners
            )
            if malformed:
                warnings.append("SQUEUE_PARTIAL")
        else:
            warnings.append(
                "SQUEUE_PERMISSION_DENIED"
                if _permission_denied(squeue_result)
                else "SQUEUE_FAILED"
            )

        if successful_commands == 0:
            permission_denied = _permission_denied(sinfo_result) or (
                squeue_result is not None and _permission_denied(squeue_result)
            )
            return SourceCollection(
                command_count=command_count,
                partial=True,
                failed=True,
                error_code=(
                    "platform_account:PERMISSION_DENIED"
                    if permission_denied
                    else "platform_account:CLI_FAILED"
                ),
                warnings=tuple(warnings),
            )
        return SourceCollection(
            platform_measures=platform_measures,
            account_observations=account_observations,
            command_count=command_count,
            partial=bool(warnings),
            warnings=tuple(warnings),
        )

    def collect_active_runs(
        self, connection_id: str, targets: tuple[RunObservationTarget, ...]
    ) -> SourceCollection:
        _validate_targets(connection_id, targets)
        if not targets:
            return SourceCollection()
        job_ids = ",".join(target.job_id for target in targets)
        result = self.executor.run(
            [
                "sstat",
                "-nP",
                "--allsteps",
                "-j",
                job_ids,
                "-o",
                self._SSTAT_FIELDS,
            ],
            user=self.slurm_user,
            timeout_seconds=self.timeout_seconds,
        )
        captured_at = _timestamp(self._now())
        if result.returncode != 0:
            permission_denied = _permission_denied(result)
            return SourceCollection(
                run_observations=tuple(
                    _missing_run(
                        target,
                        operation="sstat",
                        captured_at=captured_at,
                        availability=(
                            "permission_denied"
                            if permission_denied
                            else "not_collected"
                        ),
                    )
                    for target in targets
                ),
                command_count=1,
                partial=True,
                failed=True,
                error_code=(
                    "active_run:PERMISSION_DENIED"
                    if permission_denied
                    else "active_run:SSTAT_FAILED"
                ),
                warnings=(
                    "SSTAT_PERMISSION_DENIED" if permission_denied else "SSTAT_FAILED",
                ),
            )
        parsed = parse_sstat(result.stdout)
        observations: list[SourceRunObservation] = []
        for target in targets:
            matching = [
                record for record in parsed.records if record.job_id == target.job_id
            ]
            rss_values = [
                record.max_rss_bytes
                for record in matching
                if record.max_rss_bytes is not None
            ]
            if not matching or not rss_values:
                observations.append(
                    _missing_run(target, operation="sstat", captured_at=captured_at)
                )
                continue
            observations.append(
                SourceRunObservation(
                    target=target,
                    measures=ResourceMeasureSet(
                        gpu_utilization=_unsupported_gpu(captured_at=captured_at),
                        max_rss=_available(
                            max(rss_values),
                            unit="bytes",
                            operation="sstat",
                            captured_at=captured_at,
                        )
                    ),
                )
            )
        partial = bool(parsed.warnings) or any(
            item.measures.max_rss is not None and item.measures.max_rss.value is None
            for item in observations
        )
        return SourceCollection(
            run_observations=tuple(observations),
            command_count=1,
            partial=partial,
            warnings=tuple((*parsed.warnings, *(("SSTAT_PARTIAL",) if partial else ()))),
        )

    def collect_terminal_runs(
        self, connection_id: str, targets: tuple[RunObservationTarget, ...]
    ) -> SourceCollection:
        _validate_targets(connection_id, targets)
        if not targets:
            return SourceCollection()
        job_ids = ",".join(target.job_id for target in targets)
        result = self.executor.run(
            ["sacct", "-nP", "-j", job_ids, "-o", self._SACCT_FIELDS],
            user=self.slurm_user,
            timeout_seconds=self.timeout_seconds,
        )
        captured_at = _timestamp(self._now())
        if result.returncode != 0:
            permission_denied = _permission_denied(result)
            return SourceCollection(
                run_observations=tuple(
                    _missing_run(
                        target,
                        operation="sacct",
                        captured_at=captured_at,
                        terminal=True,
                        availability=(
                            "permission_denied"
                            if permission_denied
                            else "not_collected"
                        ),
                    )
                    for target in targets
                ),
                command_count=1,
                partial=True,
                failed=True,
                error_code=(
                    "terminal_accounting:PERMISSION_DENIED"
                    if permission_denied
                    else "terminal_accounting:SACCT_FAILED"
                ),
                warnings=(
                    "SACCT_PERMISSION_DENIED" if permission_denied else "SACCT_FAILED",
                ),
            )
        parsed = parse_sacct(result.stdout)
        observations: list[SourceRunObservation] = []
        for target in targets:
            usage = summarize_sacct_job(parsed.records, target.job_id)
            if usage is None:
                observations.append(
                    _missing_run(
                        target,
                        operation="sacct",
                        captured_at=captured_at,
                        terminal=True,
                    )
                )
                continue
            observations.append(
                SourceRunObservation(
                    target=target,
                    measures=ResourceMeasureSet(
                        gpu_utilization=_unsupported_gpu(captured_at=captured_at),
                        total_cpu=_measure_or_missing(
                            usage.total_cpu_seconds,
                            unit="seconds",
                            operation="sacct",
                            captured_at=captured_at,
                        ),
                        cpu_time_raw=_measure_or_missing(
                            usage.cpu_time_raw_seconds,
                            unit="cpu_seconds",
                            operation="sacct",
                            captured_at=captured_at,
                        ),
                        elapsed=_measure_or_missing(
                            usage.elapsed_seconds,
                            unit="seconds",
                            operation="sacct",
                            captured_at=captured_at,
                        ),
                        max_rss=_measure_or_missing(
                            usage.max_rss_bytes,
                            unit="bytes",
                            operation="sacct",
                            captured_at=captured_at,
                        ),
                    ),
                    allocated=ResourceMeasureSet(
                        allocated_cpus=_measure_or_missing(
                            usage.allocated_cpus,
                            unit="cpu",
                            operation="sacct",
                            captured_at=captured_at,
                        ),
                        allocated_memory=_measure_or_missing(
                            usage.allocated_memory_bytes,
                            unit="bytes",
                            operation="sacct",
                            captured_at=captured_at,
                        ),
                        extras=(
                            (
                                "task_count",
                                _measure_or_missing(
                                    usage.task_count,
                                    unit="tasks",
                                    operation="sacct",
                                    captured_at=captured_at,
                                ),
                            ),
                            (
                                "requested_walltime",
                                _measure_or_missing(
                                    usage.requested_walltime_seconds,
                                    unit="seconds",
                                    operation="sacct",
                                    captured_at=captured_at,
                                ),
                            ),
                        ),
                    ),
                )
            )
        partial = bool(parsed.warnings) or any(
            item.measures.total_cpu is not None and item.measures.total_cpu.value is None
            for item in observations
        )
        return SourceCollection(
            run_observations=tuple(observations),
            command_count=1,
            partial=partial,
            warnings=tuple((*parsed.warnings, *(("SACCT_PARTIAL",) if partial else ()))),
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("adapter clock must be timezone-aware")
        return value.astimezone(UTC)


def _validate_connection(connection_id: str) -> None:
    if _SAFE_ID.fullmatch(connection_id) is None:
        raise ValueError("connection_id is invalid")


def _validate_targets(
    connection_id: str, targets: tuple[RunObservationTarget, ...]
) -> None:
    _validate_connection(connection_id)
    if any(target.connection_id != connection_id for target in targets):
        raise ValueError("target connection mismatch")


def _missing_run(
    target: RunObservationTarget,
    *,
    operation: str,
    captured_at: str,
    terminal: bool = False,
    availability: Availability = "not_collected",
) -> SourceRunObservation:
    missing = _unavailable(
        operation=operation,
        captured_at=captured_at,
        availability=availability,
    )
    return SourceRunObservation(
        target=target,
        measures=ResourceMeasureSet(
            gpu_utilization=_unsupported_gpu(captured_at=captured_at),
            max_rss=missing,
            total_cpu=missing if terminal else None,
            elapsed=missing if terminal else None,
        ),
        allocated=(
            ResourceMeasureSet(allocated_cpus=missing, allocated_memory=missing)
            if terminal
            else None
        ),
        partial=True,
        warnings=(f"{operation.upper()}_NOT_COLLECTED",),
    )


def _available(
    value: float | int | str,
    *,
    unit: str,
    operation: str,
    captured_at: str,
) -> ObservedMeasure:
    return ObservedMeasure(
        value=value,
        unit=unit,
        availability="available",
        source_adapter="slurm_cli",
        source_operation=operation,
        captured_at=captured_at,
        quality="verified",
        coverage=1.0,
        warning=None,
    )


def _unavailable(
    *,
    operation: str,
    captured_at: str,
    availability: Availability = "not_collected",
) -> ObservedMeasure:
    return ObservedMeasure(
        value=None,
        unit="unknown",
        availability=availability,
        source_adapter="slurm_cli",
        source_operation=operation,
        captured_at=captured_at,
        quality="unavailable",
        coverage=None,
        warning=f"{operation} did not provide this field",
    )


def _measure_or_missing(
    value: float | int | str | None,
    *,
    unit: str,
    operation: str,
    captured_at: str,
) -> ObservedMeasure:
    if value is None:
        return _unavailable(operation=operation, captured_at=captured_at)
    return _available(
        value,
        unit=unit,
        operation=operation,
        captured_at=captured_at,
    )


def _unsupported_gpu(*, captured_at: str) -> ObservedMeasure:
    return ObservedMeasure(
        value=None,
        unit="ratio",
        availability="unsupported",
        source_adapter="slurm_cli",
        source_operation="gpu_utilization",
        captured_at=captured_at,
        quality="unavailable",
        coverage=None,
        warning="Slurm CLI source does not expose verified GPU utilization",
    )


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _permission_denied(result: CommandResult) -> bool:
    message = f"{result.stdout}\n{result.stderr}".lower()
    return any(
        marker in message
        for marker in ("permission denied", "access denied", "not authorized", "forbidden")
    )


def _parse_scontrol_config(stdout: str) -> dict[str, str]:
    config: dict[str, str] = {}
    for line in stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and (name := key.strip()) and (raw := value.strip()):
            config[name] = raw
    return config


def _partition_counts(stdout: str) -> tuple[dict[str, int], bool]:
    counts = {"partitions_total": 0}
    malformed = False
    for line in stdout.splitlines():
        columns = [column.strip() for column in line.split("|")]
        if len(columns) != 5 or not columns[0]:
            malformed = True
            continue
        counts["partitions_total"] += 1
        state = _safe_metric_suffix(columns[4])
        if state is not None:
            name = f"partitions_{state}"
            counts[name] = counts.get(name, 0) + 1
    return counts, malformed


def _job_counts(stdout: str) -> tuple[dict[str, int], bool]:
    counts: dict[str, int] = {}
    malformed = False
    for line in stdout.splitlines():
        columns = [column.strip() for column in line.split("|")]
        if len(columns) != 5:
            malformed = True
            continue
        state = _safe_metric_suffix(columns[1])
        if state is None:
            malformed = True
            continue
        counts[state] = counts.get(state, 0) + 1
    return counts, malformed


def _safe_metric_suffix(value: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return normalized if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", normalized) else None


def _merge_collections(left: SourceCollection, right: SourceCollection) -> SourceCollection:
    accounts = {item.owner: item for item in left.account_observations}
    for account_item in right.account_observations:
        account_existing = accounts.get(account_item.owner)
        accounts[account_item.owner] = (
            account_item
            if account_existing is None
            else SourceAccountObservation(
                owner=account_item.owner,
                measures=_merge_measure_sets(
                    account_existing.measures, account_item.measures
                ),
                partial=account_existing.partial and account_item.partial,
                warnings=tuple(
                    (*account_existing.warnings, *account_item.warnings)
                ),
            )
        )
    runs = {item.target: item for item in left.run_observations}
    for run_item in right.run_observations:
        run_existing = runs.get(run_item.target)
        runs[run_item.target] = (
            run_item
            if run_existing is None
            else SourceRunObservation(
                target=run_item.target,
                measures=_merge_measure_sets(
                    run_existing.measures, run_item.measures
                ),
                allocated=(
                    run_item.allocated
                    if run_existing.allocated is None
                    else run_existing.allocated
                    if run_item.allocated is None
                    else _merge_measure_sets(
                        run_existing.allocated, run_item.allocated
                    )
                ),
                partial=run_existing.partial and run_item.partial,
                warnings=tuple((*run_existing.warnings, *run_item.warnings)),
            )
        )
    merged = SourceCollection(
        platform_measures=(
            right.platform_measures
            if left.platform_measures is None
            else left.platform_measures
            if right.platform_measures is None
            else _merge_measure_sets(left.platform_measures, right.platform_measures)
        ),
        account_observations=tuple(accounts.values()),
        run_observations=tuple(runs.values()),
        command_count=left.command_count + right.command_count,
        partial=left.partial and right.partial,
        failed=left.failed and right.failed,
        error_code=(right.error_code if left.failed and right.failed else None),
        warnings=tuple((*left.warnings, *right.warnings)),
    )
    return merged


def _merge_measure_sets(left: ResourceMeasureSet, right: ResourceMeasureSet) -> ResourceMeasureSet:
    values = left.as_dict()
    for name, measure in right.as_dict().items():
        existing = values.get(name)
        if existing is None or (
            existing.availability != "available" and measure.availability == "available"
        ):
            values[name] = measure
    known = {
        name
        for name in ResourceMeasureSet.__dataclass_fields__
        if name != "extras"
    }
    return ResourceMeasureSet(
        **{name: value for name, value in values.items() if name in known},
        extras=tuple((name, value) for name, value in values.items() if name not in known),
    )
