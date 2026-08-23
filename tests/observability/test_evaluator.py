from __future__ import annotations

from pilot107.observability.evaluator import ResourceEvaluator
from pilot107.observability.model import (
    AccountPulse,
    ObservedMeasure,
    ResourceMeasureSet,
    RunResourceSummary,
)


def _measure(
    value: float | int | str | None,
    *,
    unit: str,
    coverage: float | None = 1.0,
    availability: str = "available",
) -> ObservedMeasure:
    return ObservedMeasure(
        value=value,
        unit=unit,
        availability=availability,
        source_adapter="test",
        source_operation="summary",
        captured_at="2026-08-19T00:00:00Z",
        quality="verified" if value is not None else "unavailable",
        coverage=coverage if value is not None else None,
        warning=None if value is not None else "missing",
    )


def _summary(
    *,
    used: ResourceMeasureSet,
    allocated: ResourceMeasureSet,
    run_id: str = "run1",
) -> RunResourceSummary:
    return RunResourceSummary(
        observation_id=f"summary-{run_id}",
        connection_id="connection1",
        owner="alice",
        run_id=run_id,
        attempt=0,
        cycle_id=f"cycle-{run_id}",
        captured_at="2026-08-19T00:00:00Z",
        freshness="terminal",
        partial=False,
        warnings=(),
        fencing_token=1,
        used=used,
        allocated=allocated,
    )


def _rules(summary: RunResourceSummary, **kwargs: object) -> set[str]:
    return {
        item.rule_id
        for item in ResourceEvaluator().evaluate(summary, **kwargs)  # type: ignore[arg-type]
    }


def test_cpu_underutilized_requires_ten_minutes_and_complete_cpu_accounting() -> None:
    allocated = ResourceMeasureSet(
        allocated_cpus=_measure(4, unit="cpu"),
    )
    short = _summary(
        used=ResourceMeasureSet(
            elapsed=_measure(599, unit="seconds"),
            total_cpu=_measure(100, unit="seconds"),
            cpu_time_raw=_measure(2396, unit="cpu_seconds"),
        ),
        allocated=allocated,
    )
    complete = _summary(
        used=ResourceMeasureSet(
            elapsed=_measure(600, unit="seconds"),
            total_cpu=_measure(120, unit="seconds"),
            cpu_time_raw=_measure(2400, unit="cpu_seconds"),
        ),
        allocated=allocated,
    )

    assert "CPU_UNDERUTILIZED" not in _rules(short)
    evaluations = ResourceEvaluator().evaluate(complete)
    cpu = next(item for item in evaluations if item.rule_id == "CPU_UNDERUTILIZED")
    assert cpu.confidence == "high"
    assert cpu.measured_values["cpu_efficiency"] == 0.05
    assert cpu.suggested_contract_patch


def test_cpu_rule_rejects_missing_cpu_time_raw_even_with_total_cpu() -> None:
    summary = _summary(
        used=ResourceMeasureSet(
            elapsed=_measure(900, unit="seconds"),
            total_cpu=_measure(100, unit="seconds"),
        ),
        allocated=ResourceMeasureSet(allocated_cpus=_measure(4, unit="cpu")),
    )

    assert "CPU_UNDERUTILIZED" not in _rules(summary)


def test_multitask_maxrss_cannot_trigger_memory_overallocated() -> None:
    summary = _summary(
        used=ResourceMeasureSet(max_rss=_measure(1 * 1024**3, unit="bytes")),
        allocated=ResourceMeasureSet(
            allocated_memory=_measure(16 * 1024**3, unit="bytes"),
            extras=(("task_count", _measure(8, unit="tasks")),),
        ),
    )

    assert "MEMORY_OVERALLOCATED" not in _rules(summary)


def test_single_task_maxrss_can_trigger_memory_overallocated() -> None:
    summary = _summary(
        used=ResourceMeasureSet(max_rss=_measure(1 * 1024**3, unit="bytes")),
        allocated=ResourceMeasureSet(
            allocated_memory=_measure(16 * 1024**3, unit="bytes"),
            extras=(("task_count", _measure(1, unit="tasks")),),
        ),
    )

    assert "MEMORY_OVERALLOCATED" in _rules(summary)


def test_gpu_underutilized_requires_eighty_percent_coverage_and_ten_minutes() -> None:
    insufficient = _summary(
        used=ResourceMeasureSet(
            elapsed=_measure(900, unit="seconds"),
            gpu_utilization=_measure(0.1, unit="ratio", coverage=0.79),
        ),
        allocated=ResourceMeasureSet(allocated_gpus=_measure(1, unit="gpu")),
    )
    sufficient = _summary(
        used=ResourceMeasureSet(
            elapsed=_measure(900, unit="seconds"),
            gpu_utilization=_measure(0.1, unit="ratio", coverage=0.8),
        ),
        allocated=ResourceMeasureSet(allocated_gpus=_measure(1, unit="gpu")),
    )

    assert "GPU_UNDERUTILIZED" not in _rules(insufficient)
    assert "GPU_UNDERUTILIZED" in _rules(sufficient)


def test_walltime_overrequested_is_low_confidence_until_three_comparable_runs() -> None:
    def value(run_id: str) -> RunResourceSummary:
        return _summary(
            run_id=run_id,
            used=ResourceMeasureSet(elapsed=_measure(600, unit="seconds")),
            allocated=ResourceMeasureSet(
                extras=(("requested_walltime", _measure(4000, unit="seconds")),)
            ),
        )

    current = value("run1")
    one = ResourceEvaluator().evaluate(current)
    repeated = ResourceEvaluator().evaluate(
        current,
        comparable_summaries=(value("run2"), value("run3")),
    )

    single = next(item for item in one if item.rule_id == "WALLTIME_OVERREQUESTED")
    assert single.confidence == "low"
    assert (
        next(item for item in repeated if item.rule_id == "WALLTIME_OVERREQUESTED").confidence
        == "high"
    )


def test_queue_congestion_requires_three_non_decreasing_owner_pulses() -> None:
    def pulse(index: int, pending: int) -> AccountPulse:
        return AccountPulse(
            observation_id=f"account-{index}",
            connection_id="connection1",
            owner="alice",
            run_id=None,
            attempt=None,
            cycle_id=f"cycle-{index}",
            captured_at=f"2026-08-19T00:00:0{index}Z",
            freshness="fresh",
            partial=False,
            warnings=(),
            fencing_token=1,
            measures=ResourceMeasureSet(
                extras=(("jobs_pending", _measure(pending, unit="jobs")),)
            ),
        )

    evaluator = ResourceEvaluator()

    assert evaluator.evaluate_queue_trend((pulse(1, 1), pulse(2, 2))) is None
    result = evaluator.evaluate_queue_trend(
        (pulse(1, 2), pulse(2, 3), pulse(3, 4))
    )

    assert result is not None
    assert result.rule_id == "QUEUE_CONGESTION"
    assert result.confidence == "medium"
