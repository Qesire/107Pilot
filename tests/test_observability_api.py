from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pilot107.api.http_app import build_api
from pilot107.api.observability_routes import ResourceObservationRoutes
from pilot107.core.identity import UserIdentity
from pilot107.observability.evaluator import ResourceEvaluator
from pilot107.observability.model import (
    AccountPulse,
    ObservationCycle,
    ObservedMeasure,
    PlatformPulse,
    ResourceMeasureSet,
    RunResourceSample,
    RunResourceSummary,
)
from pilot107.observability.service import ObservabilityService
from pilot107.observability.store import SQLiteObservabilityStore


def _measure(value: float | int | str, *, unit: str) -> ObservedMeasure:
    return ObservedMeasure(
        value=value,
        unit=unit,
        availability="available",
        source_adapter="test_source",
        source_operation="test_read",
        captured_at="2026-08-19T00:00:00Z",
        quality="verified",
        coverage=1.0,
        warning=None,
    )


def _cycle(cycle_id: str, lane: str) -> ObservationCycle:
    return ObservationCycle(
        cycle_id=cycle_id,
        connection_id="connection1",
        lane=lane,
        fencing_token=1,
        scheduled_at="2026-08-19T00:00:00Z",
        started_at="2026-08-19T00:00:00Z",
        completed_at="2026-08-19T00:00:00Z",
        command_count=1,
        status="complete",
    )


def _routes(tmp_path: Path) -> ResourceObservationRoutes:
    store = SQLiteObservabilityStore(tmp_path / "observability.db")
    store.save_cycle(_cycle("cycle-capability", "capability"))
    store.save_platform_pulse(
        PlatformPulse(
            observation_id="platform-capability",
            connection_id="connection1",
            owner=None,
            run_id=None,
            attempt=None,
            cycle_id="cycle-capability",
            captured_at="2026-08-19T00:00:00Z",
            freshness="fresh",
            partial=False,
            warnings=(),
            fencing_token=1,
            measures=ResourceMeasureSet(
                extras=(("slurm_version", _measure("25.11.2", unit="version")),)
            ),
        )
    )
    store.save_cycle(_cycle("cycle-platform", "platform_account"))
    store.save_platform_pulse(
        PlatformPulse(
            observation_id="platform-pulse",
            connection_id="connection1",
            owner=None,
            run_id=None,
            attempt=None,
            cycle_id="cycle-platform",
            captured_at="2026-08-19T00:00:00Z",
            freshness="fresh",
            partial=False,
            warnings=("bounded aggregate",),
            fencing_token=1,
            measures=ResourceMeasureSet(
                extras=(("partitions_idle", _measure(2, unit="partitions")),)
            ),
        )
    )
    store.save_account_pulse(
        AccountPulse(
            observation_id="account-pulse",
            connection_id="connection1",
            owner="alice",
            run_id=None,
            attempt=None,
            cycle_id="cycle-platform",
            captured_at="2026-08-19T00:00:00Z",
            freshness="fresh",
            partial=False,
            warnings=(),
            fencing_token=1,
            measures=ResourceMeasureSet(
                extras=(("jobs_running", _measure(1, unit="jobs")),)
            ),
        )
    )
    store.save_run_sample(
        RunResourceSample(
            observation_id="sample-run1",
            connection_id="connection1",
            owner="alice",
            run_id="run1",
            attempt=0,
            cycle_id="cycle-platform",
            captured_at="2026-08-19T00:00:00Z",
            freshness="fresh",
            partial=False,
            warnings=(),
            fencing_token=1,
            measures=ResourceMeasureSet(max_rss=_measure(1024, unit="bytes")),
        )
    )
    store.save_summary(
        RunResourceSummary(
            observation_id="summary-run1",
            connection_id="connection1",
            owner="alice",
            run_id="run1",
            attempt=0,
            cycle_id="cycle-platform",
            captured_at="2026-08-19T00:00:00Z",
            freshness="terminal",
            partial=False,
            warnings=(),
            fencing_token=1,
            used=ResourceMeasureSet(
                elapsed=_measure(600, unit="seconds"),
                total_cpu=_measure(120, unit="seconds"),
                cpu_time_raw=_measure(2400, unit="cpu_seconds"),
            ),
            allocated=ResourceMeasureSet(
                allocated_cpus=_measure(4, unit="cpu"),
            ),
        )
    )
    service = ObservabilityService(
        store=store,
        evaluator=ResourceEvaluator(),
        clock=lambda: datetime(2026, 8, 19, 0, 2, tzinfo=UTC),
    )
    return ResourceObservationRoutes(service)


def test_latest_routes_compute_freshness_and_preserve_measure_provenance(
    tmp_path: Path,
) -> None:
    routes = _routes(tmp_path)
    identity = UserIdentity(username="alice")

    capability = routes.handle_get(
        ["observability", "connections", "connection1", "capabilities", "latest"],
        params={},
        identity=identity,
    )
    platform = routes.handle_get(
        ["observability", "connections", "connection1", "platform", "latest"],
        params={},
        identity=identity,
    )
    account = routes.handle_get(
        ["observability", "connections", "connection1", "account", "latest"],
        params={},
        identity=identity,
    )

    assert capability is not None and capability.status == 200
    assert capability.payload["freshness"] == "fresh"
    assert capability.payload["measures"]["slurm_version"]["value"] == "25.11.2"
    assert platform is not None and platform.payload["freshness"] == "stale"
    assert platform.payload["warnings"] == ["bounded aggregate"]
    assert account is not None and account.payload["owner"] == "alice"
    assert account.payload["measures"]["jobs_running"]["source_adapter"] == "test_source"


def test_run_resources_and_evaluations_are_owner_scoped(tmp_path: Path) -> None:
    routes = _routes(tmp_path)

    response = routes.handle_get(
        ["runs", "run1", "resources"],
        params={},
        identity=UserIdentity(username="alice"),
    )
    hidden = routes.handle_get(
        ["runs", "run1", "resources"],
        params={},
        identity=UserIdentity(username="bob"),
    )
    evaluations = routes.handle_get(
        ["runs", "run1", "resource-evaluations"],
        params={},
        identity=UserIdentity(username="alice"),
    )

    assert response is not None and response.status == 200
    assert response.payload["kind"] == "run_resource_summary"
    assert response.payload["freshness"] == "terminal"
    assert hidden is not None and hidden.status == 404
    assert evaluations is not None and evaluations.status == 200
    assert evaluations.payload["items"][0]["rule_id"] == "CPU_UNDERUTILIZED"


def test_run_series_is_bounded_and_cross_owner_is_masked(tmp_path: Path) -> None:
    routes = _routes(tmp_path)

    response = routes.handle_get(
        ["runs", "run1", "resources", "series"],
        params={"step": ["raw"], "limit": ["10"]},
        identity=UserIdentity(username="alice"),
    )
    hidden = routes.handle_get(
        ["runs", "run1", "resources", "series"],
        params={"step": ["raw"], "limit": ["10"]},
        identity=UserIdentity(username="bob"),
    )

    assert response is not None and response.status == 200
    assert len(response.payload["items"]) == 1
    assert hidden is not None and hidden.status == 404


def test_main_http_api_dispatches_persisted_observability_without_cluster_reads(
    tmp_path: Path,
) -> None:
    _routes(tmp_path)
    api = build_api(
        db_path=tmp_path / "observability.db",
        evidence_root=tmp_path / "evidence",
    )

    response = api.handle_get(
        "/api/v1/runs/run1/resources",
        headers={"X-Pilot107-User": "alice"},
    )

    assert response.status == 200
    assert response.payload["observation_id"] == "summary-run1"


def test_observability_routes_reject_unsafe_resource_identifiers(tmp_path: Path) -> None:
    routes = _routes(tmp_path)

    response = routes.handle_get(
        ["runs", "..", "resources"],
        params={},
        identity=UserIdentity(username="alice"),
    )

    assert response is not None
    assert response.status == 400
    assert response.payload["error"]["code"] == "OBSERVABILITY.INVALID_REQUEST"
