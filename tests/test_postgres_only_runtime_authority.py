from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_legacy_research_workspace_authority_is_absent() -> None:
    retired = (
        "src/pilot107/core/research_workspace.py",
        "src/pilot107/core/postgres_research_workspace.py",
        "src/pilot107/services/research_workspace_service.py",
        "src/pilot107/api/research_workspace_routes.py",
    )

    assert all(not (ROOT / path).exists() for path in retired)


def test_durable_store_factory_cannot_construct_sqlite_runtime_stores() -> None:
    source = _source("src/pilot107/agent/store_factory.py")

    forbidden = (
        "SQLiteMarketSessionStore",
        "SQLiteAgentSessionStore",
        "SQLiteProjectStore",
        "SQLiteAgentTaskStore",
    )
    assert all(name not in source for name in forbidden)
    assert "SQLite runtime authority has been retired" in source


def test_control_repository_factory_has_no_sqlite_fallback() -> None:
    source = _source("src/pilot107/core/control_repository_factory.py")

    assert "SQLiteControlRepository" not in source
    assert "return PostgresControlRepository" in source
    assert "SQLite fallback has been retired" in source


def test_workarea_is_the_only_live_research_boundary_source() -> None:
    source = _source("src/pilot107/core/workarea.py")

    assert "class PostgresWorkAreaStore" in source
    assert "SQLite is not given a second production" in source
    assert "remediation" in source
    assert "are not duplicated here" in source
