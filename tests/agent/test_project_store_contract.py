from __future__ import annotations

import os
from pathlib import Path

import pytest

from pilot107.agent.postgres_project_store import PostgresProjectStore
from pilot107.agent.project import (
    ExperimentProjectOrigin,
    ProjectBlueprint,
    ProjectConflict,
    ProjectContractIntent,
    ProjectDependency,
    ProjectExpectedOutput,
    ProjectFile,
    ProjectValidation,
)
from pilot107.agent.project_store import ProjectStore, SQLiteProjectStore
from pilot107.agent.store_factory import build_project_store
from pilot107.agent.workspace import AgentWorkspaceRecord, WorkspaceSnapshot

BLUEPRINT = ProjectBlueprint(
    goal="sum the values in an input table",
    entrypoints=("main.py",),
    files=(
        ProjectFile(path="main.py", purpose="batch entrypoint", classification="editable"),
        ProjectFile(path="data/input.csv", purpose="input table", classification="read_only"),
    ),
    validations=(
        ProjectValidation(
            validation_id="syntax",
            execution="sandbox",
            argv=("python3", "-m", "py_compile", "main.py"),
            expected_outputs=(),
        ),
    ),
    contract_intent=ProjectContractIntent(
        recipe_version_id="recipe_python_cpu@1.0.0",
        resource_hints={"partition": "Students", "cpus_per_task": 1},
    ),
    expected_outputs=(
        ProjectExpectedOutput(path="results/sum.json", kind="json", required=True),
    ),
    dependencies=(
        ProjectDependency(name="python", version="3.12", source="runtime"),
    ),
    open_questions=("Should missing cells be ignored?",),
)


def exercise_project_store_contract(store: ProjectStore) -> None:
    created = store.create_project(
        owner="alice",
        origin=ExperimentProjectOrigin.BLANK,
        goal="sum numbers",
        request_key="project-request-1",
    )
    replayed = store.create_project(
        owner="alice",
        origin=ExperimentProjectOrigin.BLANK,
        goal="sum numbers",
        request_key="project-request-1",
    )
    assert replayed.project_id == created.project_id
    assert replayed.version == created.version

    updated = store.save_blueprint(
        created.project_id,
        "alice",
        created.version,
        BLUEPRINT,
    )
    assert updated.version == created.version + 1
    assert updated.blueprint == BLUEPRINT
    assert updated.state.value == "editing"
    assert store.get_project(created.project_id, owner="alice") == updated

    workspace = AgentWorkspaceRecord(
        workspace_id="workspace-contract",
        project_id=created.project_id,
        owner="alice",
        local_root="/tmp/pilot107-test/workspace-contract",
        snapshot=WorkspaceSnapshot(
            source_ref="/public/home/alice/project",
            digest="a" * 64,
            entries=(),
            captured_at="2026-08-19T00:00:00Z",
        ),
        created_at="2026-08-19T00:00:00Z",
        updated_at="2026-08-19T00:00:00Z",
    )
    assert store.save_workspace(workspace) == workspace
    assert store.get_workspace(workspace.workspace_id, owner="alice") == workspace
    with pytest.raises(KeyError):
        store.get_workspace(workspace.workspace_id, owner="bob")

    with pytest.raises(ProjectConflict):
        store.save_blueprint(
            created.project_id,
            "alice",
            created.version,
            BLUEPRINT,
        )
    with pytest.raises(ProjectConflict):
        store.create_project(
            owner="alice",
            origin=ExperimentProjectOrigin.EXISTING,
            goal="different content",
            request_key="project-request-1",
        )
    with pytest.raises(KeyError):
        store.get_project(created.project_id, owner="bob")

    blocked = store.block_for_model_unavailability(created.project_id, owner="alice")
    replayed_block = store.block_for_model_unavailability(created.project_id, owner="alice")
    assert blocked.state.value == "blocked"
    assert blocked.version == updated.version + 1
    assert replayed_block == blocked


def test_sqlite_project_store_satisfies_contract(tmp_path: Path) -> None:
    exercise_project_store_contract(SQLiteProjectStore(tmp_path / "projects.db"))


def test_sqlite_project_store_survives_reopen(tmp_path: Path) -> None:
    database = tmp_path / "projects.db"
    first = SQLiteProjectStore(database)
    project = first.create_project(
        owner="alice",
        origin="blank",
        goal="persist me",
        request_key=None,
    )
    first.save_blueprint(project.project_id, "alice", project.version, BLUEPRINT)

    reopened = SQLiteProjectStore(database).get_project(project.project_id, owner="alice")

    assert reopened.blueprint == BLUEPRINT
    assert reopened.version == 2


def test_blueprint_rejects_parent_path_traversal() -> None:
    with pytest.raises(ValueError, match="relative project path"):
        ProjectBlueprint(
            goal="unsafe",
            entrypoints=("../escape.py",),
            files=(),
            validations=(),
            contract_intent=ProjectContractIntent(recipe_version_id=None, resource_hints={}),
            expected_outputs=(),
            dependencies=(),
            open_questions=(),
        )


def test_factory_selects_sqlite_project_store(tmp_path: Path) -> None:
    store = build_project_store(sqlite_path=tmp_path / "projects.db", postgres_dsn=None)

    assert isinstance(store, SQLiteProjectStore)


def test_factory_selects_postgres_project_store(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    monkeypatch.setattr(
        "pilot107.agent.store_factory.PostgresProjectStore",
        lambda dsn: sentinel,
    )

    store = build_project_store(
        sqlite_path=Path("unused.db"),
        postgres_dsn="postgresql://project-store-test",
    )

    assert store is sentinel


@pytest.mark.skipif(
    not os.environ.get("PILOT107_TEST_POSTGRES_DSN")
    or os.environ.get("PILOT107_TEST_POSTGRES_ALLOW_RESET") != "1",
    reason="set a dedicated PostgreSQL DSN and explicit reset opt-in",
)
def test_postgres_project_store_satisfies_contract() -> None:
    dsn = os.environ["PILOT107_TEST_POSTGRES_DSN"]
    store = PostgresProjectStore(dsn)
    with store.connect() as connection:
        connection.execute("TRUNCATE agent_experiment_projects CASCADE")

    exercise_project_store_contract(store)
