"""Collect platform snapshots from slurmrestd REST API.

Unlike the CLI collector (which runs ``scontrol``/``sinfo`` on a login node),
this collector queries slurmrestd directly over HTTP. It is suitable for the
API container, which has ``read_only: true`` and ``cap_drop: ALL`` and cannot
shell out to Slurm CLI tools, but does have network access to slurmrestd.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from pilot107.adapters.slurm import HttpResponse
from pilot107.core.platform_snapshot import (
    NodeSnapshot,
    NormalizedNodeState,
    ObservationSourceType,
    PartitionSnapshot,
    PlatformSnapshot,
    PlatformSnapshotScope,
)


class _HttpTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> HttpResponse: ...


_NODE_STATE_MAP: dict[str, NormalizedNodeState] = {
    NormalizedNodeState.IDLE.value: NormalizedNodeState.IDLE,
    NormalizedNodeState.MIXED.value: NormalizedNodeState.MIXED,
    "allocated": NormalizedNodeState.ALLOCATED,
    "alloc": NormalizedNodeState.ALLOCATED,
    NormalizedNodeState.COMPLETING.value: NormalizedNodeState.COMPLETING,
    NormalizedNodeState.DOWN.value: NormalizedNodeState.DOWN,
    NormalizedNodeState.DRAINING.value: NormalizedNodeState.DRAINING,
}


def _partition_from_slurm(raw: dict[str, Any], *, captured_at: str) -> PartitionSnapshot:
    name = str(raw["name"])
    state_raw = raw.get("state")
    if isinstance(state_raw, list):
        state_raw_str = ",".join(str(s) for s in state_raw) if state_raw else None
    elif isinstance(state_raw, str):
        state_raw_str = state_raw
    else:
        state_raw_str = None
    qos_raw = raw.get("qos", {})
    if isinstance(qos_raw, dict):
        allowed = qos_raw.get("allowed", "")
        allow_qos = tuple(q.strip() for q in str(allowed).split(",") if q.strip())
    else:
        allow_qos = ()
    total_nodes = raw.get("total_nodes")
    total_cpus = raw.get("total_cpus")
    return PartitionSnapshot(
        name=name,
        nodes=None,
        total_nodes=None if total_nodes is None else int(total_nodes),
        total_cpus=None if total_cpus is None else int(total_cpus),
        allow_qos=allow_qos,
        state_raw=state_raw_str,
        source_name="slurmrestd /partitions",
        source_type=ObservationSourceType.REST,
        captured_at=captured_at,
    )


def _node_state_from_slurm(states: list[str]) -> tuple[str, ...]:
    return tuple(str(s).lower() for s in states)


def _node_from_slurm(raw: dict[str, Any], *, captured_at: str) -> NodeSnapshot:
    state_raw = raw.get("state", [])
    if isinstance(state_raw, list):
        states_list = state_raw
    elif isinstance(state_raw, str):
        states_list = [state_raw]
    else:
        states_list = []
    normalized_states = _node_state_from_slurm(states_list)
    state_raw_str = ",".join(normalized_states) if normalized_states else None
    state_normalized = (
        _NODE_STATE_MAP.get(normalized_states[0], NormalizedNodeState.UNKNOWN)
        if normalized_states
        else NormalizedNodeState.UNKNOWN
    )
    return NodeSnapshot(
        node_name=str(raw["name"]),
        state_raw=state_raw_str,
        state_normalized=state_normalized,
        cpus_total=int(raw.get("cpus", 0) or 0),
        memory_mb=int(raw.get("real_memory", 0) or 0),
        partitions=tuple(raw.get("partitions", []) or []),
        source_name="slurmrestd /nodes",
        source_type=ObservationSourceType.REST,
        captured_at=captured_at,
    )


class SlurmrestSnapshotCollector:
    """Query slurmrestd REST and build a :class:`PlatformSnapshot`."""

    def __init__(
        self,
        *,
        transport: _HttpTransport,
        api_version: str = "v0.0.41",
        collector_version: str = "pilot107.slurmrest_snapshot.v1",
        token: str | None = None,
    ) -> None:
        self.transport = transport
        self.api_version = api_version
        self.collector_version = collector_version
        self.token = token

    def collect(self, *, captured_at: str | None = None) -> PlatformSnapshot:
        timestamp = captured_at or datetime.now(UTC).isoformat()
        limitations: list[str] = []

        part_response = self.transport.request(
            "GET", f"/slurm/{self.api_version}/partitions", token=self.token
        )
        partitions: tuple[PartitionSnapshot, ...] = ()
        if part_response.status == 200:
            raw_partitions = part_response.payload.get("partitions", []) or []
            partitions = tuple(
                _partition_from_slurm(p, captured_at=timestamp) for p in raw_partitions
            )
            if not partitions:
                limitations.append("no partitions returned")
        else:
            limitations.append(f"/partitions returned HTTP {part_response.status}")

        node_response = self.transport.request(
            "GET", f"/slurm/{self.api_version}/nodes", token=self.token
        )
        nodes: tuple[NodeSnapshot, ...] = ()
        if node_response.status == 200:
            raw_nodes = node_response.payload.get("nodes", []) or []
            nodes = tuple(_node_from_slurm(n, captured_at=timestamp) for n in raw_nodes)
            if not nodes:
                limitations.append("no nodes returned")
        else:
            limitations.append(f"/nodes returned HTTP {node_response.status}")

        return PlatformSnapshot(
            snapshot_id=f"slurmrest-{timestamp.replace(':', '').replace('+', 'Z')}",
            scope=PlatformSnapshotScope.SIMULATOR,
            captured_at=timestamp,
            collector_version=f"rest:{self.collector_version}",
            command_results=(),
            partitions=partitions,
            nodes=nodes,
            squeue_jobs=(),
            defaults=(),
            runtime_limitations=(),
            limitations=tuple(dict.fromkeys(limitations)),
            redaction_report=(),
        )
