"""ResourcePlan and QoS-aware preflight primitives."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class PreflightSeverity(StrEnum):
    BLOCK = "BLOCK"
    WARN = "WARN"
    INFO = "INFO"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PreflightFinding:
    severity: PreflightSeverity
    code: str
    message: str
    source_authority: str = "A6"


@dataclass(frozen=True)
class QosResourceLimit:
    max_cpus: int | None = None
    max_gpus: int | None = None
    max_memory_gb: int | None = None
    max_wall_hours: int | None = None
    source_authority: str = "docs-main"


@dataclass(frozen=True)
class ArraySpec:
    expression: str
    max_concurrency: int | None = None


@dataclass(frozen=True)
class ResourcePlan:
    partition: str
    qos: str | None
    nodes: int
    ntasks: int
    cpus_per_task: int
    memory_value: int | None = None
    memory_unit: str | None = None
    gpus_per_node: int | None = None
    gpus_total: int | None = None
    gpu_type: str | None = None
    time_limit: str | None = None
    array: ArraySpec | None = None

    @property
    def derived_cpu_upper_bound(self) -> int:
        return self.ntasks * self.cpus_per_task

    @property
    def derived_gpu_total(self) -> int:
        if self.gpus_total is not None:
            return self.gpus_total
        if self.gpus_per_node is not None:
            return self.nodes * self.gpus_per_node
        return 0

    @property
    def derived_parallelism(self) -> int:
        if self.array and self.array.max_concurrency:
            return self.array.max_concurrency
        return self.ntasks


# Compatibility fixture for unit tests and importers predating CapabilityProfile.
# Product service builders must obtain partition/QoS policy from the active
# CapabilityProfile (or a fresh owner entitlement), never from this mapping.
REAL107_SIM_PARTITION_QOS: dict[str, tuple[str, ...]] = {
    "CPU-6530": ("qos_cpu-6530",),
    "CPU-8358P": ("qos_cpu-8358p",),
    "GPU-RTX5090": ("qos_gpu-rtx5090",),
    "GPU-A100": ("qos_gpu-a100",),
    "P107-RTX5090": ("qos_p107-rtx5090",),
    "P107-A100": ("qos_p107-a100",),
    "Students": (
        "qos_stu001",
        "qos_stu_default",
        "qos_stu_small",
        "qos_stu_medium",
        "qos_stu_medium_2gpu",
        "qos_stu_large",
        "qos_stu_long",
        "qos_stu_cpu_long",
    ),
    "debug": ("normal",),
}


def validate_resource_plan(
    plan: ResourcePlan,
    *,
    partition_qos: Mapping[str, tuple[str, ...]] | None = None,
    qos_limits: Mapping[str, QosResourceLimit] | None = None,
) -> list[PreflightFinding]:
    findings: list[PreflightFinding] = []

    positive_fields = {
        "nodes": plan.nodes,
        "ntasks": plan.ntasks,
        "cpus_per_task": plan.cpus_per_task,
    }
    for field, value in positive_fields.items():
        if value <= 0:
            findings.append(
                PreflightFinding(
                    severity=PreflightSeverity.BLOCK,
                    code=f"RESOURCE.INVALID_{field.upper()}",
                    message=f"{field} must be a positive integer",
                )
            )

    if plan.gpus_per_node is not None and plan.gpus_per_node < 0:
        findings.append(
            PreflightFinding(
                severity=PreflightSeverity.BLOCK,
                code="RESOURCE.INVALID_GPUS_PER_NODE",
                message="gpus_per_node cannot be negative",
            )
        )

    if plan.gpus_total is not None and plan.gpus_total < 0:
        findings.append(
            PreflightFinding(
                severity=PreflightSeverity.BLOCK,
                code="RESOURCE.INVALID_GPUS_TOTAL",
                message="gpus_total cannot be negative",
            )
        )

    if plan.gpus_per_node is not None and plan.gpus_total is not None:
        derived = plan.nodes * plan.gpus_per_node
        if derived != plan.gpus_total:
            findings.append(
                PreflightFinding(
                    severity=PreflightSeverity.BLOCK,
                    code="RESOURCE.GPU_TOTAL_CONFLICT",
                    message=(
                        "gpus_total must equal nodes * gpus_per_node when both are provided"
                    ),
                )
            )

    if not plan.time_limit:
        findings.append(
            PreflightFinding(
                severity=PreflightSeverity.BLOCK,
                code="RESOURCE.TIME_LIMIT_REQUIRED",
                message="time_limit is required because partitions may have DefaultTime=NONE",
            )
        )

    if partition_qos is not None:
        allowed_qos = partition_qos.get(plan.partition)
        if allowed_qos is None:
            findings.append(
                PreflightFinding(
                    severity=PreflightSeverity.BLOCK,
                    code="RESOURCE.UNKNOWN_PARTITION",
                    message=f"partition is not in the active cluster profile: {plan.partition}",
                    source_authority="real107_sim_profile",
                )
            )
        elif not plan.qos:
            findings.append(
                PreflightFinding(
                    severity=PreflightSeverity.BLOCK,
                    code="RESOURCE.QOS_REQUIRED",
                    message=f"partition {plan.partition} requires an explicit matching QoS",
                    source_authority="real107_sim_profile",
                )
            )
        elif plan.qos not in allowed_qos:
            findings.append(
                PreflightFinding(
                    severity=PreflightSeverity.BLOCK,
                    code="RESOURCE.QOS_NOT_ALLOWED",
                    message=f"QoS {plan.qos} is not allowed on partition {plan.partition}",
                    source_authority="real107_sim_profile",
                )
            )

    if qos_limits is not None and plan.qos:
        limits = qos_limits.get(plan.qos)
        if limits is None:
            findings.append(
                PreflightFinding(
                    severity=PreflightSeverity.WARN,
                    code="RESOURCE.QOS_LIMITS_UNKNOWN",
                    message=(
                        f"QoS {plan.qos} has no numeric limit profile; "
                        "live Slurm may still reject"
                    ),
                    source_authority="capability_profile",
                )
            )
        else:
            findings.extend(_validate_qos_limits(plan, limits))

    return findings


def _validate_qos_limits(
    plan: ResourcePlan,
    limits: QosResourceLimit,
) -> list[PreflightFinding]:
    findings: list[PreflightFinding] = []
    source = limits.source_authority
    cpu_count = plan.derived_cpu_upper_bound
    gpu_count = plan.derived_gpu_total
    memory_gb, memory_warning = _memory_to_gb(plan.memory_value, plan.memory_unit)
    wall_hours, wall_warning = _time_limit_to_hours(plan.time_limit)

    if limits.max_cpus is not None and cpu_count > limits.max_cpus:
        findings.append(
            PreflightFinding(
                severity=PreflightSeverity.BLOCK,
                code="RESOURCE.QOS_CPU_LIMIT_EXCEEDED",
                message=f"requested {cpu_count} CPUs exceeds QoS limit {limits.max_cpus}",
                source_authority=source,
            )
        )
    if limits.max_gpus is not None and gpu_count > limits.max_gpus:
        findings.append(
            PreflightFinding(
                severity=PreflightSeverity.BLOCK,
                code="RESOURCE.QOS_GPU_LIMIT_EXCEEDED",
                message=f"requested {gpu_count} GPUs exceeds QoS limit {limits.max_gpus}",
                source_authority=source,
            )
        )
    if (
        limits.max_memory_gb is not None
        and memory_gb is not None
        and memory_gb > limits.max_memory_gb
    ):
        findings.append(
            PreflightFinding(
                severity=PreflightSeverity.BLOCK,
                code="RESOURCE.QOS_MEMORY_LIMIT_EXCEEDED",
                message=(
                    f"requested {memory_gb:g}G memory exceeds QoS limit "
                    f"{limits.max_memory_gb}G"
                ),
                source_authority=source,
            )
        )
    if memory_warning is not None:
        findings.append(memory_warning)
    if (
        limits.max_wall_hours is not None
        and wall_hours is not None
        and wall_hours > limits.max_wall_hours
    ):
        findings.append(
            PreflightFinding(
                severity=PreflightSeverity.BLOCK,
                code="RESOURCE.QOS_WALLTIME_LIMIT_EXCEEDED",
                message=(
                    f"requested {wall_hours:g}h walltime exceeds QoS limit "
                    f"{limits.max_wall_hours}h"
                ),
                source_authority=source,
            )
        )
    if wall_warning is not None:
        findings.append(wall_warning)
    return findings


def _memory_to_gb(
    value: int | None,
    unit: str | None,
) -> tuple[float | None, PreflightFinding | None]:
    if value is None:
        return None, None
    normalized = (unit or "M").strip().upper()
    factors = {
        "K": 1 / (1024 * 1024),
        "KB": 1 / (1024 * 1024),
        "M": 1 / 1024,
        "MB": 1 / 1024,
        "G": 1,
        "GB": 1,
        "T": 1024,
        "TB": 1024,
    }
    factor = factors.get(normalized)
    if factor is None:
        return None, PreflightFinding(
            severity=PreflightSeverity.WARN,
            code="RESOURCE.MEMORY_UNIT_UNKNOWN",
            message=f"memory unit {unit!r} cannot be compared against QoS limits",
            source_authority="capability_profile",
        )
    return value * factor, None


def _time_limit_to_hours(value: str | None) -> tuple[float | None, PreflightFinding | None]:
    if value is None:
        return None, None
    text = value.strip()
    if not text:
        return None, None
    days = 0
    if "-" in text:
        day_text, text = text.split("-", 1)
        try:
            days = int(day_text)
        except ValueError:
            return None, _time_parse_warning(value)
    parts = text.split(":")
    if len(parts) == 3:
        hour_text, minute_text, second_text = parts
    elif len(parts) == 2:
        hour_text = "0"
        minute_text, second_text = parts
    elif len(parts) == 1 and re.fullmatch(r"\d+", parts[0]):
        hour_text = "0"
        minute_text = parts[0]
        second_text = "0"
    else:
        return None, _time_parse_warning(value)
    try:
        hours = days * 24 + int(hour_text) + int(minute_text) / 60 + int(second_text) / 3600
    except ValueError:
        return None, _time_parse_warning(value)
    return hours, None


def _time_parse_warning(value: str) -> PreflightFinding:
    return PreflightFinding(
        severity=PreflightSeverity.WARN,
        code="RESOURCE.TIME_LIMIT_PARSE_UNKNOWN",
        message=f"time_limit {value!r} cannot be compared against QoS limits",
        source_authority="capability_profile",
    )
