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
    "agent-sessions": "session_id",
    "contracts": "contract_id",
    "connections": "connection_id",
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
        "agent-sessions",
        "contracts",
        "evidence",
        "files",
        "health",
        "observability",
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
        "abort",
        "action",
        "adopt",
        "advance",
        "agent-sessions",
        "advice",
        "approve",
        "archive",
        "cancel",
        "capabilities",
        "account",
        "compare",
        "complete",
        "content",
        "decision",
        "delete",
        "diff",
        "entitlements",
        "events",
        "execute",
        "executions",
        "filters",
        "latest",
        "live",
        "market",
        "mkdir",
        "publish",
        "ready",
        "resource-evaluations",
        "resources",
        "reject",
        "releases",
        "remediation-sessions",
        "rename",
        "review-queue",
        "reviews",
        "schema",
        "snapshots",
        "series",
        "stream",
        "takeover",
        "turns",
        "tus",
        "uploads",
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
        self._trace_writes: defaultdict[str, int] = defaultdict(int)
        self._llm_calls: defaultdict[tuple[str, str, str], int] = defaultdict(int)
        self._llm_duration_sum: defaultdict[tuple[str, str], float] = defaultdict(float)
        self._llm_duration_count: defaultdict[tuple[str, str], int] = defaultdict(int)
        self._llm_tokens: defaultdict[tuple[str, str, str], int] = defaultdict(int)
        self._sse_active = 0
        self._sse_streams: defaultdict[str, int] = defaultdict(int)
        self._sse_duration_sum = 0.0
        self._sse_duration_count = 0
        self._sse_events = 0
        self._upload_events: defaultdict[str, int] = defaultdict(int)
        self._upload_bytes_total = 0

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

    def observe_trace_write(self, *, outcome: str) -> None:
        normalized = outcome if outcome in {"success", "error"} else "error"
        with self._lock:
            self._trace_writes[normalized] += 1

    def observe_llm_call(
        self,
        *,
        provider: str,
        model: str,
        outcome: str,
        duration_seconds: float,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        labels = (_metric_label(provider), _metric_label(model))
        normalized_outcome = _llm_outcome(outcome)
        duration = duration_seconds if math.isfinite(duration_seconds) else 0.0
        with self._lock:
            self._llm_calls[(*labels, normalized_outcome)] += 1
            self._llm_duration_sum[labels] += max(0.0, duration)
            self._llm_duration_count[labels] += 1
            self._llm_tokens[(*labels, "input")] += max(0, input_tokens)
            self._llm_tokens[(*labels, "output")] += max(0, output_tokens)

    def sse_opened(self) -> None:
        with self._lock:
            self._sse_active += 1

    def observe_upload_event(self, *, outcome: str, size_bytes: int = 0) -> None:
        normalized = outcome if outcome in {
            "created", "chunk", "completed", "aborted", "failed", "quota_rejected",
        } else "error"
        with self._lock:
            self._upload_events[normalized] += 1
            self._upload_bytes_total += max(0, size_bytes)

    def observe_sse_closed(
        self,
        *,
        outcome: str,
        duration_seconds: float,
        events: int,
    ) -> None:
        normalized = (
            outcome if outcome in {"complete", "deadline", "poll_error", "disconnect"} else "error"
        )
        duration = duration_seconds if math.isfinite(duration_seconds) else 0.0
        with self._lock:
            self._sse_active = max(0, self._sse_active - 1)
            self._sse_streams[normalized] += 1
            self._sse_duration_sum += max(0.0, duration)
            self._sse_duration_count += 1
            self._sse_events += max(0, events)

    def render(self, *, now: float | None = None) -> str:
        timestamp = time.time() if now is None else now
        with self._lock:
            requests = dict(self._requests)
            duration_sum = dict(self._duration_sum)
            duration_count = dict(self._duration_count)
            trace_writes = dict(self._trace_writes)
            llm_calls = dict(self._llm_calls)
            llm_duration_sum = dict(self._llm_duration_sum)
            llm_duration_count = dict(self._llm_duration_count)
            llm_tokens = dict(self._llm_tokens)
            sse_active = self._sse_active
            sse_streams = dict(self._sse_streams)
            sse_duration_sum = self._sse_duration_sum
            sse_duration_count = self._sse_duration_count
            sse_events = self._sse_events
            upload_events = dict(self._upload_events)
            upload_bytes_total = self._upload_bytes_total
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
                f"pilot107_api_request_duration_seconds_count{{{labels}}} {duration_count[key]}"
            )
        lines.extend(
            (
                "# HELP pilot107_control_trace_writes_total Durable trace write outcomes.",
                "# TYPE pilot107_control_trace_writes_total counter",
            )
        )
        for outcome, value in sorted(trace_writes.items()):
            lines.append(
                f"pilot107_control_trace_writes_total{{{_labels(outcome=outcome)}}} {value}"
            )
        lines.extend(
            (
                "# HELP pilot107_llm_calls_total Local LLM call attempts.",
                "# TYPE pilot107_llm_calls_total counter",
                "# HELP pilot107_llm_call_duration_seconds Local LLM call duration.",
                "# TYPE pilot107_llm_call_duration_seconds summary",
                "# HELP pilot107_llm_tokens_total Reported local LLM tokens.",
                "# TYPE pilot107_llm_tokens_total counter",
            )
        )
        for (provider, model, outcome), value in sorted(llm_calls.items()):
            labels = _labels(provider=provider, model=model, outcome=outcome)
            lines.append(f"pilot107_llm_calls_total{{{labels}}} {value}")
        for (provider, model), count in sorted(llm_duration_count.items()):
            labels = _labels(provider=provider, model=model)
            lines.append(
                f"pilot107_llm_call_duration_seconds_sum{{{labels}}} "
                f"{_number(llm_duration_sum[(provider, model)])}"
            )
            lines.append(f"pilot107_llm_call_duration_seconds_count{{{labels}}} {count}")
        for (provider, model, direction), value in sorted(llm_tokens.items()):
            labels = _labels(provider=provider, model=model, direction=direction)
            lines.append(f"pilot107_llm_tokens_total{{{labels}}} {value}")
        lines.extend(
            (
                "# HELP pilot107_sse_active Active server-sent event streams.",
                "# TYPE pilot107_sse_active gauge",
                f"pilot107_sse_active {sse_active}",
                "# HELP pilot107_sse_streams_total Completed server-sent event streams.",
                "# TYPE pilot107_sse_streams_total counter",
                "# HELP pilot107_sse_stream_duration_seconds Server-sent event stream duration.",
                "# TYPE pilot107_sse_stream_duration_seconds summary",
                f"pilot107_sse_stream_duration_seconds_sum {_number(sse_duration_sum)}",
                f"pilot107_sse_stream_duration_seconds_count {sse_duration_count}",
                "# HELP pilot107_sse_events_total Server-sent events delivered.",
                "# TYPE pilot107_sse_events_total counter",
                f"pilot107_sse_events_total {sse_events}",
            )
        )
        for outcome, value in sorted(sse_streams.items()):
            lines.append(f"pilot107_sse_streams_total{{{_labels(outcome=outcome)}}} {value}")
        lines.extend(
            (
                "# HELP pilot107_upload_events_total File upload lifecycle events.",
                "# TYPE pilot107_upload_events_total counter",
                "# HELP pilot107_upload_bytes_total Total bytes received via file uploads.",
                "# TYPE pilot107_upload_bytes_total counter",
                f"pilot107_upload_bytes_total {upload_bytes_total}",
            )
        )
        for outcome, value in sorted(upload_events.items()):
            lines.append(f"pilot107_upload_events_total{{{_labels(outcome=outcome)}}} {value}")

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
    return route_path if isinstance(route_path, str) and route_path.startswith("/") else "unmatched"


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
    return ",".join(f'{name}="{_escape_label(value)}"' for name, value in sorted(values.items()))


def _metric_label(value: str) -> str:
    normalized = value.strip()[:128]
    return normalized if normalized and "\n" not in normalized else "unknown"


def _llm_outcome(value: str) -> str:
    if value == "success":
        return value
    if value.startswith("http_"):
        try:
            status = int(value.removeprefix("http_"))
        except ValueError:
            return "provider_error"
        return "http_5xx" if status >= 500 else "http_4xx"
    if value.startswith("invalid_schema"):
        return "invalid_schema"
    if value in {
        "transport_error",
        "invalid_response",
        "invalid_json",
        "invalid_citation",
        "incomplete_citations",
    }:
        return value
    return "provider_error"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _number(value: float) -> str:
    return format(value, ".9g")
