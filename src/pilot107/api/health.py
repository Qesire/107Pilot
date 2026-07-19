"""Transport-independent API liveness and readiness checks."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pilot107.core.platform_snapshot_store import PlatformSnapshotStore
from pilot107.core.run_store import RunStore
from pilot107.core.user_entitlement_store import UserEntitlementStore


class HealthCheckStatus(StrEnum):
    OK = "ok"
    CONFIGURED = "configured"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


@dataclass(frozen=True)
class HealthCheck:
    name: str
    status: HealthCheckStatus
    required: bool
    latency_ms: float | None = None
    reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "status": self.status.value,
            "required": self.required,
        }
        if self.latency_ms is not None:
            payload["latency_ms"] = round(self.latency_ms, 3)
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


class ApiHealthService:
    def __init__(
        self,
        *,
        store: RunStore,
        evidence_root: Path,
        platform_snapshot_store: PlatformSnapshotStore | None,
        submission_enabled: bool,
        llm_enabled: bool,
        user_entitlement_store: UserEntitlementStore | None = None,
        worker_health_path: str | None = None,
    ) -> None:
        self.store = store
        self.evidence_root = evidence_root
        self.platform_snapshot_store = platform_snapshot_store
        self.submission_enabled = submission_enabled
        self.llm_enabled = llm_enabled
        self.user_entitlement_store = user_entitlement_store
        self.worker_health_path = worker_health_path

    def live_payload(self) -> dict[str, str]:
        return {"status": "alive", "service": "pilot107-api"}

    def ready(self) -> tuple[bool, dict[str, Any]]:
        checks = [self._database_check(), self._evidence_store_check()]
        checks.append(
            self._platform_snapshot_check()
            if self.platform_snapshot_store is not None
            else HealthCheck(
                name="platform_snapshot_store",
                status=HealthCheckStatus.DISABLED,
                required=False,
            )
        )
        checks.append(
            self._user_entitlement_check()
            if self.user_entitlement_store is not None
            else HealthCheck(
                name="user_entitlement_store",
                status=HealthCheckStatus.DISABLED,
                required=False,
            )
        )
        checks.extend(
            (
                _configured_check("run_submission", self.submission_enabled),
                _configured_check("local_llm", self.llm_enabled),
            )
        )
        checks.append(self._worker_heartbeat_check())
        ready = all(
            check.status == HealthCheckStatus.OK
            for check in checks
            if check.required
        )
        return ready, {
            "status": "ready" if ready else "not_ready",
            "checks": [check.to_payload() for check in checks],
        }

    def _database_check(self) -> HealthCheck:
        started = time.monotonic()
        try:
            with self.store.connect() as conn:
                conn.execute("SELECT 1").fetchone()
                row = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
                ).fetchone()
                if row is None:
                    raise sqlite3.DatabaseError("required schema is missing")
        except (OSError, sqlite3.Error):
            return HealthCheck(
                name="database",
                status=HealthCheckStatus.UNAVAILABLE,
                required=True,
                latency_ms=_elapsed_ms(started),
                reason="database_unavailable",
            )
        return HealthCheck(
            name="database",
            status=HealthCheckStatus.OK,
            required=True,
            latency_ms=_elapsed_ms(started),
        )

    def _evidence_store_check(self) -> HealthCheck:
        started = time.monotonic()
        available = (
            self.evidence_root.is_dir()
            and os.access(self.evidence_root, os.R_OK)
            and os.access(self.evidence_root, os.W_OK)
        )
        return HealthCheck(
            name="evidence_store",
            status=(HealthCheckStatus.OK if available else HealthCheckStatus.UNAVAILABLE),
            required=True,
            latency_ms=_elapsed_ms(started),
            reason=None if available else "evidence_store_unavailable",
        )

    def _platform_snapshot_check(self) -> HealthCheck:
        assert self.platform_snapshot_store is not None
        started = time.monotonic()
        try:
            with self.platform_snapshot_store.connect() as conn:
                row = conn.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'platform_snapshots'"
                ).fetchone()
                if row is None:
                    raise sqlite3.DatabaseError("platform snapshot schema is missing")
        except (OSError, sqlite3.Error):
            return HealthCheck(
                name="platform_snapshot_store",
                status=HealthCheckStatus.UNAVAILABLE,
                required=True,
                latency_ms=_elapsed_ms(started),
                reason="platform_snapshot_store_unavailable",
            )
        return HealthCheck(
            name="platform_snapshot_store",
            status=HealthCheckStatus.OK,
            required=True,
            latency_ms=_elapsed_ms(started),
        )

    def _user_entitlement_check(self) -> HealthCheck:
        assert self.user_entitlement_store is not None
        started = time.monotonic()
        try:
            with self.user_entitlement_store.connect() as conn:
                row = conn.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'user_entitlement_snapshots'"
                ).fetchone()
                if row is None:
                    raise sqlite3.DatabaseError("user entitlement schema is missing")
        except (OSError, sqlite3.Error):
            return HealthCheck(
                name="user_entitlement_store",
                status=HealthCheckStatus.UNAVAILABLE,
                required=True,
                latency_ms=_elapsed_ms(started),
                reason="user_entitlement_store_unavailable",
            )
        return HealthCheck(
            name="user_entitlement_store",
            status=HealthCheckStatus.OK,
            required=True,
            latency_ms=_elapsed_ms(started),
        )

    def _worker_heartbeat_check(self) -> HealthCheck:
        if self.worker_health_path is None:
            return HealthCheck(
                name="worker_heartbeat",
                status=HealthCheckStatus.DISABLED,
                required=False,
            )
        started = time.monotonic()
        path = Path(self.worker_health_path)
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            return HealthCheck(
                name="worker_heartbeat",
                status=HealthCheckStatus.UNAVAILABLE,
                required=True,
                latency_ms=_elapsed_ms(started),
                reason=f"worker_heartbeat_unreadable: {exc}",
            )
        if not isinstance(data, dict):
            return HealthCheck(
                name="worker_heartbeat",
                status=HealthCheckStatus.UNAVAILABLE,
                required=True,
                latency_ms=_elapsed_ms(started),
                reason="worker_heartbeat_payload_invalid",
            )
        ok = data.get("ok")
        last_tick: Any = data.get("last_tick_unix")
        if ok is not True:
            return HealthCheck(
                name="worker_heartbeat",
                status=HealthCheckStatus.UNAVAILABLE,
                required=True,
                latency_ms=_elapsed_ms(started),
                reason=f"worker_heartbeat_ok_false: ok={ok!r}",
            )
        try:
            last_tick_seconds = float(last_tick)
        except (TypeError, ValueError):
            return HealthCheck(
                name="worker_heartbeat",
                status=HealthCheckStatus.UNAVAILABLE,
                required=True,
                latency_ms=_elapsed_ms(started),
                reason=f"worker_heartbeat_tick_invalid: {last_tick!r}",
            )
        stale_seconds = time.time() - last_tick_seconds
        if stale_seconds > 120:
            return HealthCheck(
                name="worker_heartbeat",
                status=HealthCheckStatus.UNAVAILABLE,
                required=True,
                latency_ms=_elapsed_ms(started),
                reason=f"worker_heartbeat_stale: {stale_seconds:.1f}s",
            )
        return HealthCheck(
            name="worker_heartbeat",
            status=HealthCheckStatus.OK,
            required=True,
            latency_ms=_elapsed_ms(started),
        )


def _configured_check(name: str, enabled: bool) -> HealthCheck:
    return HealthCheck(
        name=name,
        status=(HealthCheckStatus.CONFIGURED if enabled else HealthCheckStatus.DISABLED),
        required=False,
    )


def _elapsed_ms(started: float) -> float:
    return (time.monotonic() - started) * 1000
