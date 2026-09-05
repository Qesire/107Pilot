"""SlurmrestSnapshotCollector: query slurmrestd REST, build PlatformSnapshot."""

from __future__ import annotations

import pytest

from pilot107.adapters.slurm import HttpResponse
from pilot107.adapters.slurmrest_snapshot import (
    SlurmrestSnapshotCollector,
    _node_state_from_slurm,
    _partition_from_slurm,
)
from pilot107.core.platform_snapshot import (
    ObservationSourceType,
    PlatformSnapshotScope,
)


class FakeHttpTransport:
    """Records requests, returns canned payloads."""

    def __init__(self, *, partitions_payload: dict, nodes_payload: dict) -> None:
        self._payloads = {
            "/slurm/v0.0.41/partitions": partitions_payload,
            "/slurm/v0.0.41/nodes": nodes_payload,
        }
        self.calls: list[tuple[str, str]] = []

    def request(self, method, path, *, token=None, payload=None) -> HttpResponse:
        self.calls.append((method, path))
        if path not in self._payloads:
            return HttpResponse(status=404, payload={"error": "not found"})
        return HttpResponse(status=200, payload=self._payloads[path])


@pytest.fixture()
def captured_at() -> str:
    return "2026-07-18T15:03:42+00:00"


@pytest.fixture()
def partitions_payload() -> dict:
    return {
        "partitions": [
            {
                "name": "CPU-RC",
                "state": "UP",
                "nodes": {"anode16": ["idle"]},
                "qos": {"allowed": "qos_cpu_rc"},
                "total_cpus": 4,
                "total_nodes": 1,
            }
        ]
    }


@pytest.fixture()
def nodes_payload() -> dict:
    return {
        "nodes": [
            {
                "name": "anode16",
                "state": ["IDLE"],
                "cpus": 4,
                "real_memory": 6144,
                "partitions": ["CPU-RC"],
            }
        ]
    }


def test_collect_queries_partitions_and_nodes(partitions_payload, nodes_payload, captured_at):
    transport = FakeHttpTransport(
        partitions_payload=partitions_payload, nodes_payload=nodes_payload
    )
    collector = SlurmrestSnapshotCollector(transport=transport, api_version="v0.0.41")
    snapshot = collector.collect(captured_at=captured_at)
    assert ("GET", "/slurm/v0.0.41/partitions") in transport.calls
    assert ("GET", "/slurm/v0.0.41/nodes") in transport.calls
    assert snapshot.scope == PlatformSnapshotScope.SIMULATOR
    assert len(snapshot.partitions) == 1
    assert snapshot.partitions[0].name == "CPU-RC"
    assert snapshot.partitions[0].state_raw == "UP"


def test_collect_builds_node_snapshots(partitions_payload, nodes_payload, captured_at):
    transport = FakeHttpTransport(
        partitions_payload=partitions_payload, nodes_payload=nodes_payload
    )
    collector = SlurmrestSnapshotCollector(transport=transport)
    snapshot = collector.collect(captured_at=captured_at)
    assert len(snapshot.nodes) == 1
    assert snapshot.nodes[0].node_name == "anode16"
    assert snapshot.nodes[0].cpus_total == 4


def test_collect_records_limitations_on_partial_failure(captured_at):
    transport = FakeHttpTransport(
        partitions_payload={"partitions": []}, nodes_payload={"nodes": []}
    )
    collector = SlurmrestSnapshotCollector(transport=transport)
    snapshot = collector.collect(captured_at=captured_at)
    assert "no partitions returned" in snapshot.limitations
    assert "no nodes returned" in snapshot.limitations


def test_collect_marks_source_type_rest(partitions_payload, nodes_payload, captured_at):
    transport = FakeHttpTransport(
        partitions_payload=partitions_payload, nodes_payload=nodes_payload
    )
    collector = SlurmrestSnapshotCollector(transport=transport)
    snapshot = collector.collect(captured_at=captured_at)
    assert "rest" in snapshot.collector_version
    assert snapshot.partitions[0].source_type == ObservationSourceType.REST
    assert snapshot.nodes[0].source_type == ObservationSourceType.REST


def test_partition_from_slurm_parses_qos_and_state():
    raw = {
        "name": "CPU-RC",
        "state": "UP",
        "qos": {"allowed": "qos_cpu_rc,other"},
        "total_nodes": 2,
    }
    captured_at = "2026-07-18T15:03:42+00:00"
    partition = _partition_from_slurm(raw, captured_at=captured_at)
    assert partition.name == "CPU-RC"
    assert partition.state_raw == "UP"
    assert "qos_cpu_rc" in partition.allow_qos
    assert "other" in partition.allow_qos
    assert partition.total_nodes == 2


def test_node_state_from_slurm_normalizes_lowercase():
    assert _node_state_from_slurm(["IDLE"]) == ("idle",)
    assert _node_state_from_slurm(["MIXED", "COMPLETING"]) == ("mixed", "completing")


class TokenRecordingTransport(FakeHttpTransport):
    """Records tokens passed to ``request`` for assertion."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.tokens: list[str | None] = []

    def request(self, method, path, *, token=None, payload=None) -> HttpResponse:
        self.tokens.append(token)
        return super().request(method, path, token=token, payload=payload)


def test_collect_passes_token_to_transport(partitions_payload, nodes_payload, captured_at):
    """Collector forwards configured JWT to transport.request."""
    transport = TokenRecordingTransport(
        partitions_payload=partitions_payload, nodes_payload=nodes_payload
    )
    collector = SlurmrestSnapshotCollector(transport=transport, token="jwt-abc-123")
    collector.collect(captured_at=captured_at)
    assert len(transport.tokens) == 2  # partitions + nodes
    assert all(t == "jwt-abc-123" for t in transport.tokens)


def test_collect_passes_none_token_by_default(partitions_payload, nodes_payload, captured_at):
    """When no token configured, requests send token=None (current behavior)."""
    transport = TokenRecordingTransport(
        partitions_payload=partitions_payload, nodes_payload=nodes_payload
    )
    collector = SlurmrestSnapshotCollector(transport=transport)
    collector.collect(captured_at=captured_at)
    assert all(t is None for t in transport.tokens)
