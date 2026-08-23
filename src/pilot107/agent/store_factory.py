"""Runtime selection for the durable Agent Session Store."""

from __future__ import annotations

from pathlib import Path

from pilot107.agent.postgres_project_store import PostgresProjectStore
from pilot107.agent.postgres_store import PostgresAgentSessionStore
from pilot107.agent.postgres_task_store import PostgresAgentTaskStore
from pilot107.agent.project_store import ProjectStore, SQLiteProjectStore
from pilot107.agent.store import AgentSessionStore, SQLiteAgentSessionStore
from pilot107.agent.task_store import AgentTaskStore, SQLiteAgentTaskStore


def build_agent_session_store(
    *, sqlite_path: Path, postgres_dsn: str | None
) -> AgentSessionStore:
    if postgres_dsn:
        return PostgresAgentSessionStore(postgres_dsn)
    return SQLiteAgentSessionStore(sqlite_path)


def build_project_store(*, sqlite_path: Path, postgres_dsn: str | None) -> ProjectStore:
    if postgres_dsn:
        return PostgresProjectStore(postgres_dsn)
    return SQLiteProjectStore(sqlite_path)


def build_agent_task_store(
    *, sqlite_path: Path, postgres_dsn: str | None
) -> AgentTaskStore:
    if postgres_dsn:
        return PostgresAgentTaskStore(postgres_dsn)
    return SQLiteAgentTaskStore(sqlite_path)
