from __future__ import annotations

from pathlib import Path

import pytest

from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.services.agent_session_service import AgentSessionService


class FailFirstEnqueue:
    def __init__(self, delegate: SQLiteControlRepository) -> None:
        self.delegate = delegate
        self.fail = True

    def enqueue(self, **kwargs):
        if self.fail:
            self.fail = False
            raise RuntimeError("simulated enqueue crash")
        return self.delegate.enqueue(**kwargs)


def _session(store: SQLiteAgentSessionStore):
    return store.create_session(
        owner="alice",
        request_key="session-1",
        profile_id="hpc-readonly-v1",
        model_profile_id="faux-default",
        source={"run_id": "run-1"},
    )[0]


def test_submit_persists_turn_before_idempotent_outbox_enqueue(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    store = SQLiteAgentSessionStore(database)
    control = SQLiteControlRepository(database)
    failing = FailFirstEnqueue(control)
    service = AgentSessionService(store=store, control_repository=failing)
    session = _session(store)

    with pytest.raises(RuntimeError, match="simulated enqueue crash"):
        service.submit_message(
            session_id=session.session_id,
            owner="alice",
            request_key="turn-request-1",
            message="inspect run-1",
            expected_state_version=session.state_version,
        )

    [persisted] = store.list_recoverable_turns(limit=10)
    with pytest.raises(KeyError):
        control.get_outbox(f"agent-turn:{persisted.turn_id}")

    replay, created = service.submit_message(
        session_id=session.session_id,
        owner="alice",
        request_key="turn-request-1",
        message="inspect run-1",
        expected_state_version=session.state_version,
    )

    assert replay.turn_id == persisted.turn_id
    assert created is False
    message = control.get_outbox(f"agent-turn:{persisted.turn_id}")
    assert message.topic == "agent.turn.execute.v1"
    assert message.aggregate_id == persisted.turn_id
    assert message.payload == {"turn_id": persisted.turn_id}


def test_recover_pending_turns_recreates_only_missing_outbox_messages(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agent.db"
    store = SQLiteAgentSessionStore(database)
    control = SQLiteControlRepository(database)
    service = AgentSessionService(store=store, control_repository=control)
    session = _session(store)
    turn, _ = store.create_turn(
        session_id=session.session_id,
        owner="alice",
        request_key="turn-request-1",
        message="inspect run-1",
        expected_state_version=session.state_version,
    )

    assert service.recover_pending_turns(limit=10) == 1
    assert service.recover_pending_turns(limit=10) == 0
    assert control.get_outbox(f"agent-turn:{turn.turn_id}").payload == {"turn_id": turn.turn_id}


def test_experiment_builder_persists_owner_approved_resource_envelope(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agent.db"
    service = AgentSessionService(
        store=SQLiteAgentSessionStore(database),
        control_repository=SQLiteControlRepository(database),
    )
    envelope = {
        "partition": "debug",
        "qos": "normal",
        "cpus": 2,
        "memory_mib": 2048,
        "gpu_type": None,
        "gpus": 0,
        "walltime_seconds": 300,
        "max_tasks": 1,
        "max_submissions": 1,
        "workspace_snapshot_digest": "a" * 64,
        "expires_at": "2026-08-19T01:00:00Z",
        "approved_by": "alice",
    }

    session, _ = service.create_session(
        owner="alice",
        request_key="builder-with-envelope",
        profile_id="experiment_builder",
        model_profile_id="faux-default",
        source={
            "project_id": "project-1",
            "workspace_id": "workspace-1",
            "resource_envelope": envelope,
        },
    )

    assert session.source["resource_envelope"] == envelope
