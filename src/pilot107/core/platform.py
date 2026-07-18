"""Platform profile snapshots used to gate simulator and real-cluster compatibility."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pilot107.adapters.slurm import HttpTransport, SlurmTransportError
from pilot107.core.resources import REAL107_SIM_PARTITION_QOS, QosResourceLimit


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
    return CapabilityProfile(
        profile_id="docker-real107-sim",
        source_authority="static_competition_profile+docs-main",
        captured_at=captured_at or datetime.now(UTC).isoformat(),
        freshness_seconds=300,
        shared_roots=("/public",),
        local_roots=("/tmp", "/usr", "/var", "/opt"),
        default_partition="Students",
        default_qos="qos_stu_medium_2gpu",
        partitions=_static_partitions(SourceAuthority.STATIC_COMPETITION_PROFILE),
        qos=_docs_main_qos_capabilities(),
        rest=RestCapability(
            base_url=slurm_rest_url,
            api_version="v0.0.41",
            auth_strategy="trusted_header_simulated_users",
            supports_query=True,
            supports_submit=False,
            supports_cancel=False,
            supports_accounting=True,
            partial_payload_with_errors=True,
        ),
        dynamic_facts=(
            "docs-main marks GPU models, nodes, partition/QOS and quotas as dynamic facts",
            "docs-main default flow uses Students/qos_stu_default; competition smoke uses "
            "Students/qos_stu_medium_2gpu based on the real107 probe carrier job",
        ),
        limitations=(
            "Docker simulator is scaled down to anode16/anode17 live workers",
            "Docker simulator exposes fake GPU GRES for scheduler behavior only",
            "Runtime GPU devices, CUDA driver, NVML and GPU cgroup binding are "
            "unavailable by default",
        ),
    )


def docker_sim_configuration_snapshot(
    *,
    slurm_rest_url: str = "http://pilot107-slurmrestd-sim:6820",
    command_gateway_url: str = "http://pilot107-command-gateway:8090",
    evidence_transport_url: str | None = None,
    captured_at: str | None = None,
) -> ConfigurationSnapshot:
    source = SourceAuthority.STATIC_COMPETITION_PROFILE
    return ConfigurationSnapshot(
        cluster=ClusterProfile(
            name="docker-slurm-sim",
            slurm_version="25.11-compatible",
            api_version="v0.0.41",
            shared_roots=("/public",),
            local_roots=("/tmp", "/usr", "/var", "/opt"),
            partitions=(
                "CPU-6530",
                "CPU-8358P",
                "GPU-A100",
                "GPU-RTX5090",
                "P107-A100",
                "P107-RTX5090",
                "Students",
                "debug",
            ),
            qos=(
                "normal",
                "qos_cpu-6530",
                "qos_cpu-8358p",
                "qos_gpu-a100",
                "qos_gpu-rtx5090",
                "qos_p107-a100",
                "qos_p107-rtx5090",
                "qos_stu001",
                "qos_stu_default",
                "qos_stu_small",
                "qos_stu_medium",
                "qos_stu_medium_2gpu",
                "qos_stu_long",
                "qos_stu_cpu_long",
            ),
            source_authority=source,
        ),
        users=(
            UserEntitlementProfile(
                username="alice",
                home="/public/home/alice",
                allowed_roots=("/public/home/alice",),
                default_partition="Students",
                default_qos="qos_stu_medium_2gpu",
                source_authority=source,
            ),
            UserEntitlementProfile(
                username="bob",
                home="/public/home/bob",
                allowed_roots=("/public/home/bob",),
                default_partition="Students",
                default_qos="qos_stu_medium_2gpu",
                source_authority=source,
            ),
        ),
        endpoints=EndpointSet(
            slurm_rest_url=slurm_rest_url,
            command_gateway_url=command_gateway_url,
            evidence_transport_url=evidence_transport_url,
        ),
        auth_strategy="trusted_header_simulated_users",
        captured_at=captured_at or datetime.now(UTC).isoformat(),
        freshness_seconds=300,
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
    return CapabilityProfile(
        profile_id="real107-probe",
        source_authority="real_cluster_probe+docs-main",
        captured_at=str(
            configuration_snapshot.get("captured_at") or probe_report.get("observed_at")
        ),
        freshness_seconds=int(configuration_snapshot.get("freshness_seconds") or 300),
        shared_roots=tuple(str(item) for item in cluster.get("shared_roots", [])),
        local_roots=tuple(str(item) for item in cluster.get("local_roots", [])),
        default_partition=str(first_user.get("default_partition") or "Students"),
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
    payload = _read_json(path)
    if payload.get("schema") == "pilot107.capability_profile.v1":
        return _capability_profile_from_payload(payload)
    if "configuration_snapshot" in payload and "probe_report" in payload:
        return capability_profile_from_real107_probe(
            configuration_snapshot=_as_dict(payload["configuration_snapshot"]),
            probe_report=_as_dict(payload["probe_report"]),
        )
    raise ValueError(f"unsupported capability profile source: {path}")


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


def _static_partitions(source: SourceAuthority) -> tuple[PartitionCapability, ...]:
    gpu_by_partition = {
        "GPU-A100": ("A100",),
        "P107-A100": ("A100",),
        "Students": ("A100", "RTX5090"),
        "GPU-RTX5090": ("RTX5090",),
        "P107-RTX5090": ("RTX5090",),
    }
    nodes_by_partition = {
        "CPU-6530": "anode05",
        "CPU-8358P": "anode[16-17]",
        "GPU-RTX5090": "anode05",
        "GPU-A100": "anode[16-17]",
        "P107-RTX5090": "anode05",
        "P107-A100": "anode[16-17]",
        "Students": "anode[16-17]",
        "debug": "anode[16-17]",
    }
    return tuple(
        PartitionCapability(
            name=name,
            nodes=nodes_by_partition.get(name),
            total_nodes=2
            if name in {"CPU-8358P", "GPU-A100", "P107-A100", "Students", "debug"}
            else 1,
            allow_qos=REAL107_SIM_PARTITION_QOS[name],
            state=("UP",),
            gpu_types=gpu_by_partition.get(name, ()),
            source_authority=source,
        )
        for name in REAL107_SIM_PARTITION_QOS
    )


def _docs_main_qos_capabilities() -> tuple[QosCapability, ...]:
    dynamic_note = "docs-main: platform page/current authorization is authoritative"
    return (
        QosCapability("qos_stu001", source_authority="real107_probe", notes=(dynamic_note,)),
        QosCapability("qos_stu_default", 4, 1, 16, 4, notes=(dynamic_note,)),
        QosCapability("qos_stu_small", 8, 1, 32, 8, notes=(dynamic_note,)),
        QosCapability("qos_stu_medium", source_authority="real107_probe", notes=(dynamic_note,)),
        QosCapability("qos_stu_medium_2gpu", 24, 2, 128, 12, notes=(dynamic_note,)),
        QosCapability("qos_stu_long", 16, 1, 64, 72, notes=(dynamic_note,)),
        QosCapability("qos_stu_cpu_long", 32, 0, 128, 72, notes=(dynamic_note,)),
        QosCapability("qos_p107-rtx5090", 16, 4, None, None, "training-material"),
        QosCapability("qos_p107-a100", 16, 2, None, None, "training-material"),
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
            allow_qos=REAL107_SIM_PARTITION_QOS.get(str(name), ()),
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
