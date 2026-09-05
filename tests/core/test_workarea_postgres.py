from __future__ import annotations

import os
from dataclasses import fields
from datetime import UTC, datetime

import pytest

from pilot107.agent.postgres_project_store import PostgresProjectStore
from pilot107.agent.project import ExperimentProjectOrigin
from pilot107.core.workarea import (
    PostgresWorkAreaStore,
    WorkAreaConflict,
    WorkAreaRecord,
)


PG_ENABLED = bool(
    os.environ.get("PILOT107_TEST_POSTGRES_DSN")
    and os.environ.get("PILOT107_TEST_POSTGRES_ALLOW_RESET") == "1"
)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _reset(store: PostgresWorkAreaStore) -> None:
    with store.connect() as connection:
        connection.execute(
            "TRUNCATE workarea_assets, workarea_agent_projects, workarea_runs, "
            "workarea_contracts, workareas, agent_experiment_projects, contracts, runs "
            "RESTART IDENTITY CASCADE"
        )


def _insert_contract(
    store: PostgresWorkAreaStore,
    *,
    contract_id: str,
    owner: str,
) -> None:
    now = _now()
    with store.connect() as connection:
        connection.execute(
            """
            INSERT INTO contracts (
                contract_id, owner, recipe_version_id, payload_json,
                field_sources_json, schema_version, digest, created_at, updated_at
            ) VALUES (%s, %s, 'recipe:test@1', '{}', '[]',
                      'pilot107.contract/v1', %s, %s, %s)
            """,
            (contract_id, owner, f"digest-{contract_id}", now, now),
        )


def _insert_run(
    store: PostgresWorkAreaStore,
    *,
    run_id: str,
    owner: str,
    parent_run_id: str | None = None,
    lineage_reason: str | None = None,
) -> None:
    now = _now()
    with store.connect() as connection:
        connection.execute(
            """
            INSERT INTO runs (
                run_id, parent_run_id, lineage_reason, owner,
                state, collection_state, diagnosis_state, capsule_state,
                result_status, workdir, script, created_at, updated_at
            ) VALUES (%s, %s, %s, %s,
                      'created', 'pending', 'pending', 'pending',
                      'unknown', %s, %s, %s, %s)
            """,
            (
                run_id,
                parent_run_id,
                lineage_reason,
                owner,
                f"/tmp/{run_id}",
                "#!/bin/bash\ntrue\n",
                now,
                now,
            ),
        )


def test_workarea_record_is_boundary_not_lifecycle_state() -> None:
    field_names = {field.name for field in fields(WorkAreaRecord)}

    assert field_names == {
        "workarea_id",
        "owner",
        "request_key",
        "title",
        "description",
        "created_at",
        "updated_at",
    }
    assert not {
        "state",
        "stage",
        "coarse_state",
        "active_run_id",
        "active_contract_id",
        "next_action",
        "workspace_revision",
        "workspace_digest",
    } & field_names


@pytest.mark.skipif(
    not PG_ENABLED,
    reason="set a dedicated PostgreSQL DSN and explicit reset opt-in",
)
def test_workarea_schema_reserves_workspace_terminology_for_agent() -> None:
    store = PostgresWorkAreaStore(os.environ["PILOT107_TEST_POSTGRES_DSN"])
    _reset(store)

    with store.connect() as connection:
        row = connection.execute(
            """
            SELECT
                to_regclass('public.workareas') AS workareas,
                to_regclass('public.research_workspaces') AS legacy_research_workspaces
            """
        ).fetchone()
        run_columns = connection.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'workarea_runs'
            ORDER BY ordinal_position
            """
        ).fetchall()

    assert row["workareas"] == "workareas"
    assert row["legacy_research_workspaces"] is None
    assert [item["column_name"] for item in run_columns] == [
        "workarea_id",
        "run_id",
        "linked_at",
    ]


@pytest.mark.skipif(
    not PG_ENABLED,
    reason="set a dedicated PostgreSQL DSN and explicit reset opt-in",
)
def test_contract_and_asset_can_be_reused_across_workareas() -> None:
    store = PostgresWorkAreaStore(os.environ["PILOT107_TEST_POSTGRES_DSN"])
    _reset(store)
    area_a = store.create(owner="alice", request_key="area-a", title="A")
    area_b = store.create(owner="alice", request_key="area-b", title="B")
    _insert_contract(store, contract_id="contract-shared", owner="alice")

    for area in (area_a, area_b):
        store.link_contract(area.workarea_id, owner="alice", contract_id="contract-shared")
        store.link_asset(
            area.workarea_id,
            owner="alice",
            asset_ref="dataset://shared/calibration-v1",
            asset_kind="dataset",
        )

    assert store.graph(area_a.workarea_id, owner="alice").contract_ids == (
        "contract-shared",
    )
    assert store.graph(area_b.workarea_id, owner="alice").contract_ids == (
        "contract-shared",
    )
    assert store.graph(area_a.workarea_id, owner="alice").assets[0].asset_ref == (
        "dataset://shared/calibration-v1"
    )
    assert store.graph(area_b.workarea_id, owner="alice").assets[0].asset_ref == (
        "dataset://shared/calibration-v1"
    )


@pytest.mark.skipif(
    not PG_ENABLED,
    reason="set a dedicated PostgreSQL DSN and explicit reset opt-in",
)
def test_run_has_one_primary_workarea_but_lineage_may_cross_workareas() -> None:
    store = PostgresWorkAreaStore(os.environ["PILOT107_TEST_POSTGRES_DSN"])
    _reset(store)
    area_a = store.create(owner="alice", request_key="area-a", title="A")
    area_b = store.create(owner="alice", request_key="area-b", title="B")
    _insert_run(store, run_id="run-parent", owner="alice")
    _insert_run(
        store,
        run_id="run-child",
        owner="alice",
        parent_run_id="run-parent",
        lineage_reason="forked-hypothesis",
    )

    store.link_run(area_a.workarea_id, owner="alice", run_id="run-parent")
    # Idempotent replay on the same boundary is safe.
    store.link_run(area_a.workarea_id, owner="alice", run_id="run-parent")
    # Scientific lineage is a DAG edge and is allowed to cross WorkArea boundaries.
    store.link_run(area_b.workarea_id, owner="alice", run_id="run-child")

    assert store.get_run_workarea("run-parent", owner="alice") == area_a
    assert store.get_run_workarea("run-child", owner="alice") == area_b
    with pytest.raises(WorkAreaConflict, match="another WorkArea"):
        store.link_run(area_b.workarea_id, owner="alice", run_id="run-parent")


@pytest.mark.skipif(
    not PG_ENABLED,
    reason="set a dedicated PostgreSQL DSN and explicit reset opt-in",
)
def test_agent_project_has_one_primary_workarea_and_owner_is_enforced() -> None:
    dsn = os.environ["PILOT107_TEST_POSTGRES_DSN"]
    store = PostgresWorkAreaStore(dsn)
    _reset(store)
    projects = PostgresProjectStore(dsn)
    area_a = store.create(owner="alice", request_key="area-a", title="A")
    area_b = store.create(owner="alice", request_key="area-b", title="B")
    alice_project = projects.create_project(
        owner="alice",
        origin=ExperimentProjectOrigin.BLANK,
        goal="repair experiment",
        request_key="project-a",
    )
    bob_project = projects.create_project(
        owner="bob",
        origin=ExperimentProjectOrigin.BLANK,
        goal="other owner's experiment",
        request_key="project-b",
    )

    store.link_agent_project(
        area_a.workarea_id,
        owner="alice",
        project_id=alice_project.project_id,
    )
    store.link_agent_project(
        area_a.workarea_id,
        owner="alice",
        project_id=alice_project.project_id,
    )

    with pytest.raises(WorkAreaConflict, match="another WorkArea"):
        store.link_agent_project(
            area_b.workarea_id,
            owner="alice",
            project_id=alice_project.project_id,
        )
    with pytest.raises(WorkAreaConflict, match="owner"):
        store.link_agent_project(
            area_a.workarea_id,
            owner="alice",
            project_id=bob_project.project_id,
        )


@pytest.mark.skipif(
    not PG_ENABLED,
    reason="set a dedicated PostgreSQL DSN and explicit reset opt-in",
)
def test_workarea_rejects_cross_owner_contract_and_run_edges() -> None:
    store = PostgresWorkAreaStore(os.environ["PILOT107_TEST_POSTGRES_DSN"])
    _reset(store)
    area = store.create(owner="alice", request_key="area-a", title="A")
    _insert_contract(store, contract_id="contract-bob", owner="bob")
    _insert_run(store, run_id="run-bob", owner="bob")

    with pytest.raises(WorkAreaConflict, match="Contract owner"):
        store.link_contract(area.workarea_id, owner="alice", contract_id="contract-bob")
    with pytest.raises(WorkAreaConflict, match="Run owner"):
        store.link_run(area.workarea_id, owner="alice", run_id="run-bob")
