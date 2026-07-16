"""Parsers for read-only Slurm CLI platform facts."""

from __future__ import annotations

import re

from pilot107.core.platform_snapshot import (
    JobDetailSnapshot,
    NodeSnapshot,
    NormalizedNodeState,
    PartitionSnapshot,
    SqueueJobSnapshot,
)

_KEY_VALUE_RE = re.compile(r"([A-Za-z][A-Za-z0-9_]+)=([^ \n]+)")


def normalize_slurm_node_state(value: str | None) -> NormalizedNodeState:
    if not value:
        return NormalizedNodeState.UNKNOWN
    normalized = value.lower().replace("*", "")
    parts = {part for part in re.split(r"[^a-z0-9]+", normalized) if part}
    if parts & {"down", "fail", "failed", "not_responding"}:
        return NormalizedNodeState.DOWN
    if parts & {"drain", "drng", "draining", "drained"}:
        return NormalizedNodeState.DRAINING
    if parts & {"mix", "mixed"}:
        return NormalizedNodeState.MIXED
    if parts & {"alloc", "allocated"}:
        return NormalizedNodeState.ALLOCATED
    if parts & {"comp", "completing", "cg"}:
        return NormalizedNodeState.COMPLETING
    if parts & {"idle"}:
        return NormalizedNodeState.IDLE
    return NormalizedNodeState.UNKNOWN


def parse_scontrol_show_part(
    text: str,
    *,
    source_name: str = "scontrol show part",
    raw_artifact: str | None = None,
    captured_at: str | None = None,
) -> tuple[PartitionSnapshot, ...]:
    partitions: list[PartitionSnapshot] = []
    current: dict[str, str] = {}

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("PartitionName=") and current:
            partitions.append(
                _partition_from_fields(current, source_name, raw_artifact, captured_at)
            )
            current = {}
        current.update(_parse_key_values(stripped))

    if current:
        partitions.append(_partition_from_fields(current, source_name, raw_artifact, captured_at))
    return tuple(partition for partition in partitions if partition.name)


def parse_scontrol_show_nodes(
    text: str,
    *,
    source_name: str = "scontrol show nodes",
    raw_artifact: str | None = None,
    captured_at: str | None = None,
) -> tuple[NodeSnapshot, ...]:
    nodes: list[NodeSnapshot] = []
    current: dict[str, str] = {}

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("NodeName=") and current:
            nodes.append(_node_from_fields(current, source_name, raw_artifact, captured_at))
            current = {}
        current.update(_parse_key_values(stripped))

    if current:
        nodes.append(_node_from_fields(current, source_name, raw_artifact, captured_at))
    return tuple(node for node in nodes if node.node_name)


def parse_sinfo_pipe(
    text: str,
    *,
    source_name: str = "sinfo",
    captured_at: str | None = None,
) -> tuple[NodeSnapshot, ...]:
    """Parse ``sinfo -h -o '%N|%P|%t|%c|%m|%G|%E'``-style output."""

    nodes: list[NodeSnapshot] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        node_name = parts[0].strip()
        partitions = _split_csv(parts[1].replace("*", "")) if len(parts) > 1 else ()
        state = parts[2].strip() if len(parts) > 2 else None
        cpus = _int_or_none(parts[3]) if len(parts) > 3 else None
        memory = _int_or_none(parts[4]) if len(parts) > 4 else None
        gres = _parse_gres(parts[5]) if len(parts) > 5 else {}
        reason = _none_if_empty(parts[6]) if len(parts) > 6 else None
        nodes.append(
            NodeSnapshot(
                node_name=node_name,
                partitions=partitions,
                state_raw=state,
                state_normalized=normalize_slurm_node_state(state),
                cpus_total=cpus,
                memory_mb=memory,
                gres=gres,
                reason=reason,
                captured_at=captured_at,
                source_name=source_name,
            )
        )
    return tuple(nodes)


def parse_squeue_pipe(
    text: str,
    *,
    source_name: str = "squeue",
    raw_artifact: str | None = None,
    captured_at: str | None = None,
) -> tuple[SqueueJobSnapshot, ...]:
    """Parse ``squeue -h -o '%i|%T|%R|%P|%j|<TRES>'``-style output."""

    jobs: list[SqueueJobSnapshot] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 2:
            continue
        jobs.append(
            SqueueJobSnapshot(
                job_id=parts[0].strip(),
                state_raw=parts[1].strip(),
                pending_reason=_none_if_empty(parts[2]) if len(parts) > 2 else None,
                partition=_none_if_empty(parts[3]) if len(parts) > 3 else None,
                name=_none_if_empty(parts[4]) if len(parts) > 4 else None,
                tres=_parse_tres(parts[5]) if len(parts) > 5 else {},
                captured_at=captured_at,
                source_name=source_name,
                raw_artifact=raw_artifact,
            )
        )
    return tuple(jobs)


def parse_scontrol_show_job(
    text: str,
    *,
    source_name: str = "scontrol show job",
    raw_artifact: str | None = None,
    captured_at: str | None = None,
) -> tuple[JobDetailSnapshot, ...]:
    jobs: list[JobDetailSnapshot] = []
    current: dict[str, str] = {}

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("JobId=") and current:
            jobs.append(_job_detail_from_fields(current, source_name, raw_artifact, captured_at))
            current = {}
        current.update(_parse_key_values(stripped))

    if current:
        jobs.append(_job_detail_from_fields(current, source_name, raw_artifact, captured_at))
    return tuple(job for job in jobs if job.job_id)


def _parse_key_values(line: str) -> dict[str, str]:
    return {match.group(1): match.group(2) for match in _KEY_VALUE_RE.finditer(line)}


def _partition_from_fields(
    fields: dict[str, str],
    source_name: str,
    raw_artifact: str | None,
    captured_at: str | None,
) -> PartitionSnapshot:
    state = fields.get("State")
    return PartitionSnapshot(
        name=fields.get("PartitionName", ""),
        allow_accounts=_split_csv(fields.get("AllowAccounts")),
        allow_qos=_split_csv(fields.get("AllowQos")),
        nodes=_none_if_empty(fields.get("Nodes")),
        state_raw=state,
        state_normalized=normalize_slurm_node_state(state),
        max_time=_none_if_empty(fields.get("MaxTime")),
        default=_bool_or_none(fields.get("Default")),
        tres=_parse_tres(fields.get("TRES")),
        total_cpus=_int_or_none(fields.get("TotalCPUs")),
        total_nodes=_int_or_none(fields.get("TotalNodes")),
        captured_at=captured_at,
        source_name=source_name,
        raw_artifact=raw_artifact,
    )


def _node_from_fields(
    fields: dict[str, str],
    source_name: str,
    raw_artifact: str | None,
    captured_at: str | None,
) -> NodeSnapshot:
    state = fields.get("State")
    return NodeSnapshot(
        node_name=fields.get("NodeName", ""),
        partitions=_split_csv(fields.get("Partitions")),
        state_raw=state,
        state_normalized=normalize_slurm_node_state(state),
        cpus_total=_int_or_none(fields.get("CPUTot") or fields.get("CPUs")),
        cpus_allocated=_int_or_none(fields.get("CPUAlloc")),
        memory_mb=_int_or_none(fields.get("RealMemory")),
        gres=_parse_gres(fields.get("Gres")),
        reason=_none_if_empty(fields.get("Reason")),
        captured_at=captured_at,
        source_name=source_name,
        raw_artifact=raw_artifact,
    )


def _job_detail_from_fields(
    fields: dict[str, str],
    source_name: str,
    raw_artifact: str | None,
    captured_at: str | None,
) -> JobDetailSnapshot:
    return JobDetailSnapshot(
        job_id=fields.get("JobId", ""),
        state_raw=_none_if_empty(fields.get("JobState") or fields.get("State")),
        reason=_none_if_empty(fields.get("Reason")),
        partition=_none_if_empty(fields.get("Partition")),
        qos=_none_if_empty(fields.get("QOS")),
        req_tres=_parse_tres(fields.get("ReqTRES") or fields.get("TRES")),
        alloc_tres=_parse_tres(fields.get("AllocTRES")),
        captured_at=captured_at,
        source_name=source_name,
        raw_artifact=raw_artifact,
    )


def _split_csv(value: str | None) -> tuple[str, ...]:
    if not value or value in {"(null)", "N/A", "ALL"}:
        return () if value != "ALL" else ("ALL",)
    return tuple(item.strip() for item in value.replace(" ", ",").split(",") if item.strip())


def _parse_tres(value: str | None) -> dict[str, str]:
    if not value or value in {"(null)", "N/A"}:
        return {}
    result: dict[str, str] = {}
    for item in value.split(","):
        key, sep, raw = item.partition("=")
        if sep and key:
            result[key] = raw
    return result


def _parse_gres(value: str | None) -> dict[str, str]:
    if not value or value in {"(null)", "N/A"}:
        return {}
    result: dict[str, str] = {}
    for index, item in enumerate(value.split(",")):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) >= 3:
            key = ":".join(parts[:2])
            result[key] = parts[2]
        else:
            result[f"gres_{index}"] = item
    return result


def _bool_or_none(value: str | None) -> bool | None:
    if value is None:
        return None
    if value.upper() == "YES":
        return True
    if value.upper() == "NO":
        return False
    return None


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _none_if_empty(value: str | None) -> str | None:
    if value is None or value in {"", "(null)", "N/A"}:
        return None
    return value
