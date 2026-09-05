"""Platform profile snapshots used to gate simulator and real-cluster compatibility."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from pilot107.adapters.slurm import HttpTransport, SlurmTransportError
from pilot107.core.resources import QosResourceLimit


class SourceAuthority(StrEnum):
    STATIC_COMPETITION_PROFILE = "static_competition_profile"
    SIMULATOR_PROBE = "simulator_probe"
    REAL_CLUSTER_PROBE = "real_cluster_probe"


@dataclass(frozen=True)
class EndpointSet:
    slurm_rest_url: str
    command_gateway_url: str | None = None
    evidence_transport_url: str | None = None


@dataclass(frozen=True)
class ClusterProfile:
    name: str
    slurm_version: str
    api_version: str
    shared_roots: tuple[str, ...]
    local_roots: tuple[str, ...]
    partitions: tuple[str, ...]
    qos: tuple[str, ...]
    source_authority: SourceAuthority


@dataclass(frozen=True)
class UserEntitlementProfile:
    username: str
    home: str
    allowed_roots: tuple[str, ...]
    default_partition: str
    default_qos: str | None
    source_authority: SourceAuthority


@dataclass(frozen=True)
class ConfigurationSnapshot:
    cluster: ClusterProfile
    users: tuple[UserEntitlementProfile, ...]
    endpoints: EndpointSet
    auth_strategy: str
    openapi_digest: str | None = None
    captured_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    freshness_seconds: int = 300

    def to_payload(self) -> dict[str, Any]:
        return {
            "cluster": {
                "name": self.cluster.name,
                "slurm_version": self.cluster.slurm_version,
                "api_version": self.cluster.api_version,
                "shared_roots": list(self.cluster.shared_roots),
                "local_roots": list(self.cluster.local_roots),
                "partitions": list(self.cluster.partitions),
                "qos": list(self.cluster.qos),
                "source_authority": self.cluster.source_authority.value,
            },
            "users": [
                {
                    "username": user.username,
                    "home": user.home,
                    "allowed_roots": list(user.allowed_roots),
                    "default_partition": user.default_partition,
                    "default_qos": user.default_qos,
                    "source_authority": user.source_authority.value,
                }
                for user in self.users
            ],
            "endpoints": {
                "slurm_rest_url": self.endpoints.slurm_rest_url,
                "command_gateway_url": self.endpoints.command_gateway_url,
                "evidence_transport_url": self.endpoints.evidence_transport_url,
            },
            "auth_strategy": self.auth_strategy,
            "openapi_digest": self.openapi_digest,
            "captured_at": self.captured_at,
            "freshness_seconds": self.freshness_seconds,
        }


@dataclass(frozen=True)
class PartitionCapability:
    name: str
    nodes: str | None
    total_nodes: int | None
    allow_qos: tuple[str, ...]
    state: tuple[str, ...] = ()
    gpu_types: tuple[str, ...] = ()
    source_authority: SourceAuthority = SourceAuthority.STATIC_COMPETITION_PROFILE

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "nodes": self.nodes,
            "total_nodes": self.total_nodes,
            "allow_qos": list(self.allow_qos),
            "state": list(self.state),
            "gpu_types": list(self.gpu_types),
            "source_authority": self.source_authority.value,
        }


@dataclass(frozen=True)
class QosCapability:
    name: str
    max_cpus: int | None = None
    max_gpus: int | None = None
    max_memory_gb: int | None = None
    max_wall_hours: int | None = None
    source_authority: str = "docs-main"
    notes: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "max_cpus": self.max_cpus,
            "max_gpus": self.max_gpus,
            "max_memory_gb": self.max_memory_gb,
            "max_wall_hours": self.max_wall_hours,
            "source_authority": self.source_authority,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class RestCapability:
    base_url: str
    api_version: str
    auth_strategy: str
    openapi_digest: str | None = None
    supports_query: bool = True
    supports_submit: bool = False
    supports_cancel: bool = False
    supports_accounting: bool = False
    partial_payload_with_errors: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "api_version": self.api_version,
            "auth_strategy": self.auth_strategy,
            "openapi_digest": self.openapi_digest,
            "supports_query": self.supports_query,
            "supports_submit": self.supports_submit,
            "supports_cancel": self.supports_cancel,
            "supports_accounting": self.supports_accounting,
            "partial_payload_with_errors": self.partial_payload_with_errors,
        }


@dataclass(frozen=True)
class CapabilityProfile:
    profile_id: str
    source_authority: str
    captured_at: str
    freshness_seconds: int
    shared_roots: tuple[str, ...]
    local_roots: tuple[str, ...]
    default_partition: str
    default_qos: str | None
    partitions: tuple[PartitionCapability, ...]
    qos: tuple[QosCapability, ...]
    rest: RestCapability
    dynamic_facts: tuple[str, ...]
    limitations: tuple[str, ...]

    def partition_qos(self) -> dict[str, tuple[str, ...]]:
        return {partition.name: partition.allow_qos for partition in self.partitions}

    def qos_limits(self) -> dict[str, QosResourceLimit]:
        return {
            qos.name: QosResourceLimit(
                max_cpus=qos.max_cpus,
                max_gpus=qos.max_gpus,
                max_memory_gb=qos.max_memory_gb,
                max_wall_hours=qos.max_wall_hours,
                source_authority=qos.source_authority,
            )
            for qos in self.qos
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "source_authority": self.source_authority,
            "captured_at": self.captured_at,
            "freshness_seconds": self.freshness_seconds,
            "shared_roots": list(self.shared_roots),
            "local_roots": list(self.local_roots),
            "default_partition": self.default_partition,
            "default_qos": self.default_qos,
            "partitions": [partition.to_payload() for partition in self.partitions],
            "qos": [qos.to_payload() for qos in self.qos],
            "rest": self.rest.to_payload(),
            "dynamic_facts": list(self.dynamic_facts),
            "limitations": list(self.limitations),
        }


def docker_sim_capability_profile(
    *,
    captured_at: str | None = None,
    slurm_rest_url: str = "http://slurmrestd:6820",
) -> CapabilityProfile:
    """Load the simulator's declared behavior rather than duplicating it in Python.

    ``simulator-real107-behavior.yaml`` is also consumed by the profile-apply
    and smoke scripts.  Keeping this adapter on the same source prevents an
    application-level capability drift when the scheduler fixture changes.
    """

    profile = load_capability_profile(_simulator_behavior_profile_path())
    return replace(
        profile,
        captured_at=captured_at or datetime.now(UTC).isoformat(),
        rest=replace(profile.rest, base_url=slurm_rest_url),
    )


def docker_sim_configuration_snapshot(
    *,
    slurm_rest_url: str = "http://pilot107-slurmrestd-sim:6820",
    command_gateway_url: str = "http://pilot107-command-gateway:8090",
    evidence_transport_url: str | None = None,
    captured_at: str | None = None,
) -> ConfigurationSnapshot:
    profile = docker_sim_capability_profile(
        captured_at=captured_at,
        slurm_rest_url=slurm_rest_url,
    )
    return ConfigurationSnapshot(
        cluster=ClusterProfile(
            name="docker-slurm-sim",
            slurm_version=_simulator_slurm_version(),
            api_version=profile.rest.api_version,
            shared_roots=profile.shared_roots,
            local_roots=profile.local_roots,
            partitions=tuple(partition.name for partition in profile.partitions),
            qos=tuple(qos.name for qos in profile.qos),
            source_authority=SourceAuthority.SIMULATOR_PROBE,
        ),
        users=_simulator_user_profiles(),
        endpoints=EndpointSet(
            slurm_rest_url=slurm_rest_url,
            command_gateway_url=command_gateway_url,
            evidence_transport_url=evidence_transport_url,
        ),
        auth_strategy=profile.rest.auth_strategy,
        captured_at=profile.captured_at,
        freshness_seconds=profile.freshness_seconds,
    )


def capability_profile_from_real107_probe(
    *,
    configuration_snapshot: dict[str, Any],
    probe_report: dict[str, Any],
) -> CapabilityProfile:
    cluster = _as_dict(configuration_snapshot.get("cluster"))
    endpoints = _as_dict(configuration_snapshot.get("endpoints"))
    users = configuration_snapshot.get("users")
    first_user = _as_dict(users[0]) if isinstance(users, list) and users else {}
    partitions = _partitions_from_probe_report(probe_report)
    if not partitions:
        partitions = _partitions_from_snapshot(cluster)
    default_partition = str(first_user.get("default_partition") or "")
    if not default_partition and partitions:
        default_partition = partitions[0].name
    return CapabilityProfile(
        profile_id="real107-probe",
        source_authority="real_cluster_probe+docs-main",
        captured_at=str(
            configuration_snapshot.get("captured_at") or probe_report.get("observed_at")
        ),
        freshness_seconds=int(configuration_snapshot.get("freshness_seconds") or 300),
        shared_roots=tuple(str(item) for item in cluster.get("shared_roots", [])),
        local_roots=tuple(str(item) for item in cluster.get("local_roots", [])),
        default_partition=default_partition,
        default_qos=(
            None if first_user.get("default_qos") is None else str(first_user.get("default_qos"))
        ),
        partitions=partitions,
        qos=_docs_main_qos_capabilities(),
        rest=RestCapability(
            base_url=str(endpoints.get("slurm_rest_url") or ""),
            api_version=str(cluster.get("api_version") or "v0.0.41"),
            auth_strategy=str(
                configuration_snapshot.get("auth_strategy") or "single_user_jwt_bearer"
            ),
            openapi_digest=configuration_snapshot.get("openapi_digest"),
            supports_query=True,
            supports_submit=False,
            supports_cancel=False,
            supports_accounting=False,
            partial_payload_with_errors=_probe_had_partial_partition_payload(probe_report),
        ),
        dynamic_facts=(
            "real107 probe is a point-in-time M1-R observation, not a permanent platform contract",
            "docs-main says visible QOS and quotas vary by account authorization and platform page",
        ),
        limitations=(
            "real107 submit/cancel/file read were not probed",
            "configuration_snapshot.cluster.qos may be empty even when partition AllowQos is known",
            "SlurmDBD/TRES query failed during /partitions probe, "
            "but partition payload was present",
        ),
    )


def load_capability_profile(path: Path) -> CapabilityProfile:
    if path.is_dir():
        return capability_profile_from_real107_probe(
            configuration_snapshot=_read_json(path / "configuration_snapshot.json"),
            probe_report=_read_json(path / "probe_report.json"),
        )
    payload = _read_profile_document(path)
    if payload.get("schema") == "pilot107.capability_profile.v1":
        return _capability_profile_from_payload(payload)
    if payload.get("schema") == "pilot107.simulator_real107_behavior.v1":
        return capability_profile_from_simulator_behavior(payload)
    if "configuration_snapshot" in payload and "probe_report" in payload:
        return capability_profile_from_real107_probe(
            configuration_snapshot=_as_dict(payload["configuration_snapshot"]),
            probe_report=_as_dict(payload["probe_report"]),
        )
    raise ValueError(f"unsupported capability profile source: {path}")


def capability_profile_from_simulator_behavior(payload: dict[str, Any]) -> CapabilityProfile:
    """Translate the simulator's declarative behavior file into API policy.

    This profile describes the Docker fixture, not a permanent claim about
    the 107Pilot cluster.  The real-cluster probe and per-user entitlement
    snapshots remain authoritative for a live deployment.
    """

    users = [_as_dict(item) for item in payload.get("users", [])]
    default_user = next(
        (item for item in users if item.get("default_partition")),
        {},
    )
    partitions_payload = [_as_dict(item) for item in payload.get("partitions", [])]
    partitions = tuple(
        PartitionCapability(
            name=str(item["name"]),
            nodes=None if item.get("nodes") is None else str(item["nodes"]),
            total_nodes=_simulator_partition_node_count(item, payload),
            allow_qos=tuple(str(value) for value in item.get("allow_qos", [])),
            state=("UP",),
            gpu_types=_gpu_types_from_partition_name(str(item.get("name") or "")),
            source_authority=SourceAuthority.SIMULATOR_PROBE,
        )
        for item in partitions_payload
        if item.get("name")
    )
    qos = tuple(
        QosCapability(
            name=str(item["name"]),
            max_cpus=_optional_positive_int(item.get("max_cpus")),
            max_gpus=_optional_nonnegative_int(item.get("max_gpus")),
            max_memory_gb=_memory_gib(item.get("max_memory")),
            max_wall_hours=_wall_hours(item.get("max_wall")),
            source_authority="simulator-real107-behavior.yaml",
        )
        for item in (_as_dict(raw) for raw in payload.get("qos", []))
        if item.get("name")
    )
    storage = _as_dict(payload.get("storage"))
    shared_roots = tuple(
        str(item["path"])
        for item in (_as_dict(raw) for raw in storage.get("shared_paths", []))
        if item.get("path") and item.get("semantics")
    )
    local_roots = tuple(
        str(item["path"])
        for item in (_as_dict(raw) for raw in storage.get("local_paths", []))
        if item.get("path")
    )
    slurm = _as_dict(payload.get("slurm"))
    auth = _as_dict(slurm.get("auth"))
    return CapabilityProfile(
        profile_id=str(payload.get("profile_id") or "simulator-real107-behavior"),
        source_authority="simulator-real107-behavior.yaml",
        captured_at=datetime.now(UTC).isoformat(),
        freshness_seconds=300,
        shared_roots=shared_roots,
        local_roots=local_roots,
        default_partition=str(default_user.get("default_partition") or ""),
        default_qos=(
            None if default_user.get("default_qos") is None else str(default_user["default_qos"])
        ),
        partitions=partitions,
        qos=qos,
        rest=RestCapability(
            base_url="http://slurmrestd:6820",
            api_version=str(slurm.get("api_version") or "v0.0.41"),
            auth_strategy=str(auth.get("simulator_fallback") or "trusted_header_simulated_users"),
            supports_query=True,
            supports_submit=False,
            supports_cancel=False,
            supports_accounting=True,
            partial_payload_with_errors=True,
        ),
        dynamic_facts=(
            "simulator behavior is loaded from simulator-real107-behavior.yaml",
            "the real cluster's partitions, QoS, account authorization and GPU inventory "
            "must be refreshed from probes and user entitlements",
        ),
        limitations=tuple(str(item) for item in payload.get("runtime_limitations", [])),
    )


def _capability_profile_from_payload(payload: dict[str, Any]) -> CapabilityProfile:
    rest = _as_dict(payload.get("rest"))
    partitions = tuple(
        PartitionCapability(
            name=str(item["name"]),
            nodes=None if item.get("nodes") is None else str(item["nodes"]),
            total_nodes=(None if item.get("total_nodes") is None else int(item["total_nodes"])),
            allow_qos=tuple(str(value) for value in item.get("allow_qos", [])),
            state=tuple(str(value) for value in item.get("state", [])),
            gpu_types=tuple(str(value) for value in item.get("gpu_types", [])),
        )
        for raw in payload.get("partitions", [])
        for item in [_as_dict(raw)]
    )
    qos = tuple(
        QosCapability(
            name=str(item["name"]),
            max_cpus=None if item.get("max_cpus") is None else int(item["max_cpus"]),
            max_gpus=None if item.get("max_gpus") is None else int(item["max_gpus"]),
            max_memory_gb=(
                None if item.get("max_memory_gb") is None else int(item["max_memory_gb"])
            ),
            max_wall_hours=(
                None if item.get("max_wall_hours") is None else int(item["max_wall_hours"])
            ),
            source_authority=str(item.get("source_authority") or "release-profile"),
            notes=tuple(str(value) for value in item.get("notes", [])),
        )
        for raw in payload.get("qos", [])
        for item in [_as_dict(raw)]
    )
    if not partitions or not qos:
        raise ValueError("capability profile requires partitions and qos")
    return CapabilityProfile(
        profile_id=str(payload["profile_id"]),
        source_authority=str(payload["source_authority"]),
        captured_at=str(payload["captured_at"]),
        freshness_seconds=int(payload["freshness_seconds"]),
        shared_roots=tuple(str(value) for value in payload.get("shared_roots", [])),
        local_roots=tuple(str(value) for value in payload.get("local_roots", [])),
        default_partition=str(payload["default_partition"]),
        default_qos=None if payload.get("default_qos") is None else str(payload["default_qos"]),
        partitions=partitions,
        qos=qos,
        rest=RestCapability(
            base_url=str(rest["base_url"]),
            api_version=str(rest["api_version"]),
            auth_strategy=str(rest["auth_strategy"]),
            supports_query=bool(rest.get("supports_query", True)),
            supports_submit=bool(rest.get("supports_submit", False)),
            supports_cancel=bool(rest.get("supports_cancel", False)),
            supports_accounting=bool(rest.get("supports_accounting", False)),
            partial_payload_with_errors=bool(rest.get("partial_payload_with_errors", False)),
        ),
        dynamic_facts=tuple(str(value) for value in payload.get("dynamic_facts", [])),
        limitations=tuple(str(value) for value in payload.get("limitations", [])),
    )


def _docs_main_qos_capabilities() -> tuple[QosCapability, ...]:
    dynamic_note = "docs-main: platform page/current authorization is authoritative"
    return (
        QosCapability("qos_stu001", source_authority="real107_probe", notes=(dynamic_note,)),
        QosCapability("qos_stu_default", 4, 1, 16, 4, notes=(dynamic_note,)),
        QosCapability("qos_stu_small", 8, 1, 32, 8, notes=(dynamic_note,)),
        QosCapability("qos_stu_medium", 16, 1, 64, 24, "real107_ssh_environment", (dynamic_note,)),
        QosCapability(
            "qos_stu_medium_2gpu", 24, 2, 128, 24, "real107_ssh_environment", (dynamic_note,)
        ),
        QosCapability("qos_stu_large", 48, 4, 240, 12, "real107_ssh_environment", (dynamic_note,)),
        QosCapability("qos_stu_long", 16, 1, 64, 72, notes=(dynamic_note,)),
        QosCapability("qos_stu_cpu_long", 32, 0, 128, 72, notes=(dynamic_note,)),
        QosCapability("qos_p107-rtx5090", 16, 4, None, 96, "real107_ssh_environment"),
        QosCapability("qos_p107-a100", 16, 4, None, 96, "real107_ssh_environment"),
        QosCapability("normal", source_authority="simulator-legacy"),
    )


def _partitions_from_probe_report(probe_report: dict[str, Any]) -> tuple[PartitionCapability, ...]:
    partitions_payload: list[Any] = []
    for probe in probe_report.get("probes", []):
        probe_obj = _as_dict(probe)
        if probe_obj.get("name") == "partitions":
            summary = _as_dict(probe_obj.get("payload_summary"))
            partitions_payload = list(summary.get("partitions", []))
            break
    partitions: list[PartitionCapability] = []
    for item in partitions_payload:
        item_obj = _as_dict(item)
        name = str(item_obj.get("name") or item_obj.get("partition") or "")
        nodes = _as_dict(item_obj.get("nodes"))
        total_nodes = nodes.get("total")
        qos = _as_dict(item_obj.get("qos"))
        partition_state = _as_dict(item_obj.get("partition")).get("state", [])
        partitions.append(
            PartitionCapability(
                name=name,
                nodes=None if nodes.get("configured") is None else str(nodes.get("configured")),
                total_nodes=None if total_nodes is None else int(total_nodes),
                allow_qos=_split_qos(qos.get("allowed")),
                state=tuple(str(state) for state in partition_state)
                if isinstance(partition_state, list)
                else (str(partition_state),),
                gpu_types=_gpu_types_from_partition_name(name),
                source_authority=SourceAuthority.REAL_CLUSTER_PROBE,
            )
        )
    return tuple(partition for partition in partitions if partition.name)


def _partitions_from_snapshot(cluster: dict[str, Any]) -> tuple[PartitionCapability, ...]:
    return tuple(
        PartitionCapability(
            name=str(name),
            nodes=None,
            total_nodes=None,
            # A configuration snapshot that names a partition but does not
            # carry AllowQos has not observed its QoS policy.  Do not fill the
            # gap from the Docker simulator, or a real cluster could be
            # incorrectly authorized with fixture-only values.
            allow_qos=(),
            source_authority=SourceAuthority.REAL_CLUSTER_PROBE,
        )
        for name in cluster.get("partitions", [])
    )


def _probe_had_partial_partition_payload(probe_report: dict[str, Any]) -> bool:
    for probe in probe_report.get("probes", []):
        probe_obj = _as_dict(probe)
        if probe_obj.get("name") != "partitions":
            continue
        summary = _as_dict(probe_obj.get("payload_summary"))
        return bool(probe_obj.get("http_status", 200) >= 400 and summary.get("partitions"))
    return False


def _gpu_types_from_partition_name(name: str) -> tuple[str, ...]:
    if "A100" in name:
        return ("A100",)
    if "RTX5090" in name:
        return ("RTX5090",)
    if name == "Students":
        return ("A100", "RTX5090")
    return ()


def _split_qos(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item) for item in value if str(item))
    if not value:
        return ()
    return tuple(item.strip() for item in str(value).replace(" ", ",").split(",") if item.strip())


def _simulator_behavior_profile_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "config"
        / "platform_profiles"
        / "simulator-real107-behavior.yaml"
    )


def _simulator_behavior_document() -> dict[str, Any]:
    return _read_profile_document(_simulator_behavior_profile_path())


def _simulator_slurm_version() -> str:
    slurm = _as_dict(_simulator_behavior_document().get("slurm"))
    return str(slurm.get("target_version") or "25.11-compatible")


def _simulator_user_profiles() -> tuple[UserEntitlementProfile, ...]:
    users = [_as_dict(item) for item in _simulator_behavior_document().get("users", [])]
    return tuple(
        UserEntitlementProfile(
            username=str(item["name"]),
            home=str(item["home"]),
            allowed_roots=(str(item["home"]),),
            default_partition=str(item.get("default_partition") or ""),
            default_qos=(None if item.get("default_qos") is None else str(item.get("default_qos"))),
            source_authority=SourceAuthority.SIMULATOR_PROBE,
        )
        for item in users
        if item.get("name") and item.get("home")
    )


def _read_profile_document(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        return _as_dict(yaml.safe_load(text))
    return _as_dict(json.loads(text))


def _simulator_partition_node_count(
    partition: dict[str, Any],
    payload: dict[str, Any],
) -> int | None:
    expression = str(partition.get("nodes") or "")
    if not expression:
        return None
    nodes = [_as_dict(item) for item in payload.get("nodes", [])]
    matched = [str(node.get("name")) for node in nodes if node.get("name")]
    count = sum(_node_expression_contains(expression, name) for name in matched)
    return count or None


def _node_expression_contains(expression: str, node_name: str) -> bool:
    if node_name in {item.strip() for item in expression.split(",")}:
        return True
    match = re.fullmatch(r"(?P<prefix>.*)\[(?P<start>\d+)-(?P<end>\d+)\]", expression)
    if match is None or not node_name.startswith(match.group("prefix")):
        return False
    suffix = node_name.removeprefix(match.group("prefix"))
    if not suffix.isdigit():
        return False
    return int(match.group("start")) <= int(suffix) <= int(match.group("end"))


def _optional_positive_int(value: Any) -> int | None:
    parsed = _optional_nonnegative_int(value)
    return parsed if parsed and parsed > 0 else None


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _memory_gib(value: Any) -> int | None:
    if value is None:
        return None
    match = re.fullmatch(r"\s*(\d+)\s*([KMGTP]?)(?:i?B)?\s*", str(value), re.IGNORECASE)
    if match is None:
        return None
    amount = int(match.group(1))
    unit = match.group(2).upper()
    divisors = {"": 1024**3, "K": 1024**2, "M": 1024, "G": 1, "T": 1 / 1024}
    gib = amount / divisors[unit]
    return int(gib) if gib.is_integer() else int(gib) + 1


def _wall_hours(value: Any) -> int | None:
    if value is None:
        return None
    match = re.fullmatch(r"(?:(\d+)-)?(\d{1,2}):(\d{2}):(\d{2})", str(value))
    if match is None:
        return None
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    total_seconds = (((days * 24) + hours) * 60 + minutes) * 60 + seconds
    return (total_seconds + 3599) // 3600


def _read_json(path: Path) -> dict[str, Any]:
    return _as_dict(json.loads(path.read_text(encoding="utf-8")))


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# --------------------------------------------------------------------------- #
# OpenAPI digest auto-refresh
# --------------------------------------------------------------------------- #
#
# ``openapi_digest`` is a frozen field on ``ConfigurationSnapshot`` and
# ``RestCapability`` that captures a stable fingerprint of the slurmrestd
# OpenAPI document. The functions below re-fetch that document over an
# ``HttpTransport`` and recompute the digest so a service can refresh a stale
# snapshot without mutating the frozen dataclass (``dataclasses.replace`` is
# used to produce a new instance).
#
# Digest contract:
#   * Canonical form: ``json.dumps(payload, sort_keys=True,
#     separators=(",", ":"), ensure_ascii=False)`` — sorted keys make the
#     digest independent of the key order the server emits.
#   * Algorithm: ``sha256`` over the UTF-8 encoding of the canonical form.
#   * Returned as the full 64 hex characters (NOT truncated) so downstream
#     equality checks are unambiguous. Callers that want a short fingerprint
#     may slice ``digest[:16]`` but the stored value is full-width.
#
# Token safety: the ``token`` argument is forwarded ONLY to the transport
# request. It is never included in the canonical form, the digest, any error
# message, or any return value.


def compute_openapi_digest(openapi_payload: dict[str, Any] | bytes | str) -> str:
    """Compute the deterministic sha256 digest of an OpenAPI document.

    Accepts a parsed dict, raw JSON bytes, or a JSON string. For dict input
    the digest is computed over a canonical (sorted-keys, compact) JSON
    representation, so two documents with identical content but different key
    order produce the same digest. Returns the full 64-character hex digest.
    """
    if isinstance(openapi_payload, dict):
        canonical = json.dumps(
            openapi_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        material = canonical.encode("utf-8")
    elif isinstance(openapi_payload, bytes):
        material = openapi_payload
    elif isinstance(openapi_payload, str):
        material = openapi_payload.encode("utf-8")
    else:
        raise TypeError(
            f"openapi_payload must be dict, bytes, or str, got {type(openapi_payload).__name__}"
        )
    return hashlib.sha256(material).hexdigest()


def refresh_openapi_digest(
    transport: HttpTransport,
    api_version: str,
    token: str | None = None,
) -> str:
    """GET ``/openapi/v3`` via ``transport`` and return the recomputed digest.

    Raises ``SlurmTransportError`` on any HTTP failure or non-2xx response.
    The ``token`` is passed only to ``transport.request`` and is never placed
    in the returned digest or in any raised exception message.
    """
    try:
        response = transport.request("GET", "/openapi/v3", token=token)
    except SlurmTransportError:
        raise
    except Exception as exc:  # pragma: no cover - defensive, transport-specific
        raise SlurmTransportError(f"openapi refresh request failed: {type(exc).__name__}") from exc
    if response.status >= 400:
        raise SlurmTransportError(f"openapi refresh failed: HTTP {response.status}")
    if not isinstance(response.payload, dict) or not response.payload:
        raise SlurmTransportError("openapi refresh returned an empty or non-object body")
    return compute_openapi_digest(response.payload)


def refresh_configuration_snapshot_digest(
    snapshot: ConfigurationSnapshot,
    transport: HttpTransport,
    token: str | None = None,
) -> ConfigurationSnapshot:
    """Return a new ``ConfigurationSnapshot`` with a refreshed ``openapi_digest``.

    The snapshot is frozen; ``dataclasses.replace`` produces an updated copy.
    All other fields (cluster, users, endpoints, auth_strategy, captured_at,
    freshness_seconds) are preserved unchanged. Raises ``SlurmTransportError``
    on failure; the token never appears in any error.
    """
    digest = refresh_openapi_digest(transport, snapshot.cluster.api_version, token=token)
    return replace(snapshot, openapi_digest=digest)


def refresh_rest_capability_digest(
    capability: RestCapability,
    transport: HttpTransport,
    token: str | None = None,
) -> RestCapability:
    """Return a new ``RestCapability`` with a refreshed ``openapi_digest``.

    Mirrors ``refresh_configuration_snapshot_digest`` for the ``RestCapability``
    dataclass. All other capability flags are preserved unchanged.
    """
    digest = refresh_openapi_digest(transport, capability.api_version, token=token)
    return replace(capability, openapi_digest=digest)
