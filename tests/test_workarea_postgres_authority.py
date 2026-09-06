from __future__ import annotations

import os
from uuid import uuid4

import pytest

from pilot107.core.workarea import PostgresWorkAreaStore

pytestmark = pytest.mark.skipif(
    not os.environ.get("PILOT107_TEST_POSTGRES_DSN")
    or os.environ.get("PILOT107_TEST_POSTGRES_ALLOW_RESET") != "1",
    reason="set a dedicated PostgreSQL DSN and explicit reset opt-in",
)


def test_workarea_is_postgres_only_research_boundary() -> None:
    dsn = os.environ["PILOT107_TEST_POSTGRES_DSN"]
    store = PostgresWorkAreaStore(dsn)
    nonce = uuid4().hex
    owner = "workarea_test"

    area = store.create(
        owner=owner,
        request_key=f"authority-{nonce}",
        title="PostgreSQL authority",
        description="canonical research boundary",
    )
    store.link_asset(
        area.workarea_id,
        owner=owner,
        asset_ref=f"dataset:{nonce}",
        asset_kind="dataset",
    )

    graph = store.graph(area.workarea_id, owner=owner)

    assert graph.workarea == area
    assert graph.contract_ids == ()
    assert graph.run_ids == ()
    assert graph.agent_project_ids == ()
    assert [(item.asset_ref, item.asset_kind) for item in graph.assets] == [
        (f"dataset:{nonce}", "dataset")
    ]


def test_workarea_migration_owns_current_table_names() -> None:
    dsn = os.environ["PILOT107_TEST_POSTGRES_DSN"]
    store = PostgresWorkAreaStore(dsn)

    with store.connect() as connection:
        names = connection.execute(
            """
            SELECT
                to_regclass('public.workareas')::text AS workareas,
                to_regclass('public.workarea_contracts')::text AS contracts,
                to_regclass('public.workarea_runs')::text AS runs,
                to_regclass('public.workarea_agent_projects')::text AS projects,
                to_regclass('public.workarea_assets')::text AS assets
            """
        ).fetchone()
        migration = connection.execute(
            """
            SELECT checksum FROM schema_migrations
            WHERE migration_id = '006c.004.workarea_terminology'
            """
        ).fetchone()

    assert names == {
        "workareas": "workareas",
        "contracts": "workarea_contracts",
        "runs": "workarea_runs",
        "projects": "workarea_agent_projects",
        "assets": "workarea_assets",
    }
    assert migration is not None
