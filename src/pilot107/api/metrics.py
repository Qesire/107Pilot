"""Low-cardinality Prometheus metrics for API, outbox, and durable workers."""

from __future__ import annotations

import math
import threading
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, MutableMapping
from pathlib import Path
from typing import Any

from pilot107.core.control_repository import ControlRepository
from pilot107.worker.telemetry import COUNTERS, WorkerTelemetryError, load_worker_metrics

AsgiReceive = Callable[[], Awaitable[MutableMapping[str, Any]]]
AsgiSend = Callable[[MutableMapping[str, Any]], Awaitable[None]]
AsgiApp = Callable[[MutableMapping[str, Any], AsgiReceive, AsgiSend], Awaitable[None]]

_RESOURCE_IDENTIFIERS = {
    "advice": "advice_id",
    "contracts": "contract_id",
    "entitlements": "snapshot_id",
    "evidence": "evidence_id",
    "executions": "execution_id",
    "projects": "project_id",
    "recipes": "recipe_id",
    "remediation-sessions": "session_id",
    "runs": "run_id",
    "snapshots": "snapshot_id",
    "template-drafts": "draft_id",
    "template-reviews": "review_id",
    "templates": "template_id",
    "turns": "turn_id",
    "releases": "release_version",
}
_STATIC_CHILDREN = {
    "contracts": {"schema", "validate"},
    "entitlements": {"latest"},
    "recipes": {"latest"},
    "runs": {"compare", "filters"},
    "snapshots": {"latest"},
    "templates": {"market", "review-queue"},
}
_API_ROOTS = frozenset(
    {
        "agent",
        "contracts",
        "evidence",
        "health",
        "platform",
        "projects",
        "recipes",
        "remediation-sessions",
        "runs",
        "template-drafts",
        "template-reviews",
        "templates",
    }
)
_STATIC_SEGMENTS = frozenset(
    {
        "action",
        "adopt",
        "advance",
        "advice",
        "approve",
        "cancel",
        "capabilities",
        "compare",
        "decision",
        "diff",
        "entitlements",
        "events",
        "execute",
        "executions",
        "filters",
        "latest",
        "live",
        "market",
        "publish",
        "ready",
        "reject",
        "releases",
        "remediation-sessions",
        "review-queue",
        "reviews",
        "schema",
        "snapshots",
        "stream",
        "takeover",
        "turns",
        "validate",
        "verifications",
        "verify",
        "withdraw",
    }
)


class ControlPlaneMetrics:
    def __init__(
        self,
        *,
        control_repository: ControlRepository,
        worker_metrics_root: Path,
    ) -> None:
        self.control_repository = control_repository
        self.worker_metrics_root = worker_metrics_root
        self._lock = threading.Lock()
        self._requests: defaultdict[tuple[str, str, int], int] = defaultdict(int)
        self._duration_sum: defaultdict[tuple[str, str], float] = defaultdict(float)
        self._duration_count: defaultdict[tuple[str, str], int] = defaultdict(int)

    def observe_request(
        self,
        *,
        method: str,
        route: str,
        status: int,
        duration_seconds: float,
    ) -> None:
        normalized_method = method.upper() if method else "UNKNOWN"
        normalized_route = route if route.startswith("/") else "unmatched"
        normalized_status = status if 100 <= status <= 599 else 500
        duration = duration_seconds if math.isfinite(duration_seconds) else 0.0
        duration = max(0.0, duration)
        with self._lock:
            self._requests[(normalized_method, normalized_route, normalized_status)] += 1
            self._duration_sum[(normalized_method, normalized_route)] += duration
            self._duration_count[(normalized_method, normalized_route)] += 1

    def render(self, *, now: float | None = None) -> str:
        timestamp = time.time() if now is None else now
        with self._lock:
            requests = dict(self._requests)
            duration_sum = dict(self._duration_sum)
            duration_count = dict(self._duration_count)
        lines = [
            "# HELP pilot107_api_requests_total HTTP requests by normalized route and status.",
            "# TYPE pilot107_api_requests_total counter",
        ]
        for (method, route, status), value in sorted(requests.items()):
            labels = _labels(method=method, route=route, status=str(status))
            lines.append(f"pilot107_api_requests_total{{{labels}}} {value}")
        lines.extend(
            (
                "# HELP pilot107_api_request_duration_seconds API request duration.",
                "# TYPE pilot107_api_request_duration_seconds summary",
            )
        )
        for key in sorted(duration_count):
            method, route = key
            labels = _labels(method=method, route=route)
            lines.append(
                "pilot107_api_request_duration_seconds_sum"
                f"{{{labels}}} {_number(duration_sum[key])}"
            )
            lines.append(
                "pilot107_api_request_duration_seconds_count"
                f"{{{labels}}} {duration_count[key]}"
            )

        scrape_error = 0
        try:
            outbox = self.control_repository.outbox_metrics()
        except Exception:
            scrape_error = 1
        else:
            lines.extend(
                (
                    "# HELP pilot107_outbox_messages Current durable outbox messages.",
                    "# TYPE pilot107_outbox_messages gauge",
                    "# HELP pilot107_outbox_attempts Current aggregate delivery attempts.",
                    "# TYPE pilot107_outbox_attempts gauge",
                    "# HELP pilot107_outbox_reclaims Current aggregate lease reclaims.",
                    "# TYPE pilot107_outbox_reclaims gauge",
                    "# TYPE pilot107_outbox_due_pending gauge",
                    "# TYPE pilot107_outbox_expired_running gauge",
                )
            )
            for queue in outbox.queues:
                labels = _labels(topic=queue.topic, state=queue.state)
                lines.append(f"pilot107_outbox_messages{{{labels}}} {queue.messages}")
                lines.append(f"pilot107_outbox_attempts{{{labels}}} {queue.attempts}")
                lines.append(f"pilot107_outbox_reclaims{{{labels}}} {queue.reclaims}")
            lines.append(f"pilot107_outbox_due_pending {outbox.due_pending}")
            lines.append(f"pilot107_outbox_expired_running {outbox.expired_running}")

        try:
            workers = load_worker_metrics(self.worker_metrics_root)
        except (OSError, WorkerTelemetryError):
            scrape_error = 1
        else:
            lines.extend(
                (
                    "# HELP pilot107_worker_ticks_total Durable worker ticks.",
                    "# TYPE pilot107_worker_last_tick_unixtime gauge",
                    "# TYPE pilot107_worker_last_tick_age_seconds gauge",
                    "# TYPE pilot107_worker_last_tick_duration_seconds gauge",
                    "# TYPE pilot107_worker_active gauge",
                )
            )
            lines.extend(f"# TYPE pilot107_worker_{counter} counter" for counter in COUNTERS)
            for worker in workers:
                worker_id = str(worker["worker_id"])
                labels = _labels(worker_id=worker_id)
                counters = worker["counters"]
                assert isinstance(counters, dict)
                for counter, value in sorted(counters.items()):
                    metric = f"pilot107_worker_{counter}"
                    lines.append(f"{metric}{{{labels}}} {int(value)}")
                last_tick = float(worker["last_tick_unix"])
                active = 1 if worker.get("active", True) else 0
                lines.append(f"pilot107_worker_active{{{labels}}} {active}")
                lines.append(f"pilot107_worker_last_tick_unixtime{{{labels}}} {_number(last_tick)}")
                lines.append(
                    "pilot107_worker_last_tick_age_seconds"
                    f"{{{labels}}} {_number(max(0.0, timestamp - last_tick))}"
                )
                lines.append(
                    "pilot107_worker_last_tick_duration_seconds"
                    f"{{{labels}}} {_number(float(worker['last_tick_duration_seconds']))}"
                )
        lines.extend(
            (
                "# HELP pilot107_metrics_scrape_error Whether a durable metric source failed.",
                "# TYPE pilot107_metrics_scrape_error gauge",
                f"pilot107_metrics_scrape_error {scrape_error}",
            )
        )
        return "\n".join(lines) + "\n"


class ApiMetricsMiddleware:
    def __init__(self, app: AsgiApp, *, metrics: ControlPlaneMetrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: AsgiReceive,
        send: AsgiSend,
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status = 500

        async def observe_send(message: MutableMapping[str, Any]) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                candidate = message.get("status")
                if isinstance(candidate, int):
                    status = candidate
            await send(message)

        try:
            await self.app(scope, receive, observe_send)
        finally:
            self.metrics.observe_request(
                method=str(scope.get("method") or "UNKNOWN"),
                route=_route_label(scope),
                status=status,
                duration_seconds=time.monotonic() - started,
            )


def _route_label(scope: MutableMapping[str, Any]) -> str:
    route = scope.get("route")
    route_path = getattr(route, "path", None)
    if route_path == "/{path:path}":
        raw_path = scope.get("path")
        return normalize_http_route(raw_path) if isinstance(raw_path, str) else "unmatched"
    return (
        route_path
        if isinstance(route_path, str) and route_path.startswith("/")
        else "unmatched"
    )


def normalize_http_route(path: str) -> str:
    """Normalize compatibility paths without retaining object identifiers."""

    path = path.partition("?")[0]
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return "/"
    if segments[:2] != ["api", "v1"]:
        return path if path in {"/healthz", "/metrics"} else "/unmatched"
    if len(segments) < 3 or segments[2] not in _API_ROOTS:
        return "/api/v1/unmatched"
    normalized: list[str] = []
    for index, segment in enumerate(segments):
        parent = segments[index - 1] if index else ""
        placeholder = _RESOURCE_IDENTIFIERS.get(parent)
        if placeholder is not None and segment not in _STATIC_CHILDREN.get(parent, set()):
            normalized.append("{" + placeholder + "}")
        elif index > 2 and segment not in _STATIC_SEGMENTS:
            normalized.append("{action}")
        else:
            normalized.append(segment)
    return "/" + "/".join(normalized)


def _labels(**values: str) -> str:
    return ",".join(
        f'{name}="{_escape_label(value)}"' for name, value in sorted(values.items())
    )


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _number(value: float) -> str:
    return format(value, ".9g")
