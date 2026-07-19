"""Per-process tracking of slurmrestd snapshot collection outcomes.

The API service collects a platform snapshot from slurmrestd at startup and
every 5 minutes in a daemon thread. Collection failures must NOT crash the API
(a read-only snapshot is non-fatal), but they must not be silently swallowed
either: operators need to see when collection stopped so the UI is not showing
stale platform facts.

This module records the outcome of each collection attempt in-process and
exposes a read-only snapshot that the readiness service turns into a
non-required DEGRADED indicator. The state is per-process by design (no leader
election): each API process reports its own last collection.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class SnapshotCollectionState:
    """Immutable view of the last collection outcomes for readiness reporting."""

    last_success_at: float | None
    last_error_at: float | None
    last_error_message: str | None
    consecutive_failures: int
    attempts: int

    def age_seconds(self, *, now: float | None = None) -> float | None:
        if self.last_success_at is None:
            return None
        return (now if now is not None else time.time()) - self.last_success_at


class SnapshotCollectionMonitor:
    """Thread-safe recorder for snapshot collection outcomes.

    Used by :func:`pilot107.api.service.build_api_service` to wrap the
    ``SlurmrestSnapshotCollector.collect`` call. The readiness service reads
    :meth:`state` to build the ``platform_snapshot_freshness`` check.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        max_error_length: int = 200,
    ) -> None:
        self._clock = clock or time.time
        self._max_error_length = max_error_length
        self._lock = threading.Lock()
        self._last_success_at: float | None = None
        self._last_error_at: float | None = None
        self._last_error_message: str | None = None
        self._consecutive_failures = 0
        self._attempts = 0

    def record_success(self) -> None:
        with self._lock:
            self._last_success_at = self._clock()
            self._last_error_at = None
            self._last_error_message = None
            self._consecutive_failures = 0
            self._attempts += 1

    def record_failure(self, error: BaseException) -> None:
        message = f"{type(error).__name__}: {error}"
        if len(message) > self._max_error_length:
            message = message[: self._max_error_length]
        with self._lock:
            self._last_error_at = self._clock()
            self._last_error_message = message
            self._consecutive_failures += 1
            self._attempts += 1

    def state(self) -> SnapshotCollectionState:
        with self._lock:
            return SnapshotCollectionState(
                last_success_at=self._last_success_at,
                last_error_at=self._last_error_at,
                last_error_message=self._last_error_message,
                consecutive_failures=self._consecutive_failures,
                attempts=self._attempts,
            )


__all__ = [
    "SnapshotCollectionMonitor",
    "SnapshotCollectionState",
]
