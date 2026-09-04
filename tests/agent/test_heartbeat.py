from __future__ import annotations

import threading

import pytest

from pilot107.agent.heartbeat import AgentHeartbeatError, PeriodicHeartbeat


def test_periodic_heartbeat_reports_background_authority_failure() -> None:
    called = threading.Event()

    def beat() -> None:
        called.set()
        raise RuntimeError("fenced")

    heartbeat = PeriodicHeartbeat(
        beat,
        interval_seconds=0.01,
        name="test-heartbeat",
    ).start()
    try:
        assert called.wait(1.0)
        with pytest.raises(AgentHeartbeatError) as failure:
            heartbeat.raise_if_failed()
        assert isinstance(failure.value.__cause__, RuntimeError)
    finally:
        heartbeat.stop()


def test_periodic_heartbeat_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError):
        PeriodicHeartbeat(lambda: None, interval_seconds=0, name="heartbeat")
    with pytest.raises(ValueError):
        PeriodicHeartbeat(lambda: None, interval_seconds=1, name="")
