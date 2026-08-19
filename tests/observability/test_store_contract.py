from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pilot107.observability.model import (
    ObservedMeasure,
    ResourceMeasureSet,
    RunResourceSample,
    RunResourceSummary,
)
from pilot107.observability.postgres_store import PostgresObservabilityStore
from pilot107.observability.store import (
    ObservabilityConflict,
    ObservabilityStore,
    SQLiteObservabilityStore,
)


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 19, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


def _measure(
    value: float | None,
    *,
    availability: str = "available",
    unit: str = "percent",
) -> ObservedMeasure:
    return ObservedMeasure(
        value=value,
        unit=unit,
        availability=availability,
        source_adapter="slurm_cli",
        source_operation="sstat",
        captured_at="2026-08-19T00:00:00Z",
        quality="verified" if value is not None else "unavailable",
        coverage=1.0 if value is not None else None,
        warning=None if value is not None else "GPU metrics are not exposed",
    )


def _sample(observation_id: str = "sample1") -> RunResourceSample:
    return RunResourceSample(
        observation_id=observation_id,
        connection_id="connection1",
        owner="alice",
        run_id="run1",
        attempt=0,
        cycle_id="cycle1",
        captured_at="2026-08-19T00:00:00Z",
        freshness="fresh",
        partial=False,
        warnings=(),
        measures=ResourceMeasureSet(cpu_utilization=_measure(42.0)),
        fencing_token=7,
    )


def _summary(*, gpu_value: float | None = None) -> RunResourceSummary:
    return RunResourceSummary(
        observation_id="summary1",
        connection_id="connection1",
        owner="alice",
        run_id="run1",
        attempt=0,
        cycle_id="cycle-terminal",
        captured_at="2026-08-19T00:00:00Z",
        freshness="terminal",
        partial=True,
        warnings=("GPU unavailable",),
        used=ResourceMeasureSet(
            cpu_utilization=_measure(51.0),
            gpu_utilization=_measure(
                gpu_value,
                availability="unsupported" if gpu_value is None else "available",
            ),
        ),
        allocated=ResourceMeasureSet(),
        fencing_token=8,
    )


def exercise_store(store: ObservabilityStore, clock: MutableClock) -> None:
    sample = store.save_run_sample(_sample())
    assert sample.owner == "alice"
    assert store.list_run_samples("run1", owner="bob") == []

    minute = store.save_minute_aggregate(_sample("minute1"))
    assert minute.observation_id == "minute1"

    summary = store.save_summary(_summary())
    assert summary.used.gpu_utilization is not None
    assert summary.used.gpu_utilization.availability == "unsupported"
    assert summary.used.gpu_utilization.value is None
    assert store.save_summary(_summary()) == summary
    with pytest.raises(ObservabilityConflict, match="immutable"):
        store.save_summary(_summary(gpu_value=10.0))
    with pytest.raises(ObservabilityConflict, match="immutable"):
        store.save_summary(replace(_summary(), observation_id="summary2"))

    clock.advance(2 * 60 * 60)
    store.prune_expired()
    assert store.list_run_samples("run1", owner="alice") == []
    assert store.list_minute_aggregates("run1", owner="alice") == [minute]

    clock.advance(22 * 60 * 60)
    store.prune_expired()
    assert store.list_minute_aggregates("run1", owner="alice") == []
    assert store.get_summary("run1", owner="alice") == summary


def test_sqlite_observability_store_contract(tmp_path: Path) -> None:
    clock = MutableClock()
    exercise_store(SQLiteObservabilityStore(tmp_path / "observability.db", clock=clock), clock)


def test_missing_measure_cannot_be_encoded_as_zero() -> None:
    with pytest.raises(ValueError, match="unavailable.*null|value.*None"):
        _measure(0.0, availability="unsupported")


@pytest.mark.skipif(
    not os.environ.get("PILOT107_TEST_POSTGRES_DSN")
    or os.environ.get("PILOT107_TEST_POSTGRES_ALLOW_RESET") != "1",
    reason="set a dedicated PostgreSQL DSN and explicit reset opt-in",
)
def test_postgres_observability_store_contract(tmp_path: Path) -> None:
    clock = MutableClock()
    store = PostgresObservabilityStore(
        os.environ["PILOT107_TEST_POSTGRES_DSN"],
        clock=clock,
        compatibility_path=tmp_path / "compat.db",
    )
    with store.connect() as connection:
        connection.execute("TRUNCATE resource_observations")
    exercise_store(store, clock)
