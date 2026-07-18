"""Small process-local transport guards shared by API adapters."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class FixedWindowRateLimiter:
    limit: int
    window_seconds: int
    clock: Callable[[], float] = time.monotonic
    _windows: dict[str, tuple[int, int]] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def check(self, key: str) -> tuple[bool, int]:
        now = int(self.clock())
        window = now // self.window_seconds
        with self._lock:
            selected_window, count = self._windows.get(key, (window, 0))
            if selected_window != window:
                selected_window, count = window, 0
            count += 1
            self._windows[key] = (selected_window, count)
        retry_after = max(1, (window + 1) * self.window_seconds - now)
        return count <= self.limit, retry_after
