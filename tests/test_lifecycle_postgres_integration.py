from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pilot107.adapters.slurm import JobSnapshot, SubmissionStrategy, SubmitReceipt
from pilot107.adapters.ssh_relay import (
    SshRelayCheck,
    SshRelayConfig,
    SshSessionState,
)
from pilot107.agent.market_sessions import (
    MarketApplicationSourceKind,
    MarketAssurance,
)
from pilot107.agent.postgres_project_store import PostgresProjectStore
from pilot107.agent.postgres_store import PostgresAgentSessionStore
from pilot107.agent.postgres_task_store import PostgresAgentTaskStore
from pilot107.agent.store_factory import (
    DatabaseMode,
    build_market_session_store,
    resolve_durable_store_selection,
)
from pilot107.api.service import ApiServiceConfig, build_api_service
from pilot107.core.postgres_control_repository import PostgresControlRepository
from pilot107.core.postgres_domain_schema import (
    initialize_postgres_domain_schema,
    persisted_table_names,
)
from pilot107.core.postgres_domain_stores import (
    PostgresMarketSessionStore,
    PostgresRemediationStore,
    PostgresRepairTicketStore,
    PostgresRunPublicationStore,
    PostgresRunStore,
    PostgresSshConnectionStore,
    PostgresTemplateMarketStore,
)
from pilot107.core.repair_ticket import (
    ArtifactManifest,
    RepairTicket,
    RepairTicketState,
)
from pilot107.core.run_publications import (
    RunPublicationShareManifest,
    RunPublicationVisibility,
)
from pilot107.core.states import RunState
from pilot107.observability.postgres_store import PostgresObservabilityStore
from pilot107.runtime_watch.model import (
    RuntimeLogSegmentDraft,
    RuntimeWatchConflict,
    RuntimeWatchState,
)
from pilot107.runtime_watch.postgres_store import PostgresRuntimeWatchStore
from pilot107.worker.service import WorkerServiceConfig, build_worker_service

pytestmark = pytest.mark.skipif(
    not os.environ.get("PILOT107_TEST_POSTGRES_DSN")
    or os.environ.get("PILOT107_TEST_POSTGRES_ALLOW_RESET") != "1",
    reason="set a dedicated PostgreSQL DSN and explicit reset opt-in",
)


@pytest.fixture()
def postgres_runtime(tmp_path: Path):
    dsn = os.environ["PILOT107_TEST_POSTGRES_DSN"]
    initialize_postgres_domain_schema(dsn)
    PostgresControlRepository(dsn)
    store = PostgresRunStore(dsn, compatibility_path=tmp_path / "compat.db")
    with store.connect() as connection:
        connection.execute(
            "TRUNCATE " + ", ".join(reversed(persisted_table_names())) + " RESTART IDENTITY CASCADE"
        )
    return dsn, store


def test_migration_replay_and_market_session_restart(postgres_runtime, tmp_path: Path) -> None:
    dsn, runs = postgres_runtime
    initialize_postgres_domain_schema(dsn)
    selection = resolve_durable_store_selection(
        database_mode=DatabaseMode.POSTGRES,
        sqlite_path=tmp_path / "unused.db",
        postgres_dsn=dsn,
        control_postgres_dsn=dsn,
    )
    first = build_market_session_store(selection=selection)
    created = first.create_market_application(
        owner="alice",
        request_key="postgres-market-restart",
        source_kind=MarketApplicationSourceKind.RUN_PUBLICATION,
        source_item_id="runpub_postgres",
        source_digest="sha256:" + "a" * 64,
        assurance=MarketAssurance.REFERENCE_ONLY,
        user_intent="adapt exact shared Contract",
        detail={"plan_digest": "sha256:" + "b" * 64},
    )

    restarted = build_market_session_store(selection=selection)
    assert (
        restarted.get_market_application(
            created.session_id,
            owner="alice",
        )
        == created
    )

    successful = runs.create_run(
        run_id="run_postgres_share_manifest",
        owner="alice",
        workdir="/public/home/alice/project",
        script="echo postgres",
    )
    runs.apply_submit_receipt(
        successful.run_id,
        SubmitReceipt(
            job_id="4107",
            run_state=RunState.SUBMITTED,
            strategy=SubmissionStrategy.COMMAND,
        ),
    )
    runs.apply_snapshot(
        successful.run_id,
        JobSnapshot(
            job_id="4107",
            owner="alice",
            run_state=RunState.SUCCEEDED,
            raw_state_flags=["COMPLETED"],
            exit_code="0:0",
        ),
    )
    manifest = RunPublicationShareManifest(
        title="PostgreSQL share manifest",
        visibility=RunPublicationVisibility.CAMPUS,
        result_summary=True,
    )
    publications = PostgresRunPublicationStore(
        dsn,
        compatibility_path=tmp_path / "compat.db",
        run_store=runs,
    )
    publication = publications.publish(
        source_run_id=successful.run_id,
        owner="alice",
        title=manifest.title,
        description="field-selected publication",
        visibility=manifest.visibility,
        scope_key=None,
        request_key="postgres-share-manifest",
        confirmed=True,
        share_manifest=manifest,
    )
    assert publications.get(publication.publication_id).share_manifest_digest == (
        manifest.manifest_digest
    )

    repair_tickets = PostgresRepairTicketStore(
        dsn,
        compatibility_path=tmp_path / "compat.db",
    )
    artifact = repair_tickets.create_manifest(
        ArtifactManifest(
            manifest_id="artifact_postgres_restart",
            owner="alice",
            run_id=successful.run_id,
            revision="sha256:revision",
        )
    )
    ticket = repair_tickets.create_ticket(
        RepairTicket(
            ticket_id="repair_postgres_restart",
            owner="alice",
            state=RepairTicketState.OPEN,
            source_run_id=successful.run_id,
        )
    )
    assert repair_tickets.get_manifest(artifact.manifest_id) == artifact
    assert repair_tickets.get_ticket(ticket.ticket_id) == ticket

    ssh_config = SshRelayConfig(
        connection_id="real107-postgres",
        target_id="real107",
        target="login.real107",
        control_path=Path("/tmp/real107-postgres.sock"),
        portal_owner="alice",
        slurm_user="alice",
        owner_roots=("/public/home/{user}",),
    )
    ssh_store = PostgresSshConnectionStore(
        dsn,
        compatibility_path=tmp_path / "compat.db",
    )
    ssh_record = ssh_store.save_check(
        config=ssh_config,
        check=SshRelayCheck(
            state=SshSessionState.ACTIVE,
            checked_at=datetime.now(UTC).isoformat(),
            status_code="SSH.ACTIVE",
            message="active",
        ),
    )
    assert (
        PostgresSshConnectionStore(
            dsn,
            compatibility_path=tmp_path / "compat.db",
        ).get(ssh_record.connection_id)
        == ssh_record
    )


def test_four_workers_fence_stale_runtime_watch_and_replay_cursors(
    postgres_runtime,
    tmp_path: Path,
) -> None:
    dsn, runs = postgres_runtime
    run = runs.create_run(
        run_id="run_postgres_four_workers",
        owner="alice",
        workdir="/public/home/alice/project",
        script="echo postgres",
    )
    now = datetime.now(UTC)
    watch_store = PostgresRuntimeWatchStore(
        dsn,
        segment_root=tmp_path / "segments",
        clock=lambda: now,
    )
    watch = watch_store.create_watch(
        run_id=run.run_id,
        owner="alice",
        connection_id="postgres-test",
    )

    def claim(worker: str):
        return watch_store.claim_watch(
            watch.watch_id,
            owner="alice",
            worker_id=worker,
            lease_seconds=30,
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        claims = list(executor.map(claim, ("w1", "w2", "w3", "w4")))
    leases = [lease for lease in claims if lease is not None]
    assert len(leases) == 1
    stale = leases[0]

    later_store = PostgresRuntimeWatchStore(
        dsn,
        segment_root=tmp_path / "segments",
        clock=lambda: now + timedelta(seconds=31),
    )
    fresh = later_store.claim_watch(
        watch.watch_id,
        owner="alice",
        worker_id="fresh",
        lease_seconds=30,
    )
    assert fresh is not None
    with pytest.raises(RuntimeWatchConflict, match="stale|fenced"):
        watch_store.release_watch(
            stale,
            state=RuntimeWatchState.ACTIVE,
            next_poll_at=(now + timedelta(seconds=1)).isoformat(),
        )

    cursor = later_store.get_cursor(run.run_id, "alice", "stdout")
    content = b"log\n"
    advanced = replace(
        cursor,
        source_size=len(content),
        offset=len(content),
        last_data_at=(now + timedelta(seconds=31)).isoformat(),
        last_checked_at=(now + timedelta(seconds=31)).isoformat(),
        version=cursor.version + 1,
    )
    segment = later_store.commit_segment(
        lease=fresh,
        segment=RuntimeLogSegmentDraft(
            run_id=run.run_id,
            owner="alice",
            stream="stdout",
            generation=0,
            start_offset=0,
            content=content,
        ),
        next_cursor=advanced,
    )
    later_store.release_watch(
        fresh,
        state=RuntimeWatchState.ACTIVE,
        next_poll_at=(now + timedelta(seconds=32)).isoformat(),
    )
    restarted = PostgresRuntimeWatchStore(
        dsn,
        segment_root=tmp_path / "segments",
        clock=lambda: now + timedelta(seconds=31),
    )
    assert restarted.get_watch(watch.watch_id, owner="alice").state is RuntimeWatchState.ACTIVE
    replayed_cursor = restarted.get_cursor(run.run_id, "alice", "stdout")
    assert (
        replayed_cursor.generation,
        replayed_cursor.offset,
        replayed_cursor.source_size,
        replayed_cursor.version,
    ) == (0, len(content), len(content), 1)
    assert restarted.read_segment_content(segment.segment_id, owner="alice") == content
    restarted_runs = PostgresRunStore(dsn, compatibility_path=tmp_path / "compat.db")
    events = restarted_runs.list_events(run.run_id)
    assert [event.event_id for event in events] == sorted(event.event_id for event in events)


def test_api_and_worker_restart_on_one_postgres_identity(
    postgres_runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn, _runs = postgres_runtime

    def unavailable_snapshot(_collector):
        raise RuntimeError("snapshot disabled for store integration")

    monkeypatch.setattr(
        "pilot107.api.service.SlurmrestSnapshotCollector.collect",
        unavailable_snapshot,
    )
    monkeypatch.setattr("pilot107.api.service.threading.Thread.start", lambda _thread: None)
    api_config = ApiServiceConfig(
        db_path=tmp_path / "api-compat.db",
        evidence_root=tmp_path / "api-evidence",
        capsule_root=tmp_path / "api-capsules",
        database_mode=DatabaseMode.POSTGRES,
        postgres_dsn=dsn,
        control_postgres_dsn=dsn,
        backend="demo",
        allowed_roots=("/public/home/{user}",),
        agent_a1_enabled=True,
        agent_capability_hmac_secret=b"a" * 32,
    )

    first_api = build_api_service(api_config)
    restarted_api = build_api_service(api_config)
    for api in (first_api, restarted_api):
        assert isinstance(api.store, PostgresRunStore)
        assert isinstance(api.control_repository, PostgresControlRepository)
        assert isinstance(api.template_market_store, PostgresTemplateMarketStore)
        assert isinstance(
            api.market_session_routes.applications.store,
            PostgresMarketSessionStore,
        )
        assert isinstance(
            api.repair_ticket_service.repair_ticket_store,
            PostgresRepairTicketStore,
        )
        assert isinstance(api.project_agent_routes.service.store, PostgresProjectStore)
        assert isinstance(
            api.agent_session_routes.service.store,
            PostgresAgentSessionStore,
        )
        assert isinstance(api.runtime_watch_routes.store, PostgresRuntimeWatchStore)
        assert isinstance(
            api.observability_routes.service.store,
            PostgresObservabilityStore,
        )

    worker_config = WorkerServiceConfig(
        db_path=tmp_path / "worker-compat.db",
        evidence_root=tmp_path / "worker-evidence",
        database_mode=DatabaseMode.POSTGRES,
        postgres_dsn=dsn,
        control_postgres_dsn=dsn,
        backend="demo",
        allowed_roots=("/public/home/{user}",),
        auto_capsule_enabled=False,
        agent_a1_enabled=True,
        agent_capability_hmac_secret=b"b" * 32,
        agentd_url="http://127.0.0.1:9",
        agentd_token="integration-only",
        agentd_model_profile="integration",
    )

    first_worker = build_worker_service(worker_config)
    restarted_worker = build_worker_service(worker_config)
    for worker in (first_worker, restarted_worker):
        assert isinstance(worker.stack.store, PostgresRunStore)
        assert isinstance(
            worker.stack.remediation_service.remediation_store,
            PostgresRemediationStore,
        )
        assert isinstance(
            worker.stack.agent_session_service.store,
            PostgresAgentSessionStore,
        )
        assert isinstance(worker.stack.agent_task_service.store, PostgresAgentTaskStore)
