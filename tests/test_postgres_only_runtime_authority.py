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


def test_live_production_composition_has_no_research_workspace_bridge() -> None:
    sources = (
        "src/pilot107/api/run_workspace_routes.py",
        "src/pilot107/api/service.py",
        "src/pilot107/api/http_app.py",
        "src/pilot107/worker/service.py",
    )
    forbidden = (
        "research_workspace",
        "research-workspaces",
        "ResearchWorkspace",
    )

    for relative in sources:
        source = _source(relative)
        assert all(marker not in source for marker in forbidden), relative


def test_production_composition_has_no_sqlite_runtime_authority() -> None:
    sources = (
        "src/pilot107/api/service.py",
        "src/pilot107/api/http_app.py",
        "src/pilot107/worker/service.py",
        "src/pilot107/api/run_workspace_routes.py",
    )
    forbidden = (
        "DatabaseMode.SQLITE",
        "SQLiteObservabilityStore",
        "SQLiteRuntimeWatchStore",
        "SQLiteControlRepository",
        "SQLiteAgentSessionStore",
        "SQLiteResearchWorkspaceStore",
        "if not selection.is_postgres",
    )

    for relative in sources:
        source = _source(relative)
        assert all(marker not in source for marker in forbidden), relative


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
    assert "SQLite runtime authority has been retired" in source


def test_workarea_is_the_only_live_research_boundary_source() -> None:
    source = _source("src/pilot107/core/workarea.py")

    assert "class PostgresWorkAreaStore" in source
    assert "SQLite is not given a second production" in source
    assert "contract_ids: tuple[str, ...]" in source
    assert "run_ids: tuple[str, ...]" in source
    assert "agent_project_ids: tuple[str, ...]" in source
    assert "assets: tuple[WorkAreaAssetRef, ...]" in source
    assert "remediation_session_ids" not in source
    assert "agent_session_ids" not in source
    assert "Evidence, diagnosis and repair facts are reached through Run/AgentProject" in source
    assert "provenance and are not duplicated here" in source


def test_workarea_historical_migration_ids_remain_frozen() -> None:
    source = _source("src/pilot107/core/workarea.py")
    frozen_ids = (
        '_MIGRATION_002_ID = "006c.002.research_workspace_boundary"',
        '_MIGRATION_003_ID = "006c.003.research_workspace_run_edge_normalization"',
        '_MIGRATION_004_ID = "006c.004.workarea_terminology"',
    )

    assert all(migration_id in source for migration_id in frozen_ids)
