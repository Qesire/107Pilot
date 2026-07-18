"""build_api_service wires SlurmrestSnapshotCollector at startup."""
from __future__ import annotations

import importlib
import threading
import time

import pytest


def _reload_service_module():
    from pilot107.api import service as service_module
    importlib.reload(service_module)
    return service_module


def test_build_api_service_invokes_initial_snapshot(cpu_rc_env):
    """build_api_service should collect an initial snapshot at startup."""
    service_module = _reload_service_module()
    collected: list = []

    class StubCollector:
        def __init__(self, *args, **kwargs): pass
        def collect(self, *, captured_at=None):
            collected.append(captured_at)
            from pilot107.core.platform_snapshot import (
                PlatformSnapshot,
                PlatformSnapshotScope,
            )
            return PlatformSnapshot(
                snapshot_id="test-snap",
                scope=PlatformSnapshotScope.SIMULATOR,
                captured_at=captured_at or "2026-01-01T00:00:00+00:00",
                collector_version="stub",
                command_results=(),
                partitions=(),
                nodes=(),
                squeue_jobs=(),
                defaults=(),
                runtime_limitations=(),
                limitations=(),
                redaction_report=(),
            )

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(service_module, "SlurmrestSnapshotCollector", StubCollector)
        try:
            service_module.build_api_service(service_module.config_from_env())
        except Exception:
            pass  # we only care that the collector was invoked
    assert len(collected) >= 1, "initial snapshot collection must run at startup"


def test_build_api_service_starts_background_refresh_thread(cpu_rc_env):
    """build_api_service should start a daemon refresh thread (does not block exit)."""
    service_module = _reload_service_module()
    threads_before = [
        t for t in threading.enumerate()
        if "snapshot" in t.name.lower() or "slurmrest" in t.name.lower()
    ]
    try:
        service_module.build_api_service(service_module.config_from_env())
    except Exception:
        pass
    time.sleep(0.1)
    threads_after = [
        t for t in threading.enumerate()
        if "snapshot" in t.name.lower() or "slurmrest" in t.name.lower()
    ]
    assert len(threads_after) > len(threads_before), "refresh thread must start"
    new_threads = [t for t in threads_after if t not in threads_before]
    assert all(t.daemon for t in new_threads), "refresh thread must be daemon"
