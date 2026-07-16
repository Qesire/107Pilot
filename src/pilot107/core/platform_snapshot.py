"""Source-tracked platform observation snapshots.

These models describe read-only facts collected from official docs, Slurm CLI,
Slurm REST, or the simulator. They intentionally do not execute commands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ObservedAvailability(StrEnum):
    KNOWN = "known"
    UNAVAILABLE = "unavailable"
    PERMISSION_DENIED = "permission_denied"
    UNSUPPORTED = "unsupported"
    STALE = "stale"


class ObservationSourceType(StrEnum):
    CLI = "cli"
    REST = "rest"
    OFFICIAL_DOCS = "official_docs"
    SIMULATOR = "simulator"


class PlatformSnapshotScope(StrEnum):
    LOGIN_NODE = "login_node"
    COMPUTE_JOB = "compute_job"
    SIMULATOR = "simulator"


class NormalizedNodeState(StrEnum):
    IDLE = "idle"
    MIXED = "mixed"
    ALLOCATED = "allocated"
    COMPLETING = "completing"
    DOWN = "down"
    DRAINING = "draining"
    UNKNOWN = "unknown"


class RuntimeLimitationName(StrEnum):
    GPU_RUNTIME = "gpu_runtime"
    INFINIBAND_RUNTIME = "infiniband_runtime"
    IPMI_RUNTIME = "ipmi_runtime"


@dataclass(frozen=True)
class ObservedValue[T]:
    value: T | None
    availability: ObservedAvailability
    source_type: ObservationSourceType
    source_name: str
    captured_at: str
    expires_at: str | None = None
    raw_artifact: str | None = None
    warning: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "value": self.value,
            "availability": self.availability.value,
            "source_type": self.source_type.value,
            "source_name": self.source_name,
            "captured_at": self.captured_at,
            "expires_at": self.expires_at,
            "raw_artifact": self.raw_artifact,
            "warning": self.warning,
        }


@dataclass(frozen=True)
class PlatformDefault:
    name: str
    partition: str | None = None
    qos: str | None = None
    source_type: ObservationSourceType = ObservationSourceType.OFFICIAL_DOCS
    source_name: str = "unknown"
    captured_at: str | None = None
    warning: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "partition": self.partition,
            "qos": self.qos,
            "source_type": self.source_type.value,
            "source_name": self.source_name,
            "captured_at": self.captured_at,
            "warning": self.warning,
        }


@dataclass(frozen=True)
class RuntimeLimitation:
    name: RuntimeLimitationName | str
    availability: ObservedAvailability
    source_type: ObservationSourceType
    source_name: str
    captured_at: str | None = None
    raw_artifact: str | None = None
    warning: str | None = None

    def to_payload(self) -> dict[str, object]:
        name = self.name.value if isinstance(self.name, RuntimeLimitationName) else self.name
        return {
            "name": name,
            "availability": self.availability.value,
            "source_type": self.source_type.value,
            "source_name": self.source_name,
            "captured_at": self.captured_at,
            "raw_artifact": self.raw_artifact,
            "warning": self.warning,
        }


@dataclass(frozen=True)
class PartitionSnapshot:
    name: str
    allow_accounts: tuple[str, ...] = ()
    allow_qos: tuple[str, ...] = ()
    nodes: str | None = None
    state_raw: str | None = None
    state_normalized: NormalizedNodeState = NormalizedNodeState.UNKNOWN
    max_time: str | None = None
    default: bool | None = None
    tres: dict[str, str] = field(default_factory=dict)
    total_cpus: int | None = None
    total_nodes: int | None = None
    availability: ObservedAvailability = ObservedAvailability.KNOWN
    captured_at: str | None = None
    source_type: ObservationSourceType = ObservationSourceType.CLI
    source_name: str = "unknown"
    raw_artifact: str | None = None
    warning: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "allow_accounts": list(self.allow_accounts),
            "allow_qos": list(self.allow_qos),
            "nodes": self.nodes,
            "state_raw": self.state_raw,
            "state_normalized": self.state_normalized.value,
            "max_time": self.max_time,
            "default": self.default,
            "tres": dict(self.tres),
            "total_cpus": self.total_cpus,
            "total_nodes": self.total_nodes,
            "availability": self.availability.value,
            "captured_at": self.captured_at,
            "source_type": self.source_type.value,
            "source_name": self.source_name,
            "raw_artifact": self.raw_artifact,
            "warning": self.warning,
        }


@dataclass(frozen=True)
class NodeSnapshot:
    node_name: str
    partitions: tuple[str, ...] = ()
    state_raw: str | None = None
    state_normalized: NormalizedNodeState = NormalizedNodeState.UNKNOWN
    cpus_total: int | None = None
    cpus_allocated: int | None = None
    memory_mb: int | None = None
    gres: dict[str, str] = field(default_factory=dict)
    reason: str | None = None
    availability: ObservedAvailability = ObservedAvailability.KNOWN
    captured_at: str | None = None
    source_type: ObservationSourceType = ObservationSourceType.CLI
    source_name: str = "unknown"
    raw_artifact: str | None = None
    warning: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "node_name": self.node_name,
            "partitions": list(self.partitions),
            "state_raw": self.state_raw,
            "state_normalized": self.state_normalized.value,
            "cpus_total": self.cpus_total,
            "cpus_allocated": self.cpus_allocated,
            "memory_mb": self.memory_mb,
            "gres": dict(self.gres),
            "reason": self.reason,
            "availability": self.availability.value,
            "captured_at": self.captured_at,
            "source_type": self.source_type.value,
            "source_name": self.source_name,
            "raw_artifact": self.raw_artifact,
            "warning": self.warning,
        }


@dataclass(frozen=True)
class SqueueJobSnapshot:
    job_id: str
    state_raw: str
    pending_reason: str | None = None
    partition: str | None = None
    name: str | None = None
    tres: dict[str, str] = field(default_factory=dict)
    availability: ObservedAvailability = ObservedAvailability.KNOWN
    captured_at: str | None = None
    source_type: ObservationSourceType = ObservationSourceType.CLI
    source_name: str = "unknown"
    raw_artifact: str | None = None
    warning: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "state_raw": self.state_raw,
            "pending_reason": self.pending_reason,
            "partition": self.partition,
            "name": self.name,
            "tres": dict(self.tres),
            "availability": self.availability.value,
            "captured_at": self.captured_at,
            "source_type": self.source_type.value,
            "source_name": self.source_name,
            "raw_artifact": self.raw_artifact,
            "warning": self.warning,
        }


@dataclass(frozen=True)
class JobDetailSnapshot:
    job_id: str
    state_raw: str | None = None
    reason: str | None = None
    partition: str | None = None
    qos: str | None = None
    req_tres: dict[str, str] = field(default_factory=dict)
    alloc_tres: dict[str, str] = field(default_factory=dict)
    availability: ObservedAvailability = ObservedAvailability.KNOWN
    captured_at: str | None = None
    source_type: ObservationSourceType = ObservationSourceType.CLI
    source_name: str = "unknown"
    raw_artifact: str | None = None
    warning: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "state_raw": self.state_raw,
            "reason": self.reason,
            "partition": self.partition,
            "qos": self.qos,
            "req_tres": dict(self.req_tres),
            "alloc_tres": dict(self.alloc_tres),
            "availability": self.availability.value,
            "captured_at": self.captured_at,
            "source_type": self.source_type.value,
            "source_name": self.source_name,
            "raw_artifact": self.raw_artifact,
            "warning": self.warning,
        }


@dataclass(frozen=True)
class CommandObservation:
    name: str
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    truncated: bool = False

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "argv": list(self.argv),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class PlatformSnapshot:
    snapshot_id: str
    scope: PlatformSnapshotScope
    captured_at: str
    collector_version: str
    command_results: tuple[CommandObservation, ...] = ()
    partitions: tuple[PartitionSnapshot, ...] = ()
    nodes: tuple[NodeSnapshot, ...] = ()
    squeue_jobs: tuple[SqueueJobSnapshot, ...] = ()
    job_details: tuple[JobDetailSnapshot, ...] = ()
    defaults: tuple[PlatformDefault, ...] = ()
    runtime_limitations: tuple[RuntimeLimitation, ...] = ()
    limitations: tuple[str, ...] = ()
    redaction_report: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "scope": self.scope.value,
            "captured_at": self.captured_at,
            "collector_version": self.collector_version,
            "command_results": [item.to_payload() for item in self.command_results],
            "partitions": [item.to_payload() for item in self.partitions],
            "nodes": [item.to_payload() for item in self.nodes],
            "squeue_jobs": [item.to_payload() for item in self.squeue_jobs],
            "job_details": [item.to_payload() for item in self.job_details],
            "defaults": [item.to_payload() for item in self.defaults],
            "runtime_limitations": [item.to_payload() for item in self.runtime_limitations],
            "limitations": list(self.limitations),
            "redaction_report": list(self.redaction_report),
        }
