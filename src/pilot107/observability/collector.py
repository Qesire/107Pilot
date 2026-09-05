"""Leased and budgeted collection cycles for resource observations."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial

from pilot107.core.control_repository import (
    ControlRepository,
    ControlRepositoryConflict,
    LeaseClaim,
)
from pilot107.observability.adapters import (
    ObservationSourceAdapter,
    RunObservationTarget,
    SourceCollection,
)
from pilot107.observability.model import (
    AccountPulse,
    ObservationCycle,
    PlatformPulse,
    RunResourceSample,
    RunResourceSummary,
)
from pilot107.observability.store import ObservabilityStore


@dataclass(frozen=True)
class ObservabilityCollectorPolicy:
    capability_interval_seconds: int = 300
    platform_interval_seconds: int = 20
    active_run_interval_seconds: int = 30
    minimum_interval_seconds: int = 1
    max_commands_per_minute: int = 60
    max_concurrent_requests: int = 1
    command_deadline_seconds: int = 10
    batch_size: int = 50
    failure_backoff_seconds: int = 30
    lease_seconds: int = 45

    def __post_init__(self) -> None:
        values = self.__dict__.values()
        if any(isinstance(value, bool) or value <= 0 for value in values):
            raise ValueError("collector policy values must be positive")
        if self.max_concurrent_requests > 32:
            raise ValueError("max_concurrent_requests cannot exceed 32")
        if self.batch_size > 1000:
            raise ValueError("batch_size cannot exceed 1000")
        if self.lease_seconds > 300:
            raise ValueError("lease_seconds cannot exceed 300")


@dataclass(frozen=True)
class ObservabilityTickResult:
    lease_acquired: bool
    cycles: tuple[ObservationCycle, ...] = ()
    run_samples: tuple[RunResourceSample, ...] = ()
    summaries: tuple[RunResourceSummary, ...] = ()
    command_count: int = 0
    skipped_budget: bool = False
    errors: tuple[str, ...] = ()


class ObservabilityCollector:
    def __init__(
        self,
        *,
        store: ObservabilityStore,
        control_repository: ControlRepository,
        adapter: ObservationSourceAdapter,
        worker_id: str,
        clock: Callable[[], datetime] | None = None,
        policy: ObservabilityCollectorPolicy | None = None,
    ) -> None:
        if not worker_id:
            raise ValueError("worker_id is required")
        self.store = store
        self.control_repository = control_repository
        self.adapter = adapter
        self.worker_id = worker_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self.policy = policy or ObservabilityCollectorPolicy()

    def observe_run(self, target: RunObservationTarget, *, state: str) -> None:
        self.store.upsert_run_target(target, state=state, observed_at=_timestamp(self._now()))

    def tick(self, connection_id: str) -> ObservabilityTickResult:
        claim = self.control_repository.acquire_lease(
            resource_kind="observability_connection",
            resource_id=connection_id,
            owner=self.worker_id,
            lease_seconds=self.policy.lease_seconds,
        )
        if claim is None:
            return ObservabilityTickResult(lease_acquired=False)
        cycles: list[ObservationCycle] = []
        samples: list[RunResourceSample] = []
        summaries: list[RunResourceSummary] = []
        errors: list[str] = []
        command_count = 0
        skipped_budget = False
        try:
            used = self.store.count_cycle_commands_since(
                connection_id,
                since=_timestamp(self._now() - timedelta(seconds=60)),
            )
            remaining = max(0, self.policy.max_commands_per_minute - used)
            targets = self.store.list_run_targets(connection_id)
            terminal = tuple(target for target, state in targets if _terminal(state))
            active = tuple(
                target
                for target, state in targets
                if state in {"RUNNING", "COMPLETING"}
            )
            owners = tuple(sorted({target.owner for target, _state in targets}))

            for lane, lane_targets in (("terminal_accounting", terminal),):
                if lane_targets and not self._due(
                    connection_id,
                    lane,
                    self.policy.minimum_interval_seconds,
                ):
                    continue
                for batch_targets in _batches(lane_targets, self.policy.batch_size):
                    if remaining < 1:
                        skipped_budget = True
                        break
                    source, claim = self._collect(
                        claim,
                        lane=lane,
                        callback=partial(
                            self.adapter.collect_terminal_runs,
                            connection_id,
                            batch_targets,
                        ),
                    )
                    cycle = self._save_cycle(connection_id, lane, claim, source)
                    cycles.append(cycle)
                    if source.error_code is not None:
                        errors.append(source.error_code)
                    command_count += source.command_count
                    remaining = max(0, remaining - source.command_count)
                    if source.failed:
                        continue
                    for item in source.run_observations:
                        stable = self.store.record_terminal_observation(
                            item.target.run_id,
                            owner=item.target.owner,
                            digest=_terminal_digest(item),
                            observed_at=cycle.completed_at,
                        )
                        if not _terminal_ready(item) or stable < 2:
                            continue
                        summary = RunResourceSummary(
                            observation_id=_id("summary", item.target.run_id, item.target.attempt),
                            connection_id=connection_id,
                            owner=item.target.owner,
                            run_id=item.target.run_id,
                            attempt=item.target.attempt,
                            cycle_id=cycle.cycle_id,
                            captured_at=cycle.completed_at,
                            freshness="terminal",
                            partial=source.partial or item.partial,
                            warnings=tuple((*source.warnings, *item.warnings)),
                            used=item.measures,
                            allocated=item.allocated or item.measures.__class__(),
                            fencing_token=claim.fencing_token,
                        )
                        self.store.save_summary(summary)
                        self.store.mark_run_target_finalized(
                            item.target.run_id,
                            owner=item.target.owner,
                            observed_at=cycle.completed_at,
                        )
                        summaries.append(summary)

            for lane, interval, callback in (
                (
                    "capability",
                    self.policy.capability_interval_seconds,
                    lambda: self.adapter.collect_capability(connection_id),
                ),
                (
                    "platform_account",
                    self.policy.platform_interval_seconds,
                    lambda: self.adapter.collect_platform_account(connection_id, owners),
                ),
            ):
                if not self._due(connection_id, lane, interval):
                    continue
                required = _estimated_command_count(self.adapter, lane)
                if remaining < required:
                    skipped_budget = True
                    continue
                source, claim = self._collect(claim, lane=lane, callback=callback)
                cycle = self._save_cycle(connection_id, lane, claim, source)
                cycles.append(cycle)
                if source.error_code is not None:
                    errors.append(source.error_code)
                command_count += source.command_count
                remaining = max(0, remaining - source.command_count)
                if source.platform_measures is not None:
                    self.store.save_platform_pulse(
                        PlatformPulse(
                            observation_id=_id("platform", cycle.cycle_id),
                            connection_id=connection_id,
                            owner=None,
                            run_id=None,
                            attempt=None,
                            cycle_id=cycle.cycle_id,
                            captured_at=cycle.completed_at,
                            freshness="fresh",
                            partial=source.partial,
                            warnings=source.warnings,
                            fencing_token=claim.fencing_token,
                            measures=source.platform_measures,
                        )
                    )
                for account in source.account_observations:
                    self.store.save_account_pulse(
                        AccountPulse(
                            observation_id=_id("account", cycle.cycle_id, account.owner),
                            connection_id=connection_id,
                            owner=account.owner,
                            run_id=None,
                            attempt=None,
                            cycle_id=cycle.cycle_id,
                            captured_at=cycle.completed_at,
                            freshness="fresh",
                            partial=source.partial or account.partial,
                            warnings=tuple((*source.warnings, *account.warnings)),
                            fencing_token=claim.fencing_token,
                            measures=account.measures,
                        )
                    )

            if active and self._due(
                connection_id, "active_run", self.policy.active_run_interval_seconds
            ):
                for batch_targets in _batches(active, self.policy.batch_size):
                    if remaining < 1:
                        skipped_budget = True
                        break
                    source, claim = self._collect(
                        claim,
                        lane="active_run",
                        callback=partial(
                            self.adapter.collect_active_runs,
                            connection_id,
                            batch_targets,
                        ),
                    )
                    cycle = self._save_cycle(connection_id, "active_run", claim, source)
                    cycles.append(cycle)
                    if source.error_code is not None:
                        errors.append(source.error_code)
                    command_count += source.command_count
                    remaining = max(0, remaining - source.command_count)
                    for item in source.run_observations:
                        sample = RunResourceSample(
                            observation_id=_id(
                                "sample", cycle.cycle_id, item.target.run_id, item.target.attempt
                            ),
                            connection_id=connection_id,
                            owner=item.target.owner,
                            run_id=item.target.run_id,
                            attempt=item.target.attempt,
                            cycle_id=cycle.cycle_id,
                            captured_at=cycle.completed_at,
                            freshness="fresh",
                            partial=source.partial or item.partial,
                            warnings=tuple((*source.warnings, *item.warnings)),
                            fencing_token=claim.fencing_token,
                            measures=item.measures,
                        )
                        self.store.save_run_sample(sample)
                        self.store.mark_run_target_observed(
                            item.target.run_id,
                            owner=item.target.owner,
                            observed_at=cycle.completed_at,
                        )
                        samples.append(sample)
        except ControlRepositoryConflict:
            errors.append("FENCED")
        except Exception as exc:
            errors.append(type(exc).__name__)
        finally:
            self.control_repository.release_lease(claim)
        return ObservabilityTickResult(
            lease_acquired=True,
            cycles=tuple(cycles),
            run_samples=tuple(samples),
            summaries=tuple(summaries),
            command_count=command_count,
            skipped_budget=skipped_budget,
            errors=tuple(errors),
        )

    def _collect(
        self,
        claim: LeaseClaim,
        *,
        lane: str,
        callback: Callable[[], SourceCollection],
    ) -> tuple[SourceCollection, LeaseClaim]:
        try:
            source = callback()
        except Exception as exc:
            source = SourceCollection(
                command_count=1,
                partial=True,
                failed=True,
                error_code=f"{lane}:{type(exc).__name__}",
                warnings=(f"{lane.upper()}_FAILED",),
            )
        renewed = self.control_repository.renew_lease(
            claim, lease_seconds=self.policy.lease_seconds
        )
        return source, renewed

    def _save_cycle(
        self,
        connection_id: str,
        lane: str,
        claim: LeaseClaim,
        source: SourceCollection,
    ) -> ObservationCycle:
        now = _timestamp(self._now())
        cycle = ObservationCycle(
            cycle_id=f"cycle-{uuid.uuid4().hex}",
            connection_id=connection_id,
            lane=lane,
            fencing_token=claim.fencing_token,
            scheduled_at=now,
            started_at=now,
            completed_at=now,
            command_count=source.command_count,
            status=(
                "failed"
                if source.failed
                else "partial"
                if source.partial or source.warnings
                else "complete"
            ),
            warnings=source.warnings,
        )
        return self.store.save_cycle(cycle)

    def _due(self, connection_id: str, lane: str, interval: int) -> bool:
        latest = self.store.latest_cycle(connection_id, lane=lane)
        if latest is None:
            return True
        wait = (
            self.policy.failure_backoff_seconds
            if latest.status == "failed"
            else max(self.policy.minimum_interval_seconds, interval)
        )
        return self._now() >= _parse(latest.completed_at) + timedelta(seconds=wait)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("collector clock must be timezone-aware")
        return value.astimezone(UTC)


def _batches(
    values: tuple[RunObservationTarget, ...], size: int
) -> tuple[tuple[RunObservationTarget, ...], ...]:
    return tuple(values[index : index + size] for index in range(0, len(values), size))


def _estimated_command_count(adapter: ObservationSourceAdapter, lane: str) -> int:
    estimator = getattr(adapter, "estimated_command_count", None)
    value = 1 if estimator is None else estimator(lane)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("adapter command estimate must be a positive integer")
    return value


def _terminal(state: str) -> bool:
    return state in {
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "SUBMIT_FAILED",
        "COLLECTION_FAILED",
        "AUTH_REQUIRED",
        "ORPHANED",
    }


def _id(prefix: str, *parts: object) -> str:
    raw = "|".join(str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode()).hexdigest()[:32]}"


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _terminal_digest(item: object) -> str:
    from pilot107.observability.adapters import SourceRunObservation

    if not isinstance(item, SourceRunObservation):
        raise TypeError("terminal item must be SourceRunObservation")
    payload = {
        "target": {
            "connection_id": item.target.connection_id,
            "owner": item.target.owner,
            "run_id": item.target.run_id,
            "job_id": item.target.job_id,
            "attempt": item.target.attempt,
        },
        "used": {
            name: {
                key: value
                for key, value in measure.__dict__.items()
                if key != "captured_at"
            }
            for name, measure in item.measures.as_dict().items()
        },
        "allocated": {
            name: {
                key: value
                for key, value in measure.__dict__.items()
                if key != "captured_at"
            }
            for name, measure in (item.allocated or item.measures.__class__()).as_dict().items()
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _terminal_ready(item: object) -> bool:
    from pilot107.observability.adapters import SourceRunObservation

    if not isinstance(item, SourceRunObservation):
        return False
    allocated = item.allocated
    required = (
        item.measures.total_cpu,
        item.measures.cpu_time_raw,
        item.measures.elapsed,
        None if allocated is None else allocated.allocated_cpus,
    )
    return all(
        measure is not None and measure.availability == "available"
        for measure in required
    )
