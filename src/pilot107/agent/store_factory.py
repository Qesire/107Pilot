"""Runtime selection for the durable Agent Session Store."""

from __future__ import annotations

from pathlib import Path

from pilot107.agent.postgres_store import PostgresAgentSessionStore
from pilot107.agent.store import AgentSessionStore, SQLiteAgentSessionStore


def build_agent_session_store(
    *, sqlite_path: Path, postgres_dsn: str | None
) -> AgentSessionStore:
    if postgres_dsn:
        return PostgresAgentSessionStore(postgres_dsn)
    return SQLiteAgentSessionStore(sqlite_path)
