"""Typed resource facts that preserve availability and provenance."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Literal

type MeasureValue = float | int | str | None
Availability = Literal[
    "available",
    "unsupported",
    "permission_denied",
    "not_collected",
    "insufficient_coverage",
    "invalid",
]
Quality = Literal["verified", "estimated", "partial", "unavailable", "invalid"]
Freshness = Literal["fresh", "stale", "expired", "terminal"]
CycleStatus = Literal["complete", "partial", "failed", "skipped_budget"]

_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_MEASURE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


@dataclass(frozen=True)
class ObservedMeasure:
    value: MeasureValue
    unit: str
    availability: Availability
    source_adapter: str
    source_operation: str
    captured_at: str
    quality: Quality
    coverage: float | None
    warning: str | None

    def __post_init__(self) -> None:
        if self.availability not in {
            "available",
            "unsupported",
            "permission_denied",
            "not_collected",
            "insufficient_coverage",
            "invalid",
        }:
            raise ValueError("measure availability is invalid")
        if self.availability == "available" and self.value is None:
            raise ValueError("available measure requires a value")
        if self.availability != "available" and self.value is not None:
            raise ValueError("unavailable measure value must be None (null)")
        if isinstance(self.value, bool) or (
            isinstance(self.value, float) and not math.isfinite(self.value)
        ):
            raise ValueError("measure value is invalid")
        if not self.unit or len(self.unit) > 64:
            raise ValueError("measure unit is invalid")
        _identifier(self.source_adapter, "source_adapter")
        _identifier(self.source_operation, "source_operation")
        _timestamp(self.captured_at)
        if self.quality not in {
            "verified",
            "estimated",
            "partial",
            "unavailable",
            "invalid",
        }:
            raise ValueError("measure quality is invalid")
        if self.coverage is not None and (
            isinstance(self.coverage, bool)
            or not math.isfinite(self.coverage)
            or not 0 <= self.coverage <= 1
        ):
            raise ValueError("measure coverage is invalid")
        if self.warning is not None and (not self.warning or len(self.warning) > 4096):
            raise ValueError("measure warning is invalid")


@dataclass(frozen=True)
class ResourceMeasureSet:
    cpu_utilization: ObservedMeasure | None = None
    gpu_utilization: ObservedMeasure | None = None
    max_rss: ObservedMeasure | None = None
    total_cpu: ObservedMeasure | None = None
    cpu_time_raw: ObservedMeasure | None = None
    elapsed: ObservedMeasure | None = None
    allocated_cpus: ObservedMeasure | None = None
    allocated_memory: ObservedMeasure | None = None
    allocated_gpus: ObservedMeasure | None = None
    extras: tuple[tuple[str, ObservedMeasure], ...] = ()

    def __post_init__(self) -> None:
        extras = tuple(self.extras)
        names = [name for name, _ in extras]
        if len(names) != len(set(names)) or any(_MEASURE.fullmatch(name) is None for name in names):
            raise ValueError("extra measure names are invalid")
        if any(not isinstance(value, ObservedMeasure) for _, value in extras):
            raise TypeError("extra measures must be ObservedMeasure values")
        object.__setattr__(self, "extras", extras)

    def as_dict(self) -> dict[str, ObservedMeasure]:
        result = {
            item.name: value
            for item in fields(self)
            if item.name != "extras"
            and (value := getattr(self, item.name)) is not None
        }
        result.update(self.extras)
        return result


@dataclass(frozen=True)
class _Observation:
    observation_id: str
    connection_id: str
    owner: str | None
    run_id: str | None
    attempt: int | None
    cycle_id: str
    captured_at: str
    freshness: Freshness
    partial: bool
    warnings: tuple[str, ...]
    fencing_token: int

    def __post_init__(self) -> None:
        _identifier(self.observation_id, "observation_id")
        _identifier(self.connection_id, "connection_id")
        _identifier(self.cycle_id, "cycle_id")
        if self.owner is not None:
            _identifier(self.owner, "owner")
        if self.run_id is not None:
            _identifier(self.run_id, "run_id")
        if self.attempt is not None and (
            isinstance(self.attempt, bool) or not 0 <= self.attempt <= 1_048_576
        ):
            raise ValueError("attempt is invalid")
        _timestamp(self.captured_at)
        if self.freshness not in {"fresh", "stale", "expired", "terminal"}:
            raise ValueError("freshness is invalid")
        if not isinstance(self.partial, bool):
            raise TypeError("partial must be boolean")
        warnings = tuple(self.warnings)
        if len(warnings) > 256 or any(not item or len(item) > 4096 for item in warnings):
            raise ValueError("warnings are invalid")
        if isinstance(self.fencing_token, bool) or self.fencing_token < 0:
            raise ValueError("fencing_token is invalid")
        object.__setattr__(self, "warnings", warnings)


@dataclass(frozen=True)
class PlatformPulse(_Observation):
    measures: ResourceMeasureSet


@dataclass(frozen=True)
class AccountPulse(_Observation):
    measures: ResourceMeasureSet


@dataclass(frozen=True)
class RunResourceSample(_Observation):
    measures: ResourceMeasureSet

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.owner is None or self.run_id is None or self.attempt is None:
            raise ValueError("Run resource sample requires owner, run_id, and attempt")


@dataclass(frozen=True)
class RunResourceSummary(_Observation):
    used: ResourceMeasureSet
    allocated: ResourceMeasureSet

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.owner is None or self.run_id is None or self.attempt is None:
            raise ValueError("Run resource summary requires owner, run_id, and attempt")
        if self.freshness != "terminal":
            raise ValueError("Run resource summary must be terminal")


@dataclass(frozen=True)
class ObservationCycle:
    cycle_id: str
    connection_id: str
    lane: str
    fencing_token: int
    scheduled_at: str
    started_at: str
    completed_at: str
    command_count: int
    status: CycleStatus
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.cycle_id, "cycle_id")
        _identifier(self.connection_id, "connection_id")
        _identifier(self.lane, "lane")
        if isinstance(self.fencing_token, bool) or self.fencing_token <= 0:
            raise ValueError("fencing_token is invalid")
        scheduled = _timestamp_value(self.scheduled_at)
        started = _timestamp_value(self.started_at)
        completed = _timestamp_value(self.completed_at)
        if not scheduled <= started <= completed:
            raise ValueError("cycle timestamps are out of order")
        if isinstance(self.command_count, bool) or self.command_count < 0:
            raise ValueError("command_count is invalid")
        if self.status not in {"complete", "partial", "failed", "skipped_budget"}:
            raise ValueError("cycle status is invalid")
        warnings = tuple(self.warnings)
        if len(warnings) > 256 or any(not item or len(item) > 4096 for item in warnings):
            raise ValueError("cycle warnings are invalid")
        object.__setattr__(self, "warnings", warnings)


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _timestamp(value: str) -> None:
    _timestamp_value(value)


def _timestamp_value(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed
