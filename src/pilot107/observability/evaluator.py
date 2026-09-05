"""Deterministic, evidence-bounded resource evaluation rules."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal

from pilot107.observability.model import AccountPulse, ObservedMeasure, RunResourceSummary

Severity = Literal["info", "warning", "error"]
Confidence = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class ResourceEvaluation:
    evaluation_id: str
    run_id: str | None
    summary_id: str
    rule_id: str
    rule_version: str
    severity: Severity
    confidence: Confidence
    summary: str
    measured_values: dict[str, float | int]
    thresholds: dict[str, float | int]
    evidence_refs: tuple[str, ...]
    suggested_contract_patch: dict[str, float | int | str | bool | None]


class ResourceEvaluator:
    CPU_THRESHOLD = 0.20
    MEMORY_THRESHOLD = 0.30
    GPU_THRESHOLD = 0.20
    GPU_MINIMUM_COVERAGE = 0.80
    MINIMUM_RUNTIME_SECONDS = 600
    WALLTIME_THRESHOLD = 0.20
    RULE_VERSION = "1"

    def evaluate_queue_trend(
        self, pulses: tuple[AccountPulse, ...]
    ) -> ResourceEvaluation | None:
        if len(pulses) < 3:
            return None
        selected = pulses[-3:]
        if (
            len({(item.connection_id, item.owner) for item in selected}) != 1
            or any(item.partial for item in selected)
        ):
            return None
        pending = [
            _number(item.measures.as_dict().get("jobs_pending"), unit="jobs")
            for item in selected
        ]
        if (
            any(value is None for value in pending)
            or pending[-1] is None
            or pending[-1] < 3
            or any(
                current is None or following is None or current > following
                for current, following in pairwise(pending)
            )
        ):
            return None
        latest = selected[-1]
        raw = f"{latest.observation_id}|QUEUE_CONGESTION|{self.RULE_VERSION}"
        return ResourceEvaluation(
            evaluation_id=f"evaluation-{hashlib.sha256(raw.encode()).hexdigest()[:32]}",
            run_id=None,
            summary_id=latest.observation_id,
            rule_id="QUEUE_CONGESTION",
            rule_version=self.RULE_VERSION,
            severity="info",
            confidence="medium",
            summary=(
                "The caller's pending-job count has not decreased across three "
                "account pulses; this does not prove cluster-wide congestion."
            ),
            measured_values={"pending_jobs": int(pending[-1])},
            thresholds={"minimum_pending_jobs": 3, "minimum_pulses": 3},
            evidence_refs=tuple(
                f"observation:{item.observation_id}" for item in selected
            ),
            suggested_contract_patch={},
        )

    def evaluate(
        self,
        summary: RunResourceSummary,
        *,
        comparable_summaries: tuple[RunResourceSummary, ...] = (),
    ) -> tuple[ResourceEvaluation, ...]:
        evaluations = [
            result
            for result in (
                self._cpu(summary),
                self._memory(summary),
                self._gpu(summary),
                self._walltime(summary, comparable_summaries),
            )
            if result is not None
        ]
        return tuple(evaluations)

    def _cpu(self, summary: RunResourceSummary) -> ResourceEvaluation | None:
        elapsed = _number(summary.used.elapsed, unit="seconds")
        total_cpu = _number(summary.used.total_cpu, unit="seconds")
        cpu_time_raw = _number(
            summary.used.cpu_time_raw, units={"cpu_seconds", "seconds"}
        )
        allocated_cpus = _number(summary.allocated.allocated_cpus, unit="cpu")
        if (
            elapsed is None
            or elapsed < self.MINIMUM_RUNTIME_SECONDS
            or total_cpu is None
            or cpu_time_raw is None
            or cpu_time_raw <= 0
            or allocated_cpus is None
            or allocated_cpus <= 0
        ):
            return None
        efficiency = total_cpu / cpu_time_raw
        if not 0 <= efficiency < self.CPU_THRESHOLD:
            return None
        suggested = max(1, math.ceil(allocated_cpus * max(efficiency / 0.7, 0.25)))
        return self._evaluation(
            summary,
            rule_id="CPU_UNDERUTILIZED",
            severity="warning",
            confidence="high",
            text=(
                "CPU accounting indicates low utilization; review parallelism "
                "before reducing CPUs."
            ),
            measured={
                "cpu_efficiency": efficiency,
                "total_cpu_seconds": total_cpu,
                "cpu_time_raw_seconds": cpu_time_raw,
            },
            thresholds={
                "maximum_efficiency": self.CPU_THRESHOLD,
                "minimum_runtime_seconds": self.MINIMUM_RUNTIME_SECONDS,
            },
            patch={"resources.cpus_per_task": suggested},
        )

    def _memory(self, summary: RunResourceSummary) -> ResourceEvaluation | None:
        peak = _number(summary.used.max_rss, unit="bytes")
        allocated = _number(summary.allocated.allocated_memory, unit="bytes")
        task_count = _extra_number(summary, allocated=True, name="task_count", unit="tasks")
        scope = _extra_string(summary, allocated=False, name="max_rss_scope")
        reliable_job_peak = scope == "job"
        if (
            peak is None
            or allocated is None
            or allocated <= 0
            or not (task_count == 1 or reliable_job_peak)
        ):
            return None
        ratio = peak / allocated
        if not 0 <= ratio < self.MEMORY_THRESHOLD:
            return None
        suggested = max(1, math.ceil(peak * 1.5))
        return self._evaluation(
            summary,
            rule_id="MEMORY_OVERALLOCATED",
            severity="warning",
            confidence="high" if reliable_job_peak else "medium",
            text=(
                "Reliable peak memory is well below the allocation; review a "
                "smaller memory request."
            ),
            measured={"memory_ratio": ratio, "max_rss_bytes": peak},
            thresholds={"maximum_ratio": self.MEMORY_THRESHOLD},
            patch={"resources.memory_bytes": suggested},
        )

    def _gpu(self, summary: RunResourceSummary) -> ResourceEvaluation | None:
        elapsed = _number(summary.used.elapsed, unit="seconds")
        utilization = _number(summary.used.gpu_utilization, unit="ratio")
        allocated = _number(summary.allocated.allocated_gpus, unit="gpu")
        coverage = (
            summary.used.gpu_utilization.coverage
            if summary.used.gpu_utilization is not None
            else None
        )
        if (
            elapsed is None
            or elapsed < self.MINIMUM_RUNTIME_SECONDS
            or utilization is None
            or not 0 <= utilization < self.GPU_THRESHOLD
            or allocated is None
            or allocated <= 0
            or coverage is None
            or coverage < self.GPU_MINIMUM_COVERAGE
        ):
            return None
        return self._evaluation(
            summary,
            rule_id="GPU_UNDERUTILIZED",
            severity="warning",
            confidence="high",
            text=(
                "Covered GPU samples indicate low utilization; inspect the input "
                "pipeline and batch size."
            ),
            measured={"gpu_utilization": utilization, "coverage": coverage},
            thresholds={
                "maximum_utilization": self.GPU_THRESHOLD,
                "minimum_coverage": self.GPU_MINIMUM_COVERAGE,
                "minimum_runtime_seconds": self.MINIMUM_RUNTIME_SECONDS,
            },
            patch={},
        )

    def _walltime(
        self,
        summary: RunResourceSummary,
        comparable: tuple[RunResourceSummary, ...],
    ) -> ResourceEvaluation | None:
        ratio = _walltime_ratio(summary)
        if ratio is None or not 0 <= ratio < self.WALLTIME_THRESHOLD:
            return None
        comparable_ratios = [
            item for candidate in comparable if (item := _walltime_ratio(candidate)) is not None
        ]
        repeated = (
            len(comparable_ratios) + 1 >= 3
            and all(item < self.WALLTIME_THRESHOLD for item in comparable_ratios)
        )
        elapsed = _number(summary.used.elapsed, unit="seconds")
        assert elapsed is not None
        suggested = max(math.ceil(elapsed * 2), math.ceil(elapsed + 300))
        return self._evaluation(
            summary,
            rule_id="WALLTIME_OVERREQUESTED",
            severity="info",
            confidence="high" if repeated else "low",
            text=(
                "Runtime used less than 20% of requested walltime; repeated comparable "
                "Runs strengthen this signal."
            ),
            measured={"walltime_ratio": ratio, "runtime_seconds": elapsed},
            thresholds={"maximum_ratio": self.WALLTIME_THRESHOLD},
            patch={"resources.walltime_seconds": suggested},
        )

    def _evaluation(
        self,
        summary: RunResourceSummary,
        *,
        rule_id: str,
        severity: Severity,
        confidence: Confidence,
        text: str,
        measured: dict[str, float | int],
        thresholds: dict[str, float | int],
        patch: dict[str, float | int | str | bool | None],
    ) -> ResourceEvaluation:
        assert summary.run_id is not None
        raw = f"{summary.observation_id}|{rule_id}|{self.RULE_VERSION}"
        evaluation_id = f"evaluation-{hashlib.sha256(raw.encode()).hexdigest()[:32]}"
        return ResourceEvaluation(
            evaluation_id=evaluation_id,
            run_id=summary.run_id,
            summary_id=summary.observation_id,
            rule_id=rule_id,
            rule_version=self.RULE_VERSION,
            severity=severity,
            confidence=confidence,
            summary=text,
            measured_values=measured,
            thresholds=thresholds,
            evidence_refs=(f"resource-summary:{summary.observation_id}",),
            suggested_contract_patch=patch,
        )


def _number(
    measure: ObservedMeasure | None,
    *,
    unit: str | None = None,
    units: set[str] | None = None,
) -> float | None:
    accepted = units if units is not None else ({unit} if unit is not None else set())
    if (
        measure is None
        or measure.availability != "available"
        or measure.quality not in {"verified", "estimated"}
        or measure.unit not in accepted
        or isinstance(measure.value, str)
        or measure.value is None
    ):
        return None
    return float(measure.value)


def _extra_number(
    summary: RunResourceSummary, *, allocated: bool, name: str, unit: str
) -> float | None:
    measures = summary.allocated if allocated else summary.used
    return _number(measures.as_dict().get(name), unit=unit)


def _extra_string(
    summary: RunResourceSummary, *, allocated: bool, name: str
) -> str | None:
    measures = summary.allocated if allocated else summary.used
    measure = measures.as_dict().get(name)
    if (
        measure is None
        or measure.availability != "available"
        or measure.quality != "verified"
        or not isinstance(measure.value, str)
    ):
        return None
    return measure.value


def _walltime_ratio(summary: RunResourceSummary) -> float | None:
    elapsed = _number(summary.used.elapsed, unit="seconds")
    requested = _extra_number(
        summary, allocated=True, name="requested_walltime", unit="seconds"
    )
    if elapsed is None or requested is None or requested <= 0:
        return None
    return elapsed / requested
