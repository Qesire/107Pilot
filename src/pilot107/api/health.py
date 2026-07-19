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

from pilot107.adapters.rest_token import TokenValidityProbe
from pilot107.core.platform_snapshot_store import PlatformSnapshotStore
from pilot107.core.run_store import RunStore
from pilot107.core.user_entitlement_store import UserEntitlementStore
from pilot107.services.platform_snapshot_freshness import (
    SnapshotCollectionMonitor,
)


class HealthCheckStatus(StrEnum):
    OK = "ok"
    CONFIGURED = "configured"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    DEGRADED = "degraded"


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
        snapshot_freshness_monitor: SnapshotCollectionMonitor | None = None,
        token_validity_probe: TokenValidityProbe | None = None,
    ) -> None:
        self.store = store
        self.evidence_root = evidence_root
        self.platform_snapshot_store = platform_snapshot_store
        self.submission_enabled = submission_enabled
        self.llm_enabled = llm_enabled
        self.user_entitlement_store = user_entitlement_store
        self.worker_health_path = worker_health_path
        self.snapshot_freshness_monitor = snapshot_freshness_monitor
        self.token_validity_probe = token_validity_probe

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
        checks.append(self._platform_snapshot_freshness_check())
        checks.append(self._slurm_token_validity_check())
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

    def _platform_snapshot_freshness_check(self) -> HealthCheck:
        """Report whether this process is successfully collecting snapshots.

        Non-required by design: a slurmrestd collection failure must not block
        deploy (the original "startup must not crash" intent), but it must be
        visible to operators as a DEGRADED indicator with last-success age,
        consecutive failure count, and a truncated last error.
        """
        monitor = self.snapshot_freshness_monitor
        if monitor is None:
            return HealthCheck(
                name="platform_snapshot_freshness",
                status=HealthCheckStatus.DISABLED,
                required=False,
            )
        state = monitor.state()
        now = time.time()
        if state.last_success_at is None and state.consecutive_failures == 0:
            return HealthCheck(
                name="platform_snapshot_freshness",
                status=HealthCheckStatus.DISABLED,
                required=False,
                reason="snapshot collection has not run yet",
            )
        if state.consecutive_failures > 0:
            age = now - state.last_success_at if state.last_success_at else None
            reason = (
                f"collection failing: consecutive_failures={state.consecutive_failures}"
                f" last_success_age={_fmt_seconds(age)}"
            )
            if state.last_error_message:
                reason += f" last_error={state.last_error_message}"
            return HealthCheck(
                name="platform_snapshot_freshness",
                status=HealthCheckStatus.DEGRADED,
                required=False,
                reason=reason,
            )
        # Last collection succeeded: flag stale if it has been more than two
        # refresh intervals (2 * 5 min) since the last success.
        age = now - state.last_success_at if state.last_success_at else None
        if age is not None and age > 600:
            return HealthCheck(
                name="platform_snapshot_freshness",
                status=HealthCheckStatus.DEGRADED,
                required=False,
                reason=f"snapshot stale: last_success_age={age:.0f}s",
            )
        return HealthCheck(
            name="platform_snapshot_freshness",
            status=HealthCheckStatus.OK,
            required=False,
            reason=f"last_success_age={_fmt_seconds(age)}",
        )

    def _slurm_token_validity_check(self) -> HealthCheck:
        """Report Slurm REST JWT validity for the rest-native path.

        Non-required: an expired/missing JWT must not block deploy, but it must
        be visible. For simulator-minted tokens this reflects re-mint health;
        for externally-managed tokens (``PILOT107_SLURM_TOKEN``) it reports
        remaining lifespan if parseable, else "externally managed". The
        command-gateway path does not use a JWT and reports DISABLED.
        """
        probe = self.token_validity_probe
        if probe is None:
            return HealthCheck(
                name="slurm_token_validity",
                status=HealthCheckStatus.DISABLED,
                required=False,
            )
        validity = probe.validity()
        margin = validity.refresh_margin_seconds
        if validity.last_re_mint_error is not None:
            return HealthCheck(
                name="slurm_token_validity",
                status=HealthCheckStatus.DEGRADED,
                required=False,
                reason=(
                    f"re-mint failed: {validity.last_re_mint_error} "
                    f"remaining={_fmt_seconds(validity.remaining_seconds)}"
                ),
            )
        if validity.remaining_seconds is None:
            # Externally managed with unparseable expiry, or no token minted
            # yet. We cannot assert degradation without expiry info, so report
            # CONFIGURED (non-OK, non-required) so it is visible but not
            # alarming.
            if validity.externally_managed:
                return HealthCheck(
                    name="slurm_token_validity",
                    status=HealthCheckStatus.CONFIGURED,
                    required=False,
                    reason="externally managed (expiry unknown)",
                )
            return HealthCheck(
                name="slurm_token_validity",
                status=HealthCheckStatus.CONFIGURED,
                required=False,
                reason="no token minted yet",
            )
        remaining = validity.remaining_seconds
        if remaining <= 0:
            return HealthCheck(
                name="slurm_token_validity",
                status=HealthCheckStatus.DEGRADED,
                required=False,
                reason=f"token expired: remaining={remaining:.0f}s",
            )
        if remaining <= margin:
            return HealthCheck(
                name="slurm_token_validity",
                status=HealthCheckStatus.DEGRADED,
                required=False,
                reason=f"token near expiry: remaining={remaining:.0f}s margin={margin}s",
            )
        return HealthCheck(
            name="slurm_token_validity",
            status=HealthCheckStatus.OK,
            required=False,
            reason=f"remaining={remaining:.0f}s mode={validity.mode}",
        )


def _configured_check(name: str, enabled: bool) -> HealthCheck:
    return HealthCheck(
        name=name,
        status=(HealthCheckStatus.CONFIGURED if enabled else HealthCheckStatus.DISABLED),
        required=False,
    )


def _elapsed_ms(started: float) -> float:
    return (time.monotonic() - started) * 1000


def _fmt_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    return f"{seconds:.0f}s"
