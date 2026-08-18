"""Durable, per-worker cumulative telemetry with atomic publication."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

WORKER_METRICS_SCHEMA = "pilot107.worker_metrics.v1"
COUNTERS = (
    "ticks_total",
    "reconcile_checked_total",
    "reconcile_terminal_total",
    "reconcile_errors_total",
    "collection_checked_total",
    "collection_succeeded_total",
    "collection_errors_total",
    "diagnosis_checked_total",
    "diagnosis_succeeded_total",
    "diagnosis_errors_total",
    "submission_checked_total",
    "submission_succeeded_total",
    "submission_errors_total",
    "agent_execution_checked_total",
    "agent_execution_succeeded_total",
    "agent_execution_errors_total",
    "agent_turn_checked_total",
    "agent_turn_succeeded_total",
    "agent_turn_errors_total",
    "remediation_checked_total",
    "remediation_advanced_total",
    "remediation_errors_total",
    "capsule_builds_attempted_total",
    "capsule_builds_succeeded_total",
    "capsule_errors_total",
)


class WorkerTelemetryError(RuntimeError):
    """Raised when durable telemetry cannot be safely read or updated."""


class WorkerTelemetryStore:
    def __init__(self, *, root: Path, worker_id: str) -> None:
        if not worker_id:
            raise ValueError("worker_id must not be empty")
        digest = hashlib.sha256(worker_id.encode("utf-8")).hexdigest()[:20]
        self.root = root
        self.worker_id = worker_id
        self.path = root / f"worker-{digest}.json"
        self.lock_path = root / f"worker-{digest}.lock"

    def update(
        self,
        *,
        increments: Mapping[str, int],
        tick_duration_seconds: float,
        timestamp: float | None = None,
    ) -> dict[str, Any]:
        if tick_duration_seconds < 0:
            raise ValueError("tick_duration_seconds must not be negative")
        unknown = set(increments).difference(COUNTERS)
        if unknown:
            raise ValueError(f"unknown telemetry counters: {sorted(unknown)}")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in increments.values()
        ):
            raise ValueError("telemetry increments must be non-negative integers")
        now = time.time() if timestamp is None else timestamp
        self.root.mkdir(parents=True, exist_ok=True)
        with _exclusive_regular_lock(self.lock_path):
            payload = self._read_or_initialize(now)
            counters = payload["counters"]
            assert isinstance(counters, dict)
            for name in COUNTERS:
                counters[name] = int(counters[name]) + int(increments.get(name, 0))
            payload["last_tick_unix"] = now
            payload["last_tick_duration_seconds"] = round(tick_duration_seconds, 6)
            payload["active"] = True
            payload["stopped_at_unix"] = None
            _write_json_atomic(self.path, payload)
        return payload

    def mark_stopped(self, *, timestamp: float | None = None) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        now = time.time() if timestamp is None else timestamp
        self.root.mkdir(parents=True, exist_ok=True)
        with _exclusive_regular_lock(self.lock_path):
            payload = self._read_or_initialize(now)
            payload["active"] = False
            payload["stopped_at_unix"] = now
            _write_json_atomic(self.path, payload)
        return payload

    def _read_or_initialize(self, now: float) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "schema": WORKER_METRICS_SCHEMA,
                "worker_id": self.worker_id,
                "first_tick_unix": now,
                "last_tick_unix": now,
                "last_tick_duration_seconds": 0.0,
                "active": True,
                "stopped_at_unix": None,
                "counters": {name: 0 for name in COUNTERS},
            }
        if self.path.is_symlink() or not self.path.is_file():
            raise WorkerTelemetryError("worker metrics path is not a regular file")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkerTelemetryError(
                f"worker metrics are unreadable: {type(exc).__name__}"
            ) from exc
        _validate_payload(payload, worker_id=self.worker_id)
        # Backfill counters added after the file was written (additive schema
        # migration). The validator allows missing counters (subset check);
        # here we initialize them to 0 so the file is upgraded on first read.
        counters = cast(dict[str, Any], payload["counters"])
        for name in COUNTERS:
            if name not in counters:
                counters[name] = 0
        return cast(dict[str, Any], payload)


def load_worker_metrics(root: Path) -> list[dict[str, Any]]:
    """Load all valid per-worker snapshots; reject ambiguity or corruption."""

    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise WorkerTelemetryError("worker metrics root is not a real directory")
    snapshots: list[dict[str, Any]] = []
    worker_ids: set[str] = set()
    for path in sorted(root.glob("worker-*.json")):
        if path.is_symlink() or not path.is_file():
            raise WorkerTelemetryError(f"invalid worker metrics entry: {path.name}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkerTelemetryError(
                f"worker metrics entry is unreadable: {path.name}"
            ) from exc
        worker_id = payload.get("worker_id") if isinstance(payload, dict) else None
        if not isinstance(worker_id, str):
            raise WorkerTelemetryError(f"worker metrics identity is invalid: {path.name}")
        _validate_payload(payload, worker_id=worker_id)
        if worker_id in worker_ids:
            raise WorkerTelemetryError(f"duplicate worker metrics identity: {worker_id}")
        worker_ids.add(worker_id)
        snapshots.append(payload)
    return snapshots


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    _write_json_atomic(path, payload)


def _validate_payload(payload: object, *, worker_id: str) -> None:
    if not isinstance(payload, dict):
        raise WorkerTelemetryError("worker metrics payload is invalid")
    if payload.get("schema") != WORKER_METRICS_SCHEMA:
        raise WorkerTelemetryError("worker metrics schema is invalid")
    if payload.get("worker_id") != worker_id:
        raise WorkerTelemetryError("worker metrics identity does not match")
    counters = payload.get("counters")
    if not isinstance(counters, dict) or not set(counters).issubset(set(COUNTERS)):
        raise WorkerTelemetryError("worker metrics counters are invalid")
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in counters.values()
    ):
        raise WorkerTelemetryError("worker metrics counter value is invalid")
    for name in ("first_tick_unix", "last_tick_unix", "last_tick_duration_seconds"):
        value = payload.get(name)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise WorkerTelemetryError(f"worker metrics {name} is invalid")
    active = payload.get("active", True)
    if not isinstance(active, bool):
        raise WorkerTelemetryError("worker metrics active flag is invalid")
    stopped_at = payload.get("stopped_at_unix")
    if stopped_at is not None and (
        not isinstance(stopped_at, (int, float))
        or isinstance(stopped_at, bool)
        or stopped_at < 0
    ):
        raise WorkerTelemetryError("worker metrics stopped_at_unix is invalid")


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


@contextmanager
def _exclusive_regular_lock(path: Path) -> Iterator[None]:
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise WorkerTelemetryError("worker metrics lock is not a regular file")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)
