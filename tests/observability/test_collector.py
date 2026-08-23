from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.observability.adapters import (
    RunObservationTarget,
    SourceCollection,
    SourceRunObservation,
)
from pilot107.observability.collector import (
    ObservabilityCollector,
    ObservabilityCollectorPolicy,
)
from pilot107.observability.model import (
    ObservedMeasure,
    ResourceMeasureSet,
)
from pilot107.observability.store import SQLiteObservabilityStore


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 19, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


def _measure(
    value: float | int | None,
    *,
    operation: str,
    availability: str = "available",
    unit: str = "bytes",
) -> ObservedMeasure:
    return ObservedMeasure(
        value=value,
        unit=unit,
        availability=availability,
        source_adapter="scripted",
        source_operation=operation,
        captured_at="2026-08-19T00:00:00Z",
        quality="verified" if value is not None else "unavailable",
        coverage=1.0 if value is not None else None,
        warning=None if value is not None else "source unavailable",
    )


def _target(run_id: str, job_id: str) -> RunObservationTarget:
    return RunObservationTarget(
        connection_id="connection1",
        owner="alice",
        run_id=run_id,
        job_id=job_id,
        attempt=0,
    )


@dataclass
class ScriptedAdapter:
    active_measure: ObservedMeasure = field(
        default_factory=lambda: _measure(1024, operation="sstat")
    )
    calls: list[str] = field(default_factory=list)
    on_active: Callable[[], None] | None = None
    fail_active: bool = False
    terminal_total_cpu: ObservedMeasure = field(
        default_factory=lambda: _measure(30, operation="sacct", unit="seconds")
    )

    def estimated_command_count(self, lane: str) -> int:
        return 2 if lane == "platform_account" else 1

    def collect_capability(self, connection_id: str) -> SourceCollection:
        del connection_id
        self.calls.append("capability")
        return SourceCollection(command_count=0, warnings=("CAPABILITY_NOT_CONFIGURED",))

    def collect_platform_account(
        self, connection_id: str, owners: tuple[str, ...]
    ) -> SourceCollection:
        del connection_id, owners
        self.calls.append("platform_account")
        return SourceCollection(command_count=0, warnings=("PLATFORM_NOT_CONFIGURED",))

    def collect_active_runs(
        self, connection_id: str, targets: tuple[RunObservationTarget, ...]
    ) -> SourceCollection:
        del connection_id
        self.calls.append("active")
        if self.on_active is not None:
            self.on_active()
        if self.fail_active:
            raise TimeoutError("sstat deadline exceeded")
        return SourceCollection(
            run_observations=tuple(
                SourceRunObservation(
                    target=target,
                    measures=ResourceMeasureSet(max_rss=self.active_measure),
                )
                for target in targets
            ),
            command_count=1,
            partial=self.active_measure.value is None,
        )

    def collect_terminal_runs(
        self, connection_id: str, targets: tuple[RunObservationTarget, ...]
    ) -> SourceCollection:
        del connection_id
        self.calls.append("terminal")
        return SourceCollection(
            run_observations=tuple(
                SourceRunObservation(
                    target=target,
                    measures=ResourceMeasureSet(
                        total_cpu=self.terminal_total_cpu,
                        elapsed=_measure(60, operation="sacct", unit="seconds"),
                    ),
                    allocated=ResourceMeasureSet(
                        allocated_cpus=_measure(1, operation="sacct", unit="cpu")
                    ),
                )
                for target in targets
            ),
            command_count=1,
        )


def _collector(
    tmp_path: Path,
    *,
    clock: MutableClock,
    adapter: ScriptedAdapter,
    worker_id: str = "worker-a",
    max_commands_per_minute: int = 20,
) -> tuple[ObservabilityCollector, SQLiteObservabilityStore]:
    db_path = tmp_path / "observability.db"
    store = SQLiteObservabilityStore(db_path, clock=clock)
    collector = ObservabilityCollector(
        store=store,
        control_repository=SQLiteControlRepository(db_path, clock=clock),
        adapter=adapter,
        worker_id=worker_id,
        clock=clock,
        policy=ObservabilityCollectorPolicy(
            capability_interval_seconds=300,
            platform_interval_seconds=20,
            active_run_interval_seconds=30,
            minimum_interval_seconds=1,
            max_commands_per_minute=max_commands_per_minute,
            max_concurrent_requests=1,
            command_deadline_seconds=10,
            batch_size=50,
            failure_backoff_seconds=5,
            lease_seconds=45,
        ),
    )
    return collector, store


def test_failed_sstat_is_persisted_as_not_collected_not_zero(tmp_path: Path) -> None:
    clock = MutableClock()
    adapter = ScriptedAdapter(
        active_measure=_measure(
            None,
            operation="sstat",
            availability="not_collected",
        )
    )
    collector, store = _collector(tmp_path, clock=clock, adapter=adapter)
    collector.observe_run(_target("run1", "101"), state="RUNNING")

    result = collector.tick("connection1")

    assert result.lease_acquired is True
    assert len(result.run_samples) == 1
    persisted = store.list_run_samples("run1", owner="alice")[0]
    assert persisted.measures.max_rss is not None
    assert persisted.measures.max_rss.availability == "not_collected"
    assert persisted.measures.max_rss.value is None


def test_connection_lease_prevents_second_writer_during_remote_read(tmp_path: Path) -> None:
    clock = MutableClock()
    adapter_a = ScriptedAdapter()
    adapter_b = ScriptedAdapter()
    first, store = _collector(tmp_path, clock=clock, adapter=adapter_a, worker_id="worker-a")
    second, _ = _collector(tmp_path, clock=clock, adapter=adapter_b, worker_id="worker-b")
    first.observe_run(_target("run1", "101"), state="RUNNING")
    observed_second = []
    adapter_a.on_active = lambda: observed_second.append(second.tick("connection1"))

    first_result = first.tick("connection1")

    assert first_result.lease_acquired is True
    assert observed_second[0].lease_acquired is False
    assert adapter_b.calls == []
    assert len(store.list_run_samples("run1", owner="alice")) == 1


def test_terminal_accounting_consumes_budget_before_active_sampling(tmp_path: Path) -> None:
    clock = MutableClock()
    adapter = ScriptedAdapter()
    collector, store = _collector(
        tmp_path,
        clock=clock,
        adapter=adapter,
        max_commands_per_minute=1,
    )
    collector.observe_run(_target("run-active", "101"), state="RUNNING")
    collector.observe_run(_target("run-terminal", "102"), state="SUCCEEDED")

    result = collector.tick("connection1")

    assert adapter.calls == ["terminal"]
    assert result.command_count == 1
    assert result.skipped_budget is True
    with pytest.raises(KeyError):
        store.get_summary("run-terminal", owner="alice")
    assert store.list_run_samples("run-active", owner="alice") == []


def test_platform_lane_does_not_start_when_two_command_cost_exceeds_budget(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    adapter = ScriptedAdapter()
    collector, _ = _collector(
        tmp_path,
        clock=clock,
        adapter=adapter,
        max_commands_per_minute=1,
    )

    result = collector.tick("connection1")

    assert adapter.calls == ["capability"]
    assert result.skipped_budget is True


def test_terminal_summary_requires_two_stable_accounting_observations(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    adapter = ScriptedAdapter()
    collector, store = _collector(tmp_path, clock=clock, adapter=adapter)
    collector.observe_run(_target("run-terminal", "102"), state="SUCCEEDED")

    first = collector.tick("connection1")
    with pytest.raises(KeyError):
        store.get_summary("run-terminal", owner="alice")

    clock.advance(1)
    second = collector.tick("connection1")

    assert first.summaries == ()
    assert len(second.summaries) == 1
    assert store.get_summary("run-terminal", owner="alice").used.total_cpu is not None


def test_incomplete_terminal_accounting_cannot_satisfy_stability(tmp_path: Path) -> None:
    clock = MutableClock()
    adapter = ScriptedAdapter(
        terminal_total_cpu=_measure(
            None,
            operation="sacct",
            availability="not_collected",
            unit="seconds",
        )
    )
    collector, store = _collector(tmp_path, clock=clock, adapter=adapter)
    collector.observe_run(_target("run-terminal", "102"), state="SUCCEEDED")

    collector.tick("connection1")
    clock.advance(1)
    collector.tick("connection1")

    with pytest.raises(KeyError):
        store.get_summary("run-terminal", owner="alice")

    adapter.terminal_total_cpu = _measure(30, operation="sacct", unit="seconds")
    clock.advance(1)
    collector.tick("connection1")
    clock.advance(1)
    collector.tick("connection1")
    assert store.get_summary("run-terminal", owner="alice").used.total_cpu is not None


def test_pending_run_never_enters_sstat_lane(tmp_path: Path) -> None:
    clock = MutableClock()
    adapter = ScriptedAdapter()
    collector, _ = _collector(tmp_path, clock=clock, adapter=adapter)
    collector.observe_run(_target("run-pending", "101"), state="PENDING")

    collector.tick("connection1")

    assert "active" not in adapter.calls


def test_failed_lane_uses_persisted_backoff_before_retry(tmp_path: Path) -> None:
    clock = MutableClock()
    adapter = ScriptedAdapter(fail_active=True)
    collector, store = _collector(tmp_path, clock=clock, adapter=adapter)
    collector.observe_run(_target("run1", "101"), state="RUNNING")

    first = collector.tick("connection1")
    second = collector.tick("connection1")

    assert first.errors == ("active_run:TimeoutError",)
    assert second.errors == ()
    assert adapter.calls.count("active") == 1
    latest = store.latest_cycle("connection1", lane="active_run")
    assert latest is not None
    assert latest.status == "failed"

    clock.advance(5)
    collector.tick("connection1")
    assert adapter.calls.count("active") == 2
