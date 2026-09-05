from __future__ import annotations

from pathlib import Path

import pytest

from pilot107.agent.store_factory import (
    ConfigurationError,
    DatabaseMode,
    build_agent_session_store,
    build_agent_task_store,
    build_project_store,
    resolve_durable_store_selection,
)
from pilot107.api.service import ApiServiceConfig, build_api_service
from pilot107.api.service import config_from_env as api_config_from_env
from pilot107.core.control_repository_factory import build_control_repository
from pilot107.core.postgres_domain_schema import domain_table_names
from pilot107.worker.service import WorkerServiceConfig, build_worker_service
from pilot107.worker.service import config_from_env as worker_config_from_env


SQLITE_RETIRED = "SQLite runtime authority has been retired"


def test_environment_resolves_postgres_when_dsn_is_present(tmp_path: Path) -> None:
    dsn = "postgresql://api@db.internal:5432/pilot107"
    api = api_config_from_env(
        {"PILOT107_POSTGRES_DSN": dsn},
        project_root=tmp_path,
    )
    worker = worker_config_from_env(
        {"PILOT107_POSTGRES_DSN": dsn},
        project_root=tmp_path,
    )

    assert api.database_mode is DatabaseMode.POSTGRES
    assert worker.database_mode is DatabaseMode.POSTGRES
    assert api.postgres_dsn == worker.postgres_dsn == dsn
    assert api.control_postgres_dsn == worker.control_postgres_dsn == dsn


def test_postgres_dsn_file_is_shared_without_entering_config_repr(tmp_path: Path) -> None:
    dsn = "postgresql://api@db.internal:5432/pilot107"
    secret_file = tmp_path / "postgres-dsn"
    secret_file.write_text(dsn + "\n")
    environment = {"PILOT107_POSTGRES_DSN_FILE": str(secret_file)}

    api = api_config_from_env(environment, project_root=tmp_path)
    worker = worker_config_from_env(environment, project_root=tmp_path)

    assert api.database_mode is worker.database_mode is DatabaseMode.POSTGRES
    assert api.postgres_dsn == worker.postgres_dsn == dsn
    assert dsn not in repr(api)
    assert dsn not in repr(worker)


def test_postgres_dsn_rejects_inline_and_file_sources(tmp_path: Path) -> None:
    secret_file = tmp_path / "postgres-dsn"
    secret_file.write_text("postgresql://file@db.internal/pilot107\n")
    environment = {
        "PILOT107_POSTGRES_DSN": "postgresql://inline@db.internal/pilot107",
        "PILOT107_POSTGRES_DSN_FILE": str(secret_file),
    }

    with pytest.raises(ValueError, match="both inline and file"):
        api_config_from_env(environment, project_root=tmp_path)
    with pytest.raises(ValueError, match="both inline and file"):
        worker_config_from_env(environment, project_root=tmp_path)


def test_sqlite_mode_is_a_rejected_deprecation_sentinel(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match=SQLITE_RETIRED):
        resolve_durable_store_selection(
            database_mode=DatabaseMode.SQLITE,
            sqlite_path=tmp_path / "pilot107.db",
            postgres_dsn=None,
            control_postgres_dsn=None,
        )


def test_postgres_mode_rejects_sqlite_lifecycle_override(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match=SQLITE_RETIRED):
        resolve_durable_store_selection(
            database_mode=DatabaseMode.POSTGRES,
            sqlite_path=tmp_path / "pilot107.db",
            postgres_dsn="postgresql://app@db.internal/pilot107",
            control_postgres_dsn="postgresql://app@db.internal/pilot107",
            runtime_watch_sqlite_path=tmp_path / "watch.db",
        )


def test_postgres_mode_requires_domain_and_control_dsn(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match=SQLITE_RETIRED):
        resolve_durable_store_selection(
            database_mode=DatabaseMode.POSTGRES,
            sqlite_path=tmp_path / "pilot107.db",
            postgres_dsn="postgresql://app@db.internal/pilot107",
            control_postgres_dsn=None,
        )


def test_postgres_mode_rejects_different_database_identity(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="same PostgreSQL database"):
        resolve_durable_store_selection(
            database_mode=DatabaseMode.POSTGRES,
            sqlite_path=tmp_path / "pilot107.db",
            postgres_dsn="postgresql://domain@db.internal/pilot107",
            control_postgres_dsn="postgresql://control@db.internal/control",
        )


def test_postgres_identity_may_use_separate_credentials_without_leaking_them(
    tmp_path: Path,
) -> None:
    selection = resolve_durable_store_selection(
        database_mode=DatabaseMode.POSTGRES,
        sqlite_path=tmp_path / "pilot107.db",
        postgres_dsn="postgresql://domain@DB.internal:5432/pilot107",
        control_postgres_dsn="postgresql://control@db.internal:5432/pilot107",
    )

    assert selection.mode is DatabaseMode.POSTGRES
    assert selection.postgres_dsn is not None
    assert "postgresql" not in repr(selection)


def test_direct_agent_store_builders_have_no_sqlite_fallback(tmp_path: Path) -> None:
    for builder in (
        build_agent_session_store,
        build_project_store,
        build_agent_task_store,
    ):
        with pytest.raises(ConfigurationError, match=SQLITE_RETIRED):
            builder(sqlite_path=tmp_path / "legacy.db", postgres_dsn=None)


def test_control_repository_has_no_sqlite_fallback(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=SQLITE_RETIRED):
        build_control_repository(
            sqlite_path=tmp_path / "legacy.db",
            postgres_dsn=None,
        )


def test_api_and_worker_fail_closed_without_postgres(tmp_path: Path) -> None:
    api = ApiServiceConfig(
        db_path=tmp_path / "api.db",
        evidence_root=tmp_path / "api-evidence",
        capsule_root=tmp_path / "api-capsules",
    )
    worker = WorkerServiceConfig(
        db_path=tmp_path / "worker.db",
        evidence_root=tmp_path / "worker-evidence",
    )

    with pytest.raises(ConfigurationError, match=SQLITE_RETIRED):
        build_api_service(api)
    with pytest.raises(ConfigurationError, match=SQLITE_RETIRED):
        build_worker_service(worker)


def test_postgres_schema_covers_every_a1_lifecycle_table() -> None:
    required = {
        "agent_sessions",
        "agent_turns",
        "agent_tasks",
        "agent_experiment_projects",
        "agent_workspaces",
        "runtime_watches",
        "runtime_log_cursors",
        "runtime_log_segments",
        "resource_observations",
        "observation_cycles",
        "market_application_sessions",
        "template_publication_sessions",
        "repair_tickets",
        "artifact_manifests",
        "ssh_connection_sessions",
    }

    assert required <= set(domain_table_names())
