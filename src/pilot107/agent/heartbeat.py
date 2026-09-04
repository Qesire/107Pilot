"""Small fail-closed heartbeat runner for already-fenced Agent work.

The heartbeat thread never owns domain transitions.  It only calls a supplied
renew/check function and records the first failure.  The foreground owner must
check ``raise_if_failed`` before committing more durable work.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from types import TracebackType


class AgentHeartbeatError(RuntimeError):
    """A background lease/attempt heartbeat could no longer prove authority."""


class PeriodicHeartbeat:
    def __init__(
        self,
        beat: Callable[[], None],
        *,
        interval_seconds: float,
        name: str,
    ) -> None:
        if not callable(beat):
            raise TypeError("beat must be callable")
        if isinstance(interval_seconds, bool) or interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if not isinstance(name, str) or not name or "\0" in name:
            raise ValueError("heartbeat name is invalid")
        self._beat = beat
        self._interval_seconds = float(interval_seconds)
        self._name = name
        self._stop = threading.Event()
        self._failure: Exception | None = None
        self._thread: threading.Thread | None = None

    @property
    def failed(self) -> bool:
        return self._failure is not None

    def start(self) -> PeriodicHeartbeat:
        if self._thread is not None:
            raise RuntimeError("heartbeat is already started")
        thread = threading.Thread(
            target=self._run,
            name=self._name,
            daemon=True,
        )
        self._thread = thread
        thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._interval_seconds + 1.0)

    def raise_if_failed(self) -> None:
        failure = self._failure
        if failure is not None:
            raise AgentHeartbeatError("Agent heartbeat lost its fenced authority") from failure

    def __enter__(self) -> PeriodicHeartbeat:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.stop()

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                self._beat()
            except Exception as exc:
                self._failure = exc
                self._stop.set()
                return
