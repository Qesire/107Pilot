from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pilot107.agent.postgres_store import PostgresAgentSessionStore
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.agent.store_factory import build_agent_session_store

from .test_store_contract import exercise_agent_store_contract


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 14, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


def test_factory_selects_sqlite_without_postgres_dsn(tmp_path: Path) -> None:
    store = build_agent_session_store(
        sqlite_path=tmp_path / "agent.db",
        postgres_dsn=None,
    )

    assert isinstance(store, SQLiteAgentSessionStore)


def test_factory_selects_postgres_and_installs_checkpoint_durability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    dsn = "postgresql://agent-store-test"
    calls: list[tuple[str, str]] = []

    def build_store(value: str) -> object:
        calls.append(("store", value))
        return sentinel

    def install_checkpoint_pointer(value: str) -> None:
        calls.append(("checkpoint-durability", value))

    monkeypatch.setattr(
        "pilot107.agent.store_factory.PostgresAgentSessionStore",
        build_store,
    )
    monkeypatch.setattr(
        "pilot107.agent.store_factory.ensure_postgres_checkpoint_pointer",
        install_checkpoint_pointer,
    )

    store = build_agent_session_store(
        sqlite_path=Path("unused.db"),
        postgres_dsn=dsn,
    )

    assert store is sentinel
    # The low-level Store constructor establishes the base Postgres domain
    # schema. AC3 durability is then installed before the configured Store is
    # allowed to escape the production factory.
    assert calls == [
        ("store", dsn),
        ("checkpoint-durability", dsn),
    ]


@pytest.mark.skipif(
    not os.environ.get("PILOT107_TEST_POSTGRES_DSN")
    or os.environ.get("PILOT107_TEST_POSTGRES_ALLOW_RESET") != "1",
    reason="set a dedicated PostgreSQL DSN and explicit reset opt-in",
)
def test_postgres_store_satisfies_backend_contract() -> None:
    dsn = os.environ["PILOT107_TEST_POSTGRES_DSN"]
    clock = MutableClock()
    store = PostgresAgentSessionStore(dsn, clock=clock)
    with store.connect() as conn:
        conn.execute(
            "TRUNCATE agent_turn_events, agent_tool_invocations, agent_turns, "
            "agent_sessions RESTART IDENTITY CASCADE"
        )

    exercise_agent_store_contract(store, advance_clock=clock.advance)
