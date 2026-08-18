from __future__ import annotations

import json
import resource
import subprocess
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pilot107.agent.capabilities import AgentCapabilitySigner
from pilot107.agent.protocol import (
    AgentdClientError,
    AgentTurnEvent,
    DurableAgentTurnRequest,
    ToolInvocation,
)
from pilot107.agent.read_tools import AgentReadContext, build_a1_read_handlers
from pilot107.agent.session import AgentTurnState
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.agent.tool_gateway import AgentToolGateway
from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.http_app import Pilot107HttpApi
from pilot107.core.code_context import LocalWorkspaceReader
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.core.platform_snapshot import (
    ObservationSourceType,
    PlatformSnapshot,
    PlatformSnapshotScope,
)
from pilot107.core.platform_snapshot_store import PlatformSnapshotStore
from pilot107.core.run_store import RunStore
from pilot107.services.agent_session_service import AgentSessionService
from pilot107.worker.agent_turn_worker import AgentTurnWorker
from pilot107.worker.evidence import EvidenceStore

_SECRET = b"a1-vertical-capability-secret-value"
_TOOLS = ("run_get", "run_log_read", "evidence_read")


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 19, tzinfo=UTC)

    def datetime(self) -> datetime:
        return self.value

    def epoch(self) -> int:
        return int(self.value.timestamp())

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class GatewayBackedFauxAgentd:
    """Deterministic Agentd boundary that executes the real Python Gateway."""

    def __init__(
        self,
        gateway: AgentToolGateway,
        *,
        evidence_objects: dict[str, str],
        fail_after_tools: int | None = None,
        start_barrier: threading.Barrier | None = None,
    ) -> None:
        self.gateway = gateway
        self.evidence_objects = evidence_objects
        self.fail_after_tools = fail_after_tools
        self.start_barrier = start_barrier
        self.invocations: list[ToolInvocation] = []
        self.bytes_returned: list[int] = []
        self.started_at: dict[str, float] = {}
        self.finished_at: dict[str, float] = {}
        self.cancelled: list[str] = []
        self._active: set[str] = set()
        self.peak_active = 0
        self._lock = threading.Lock()

    @property
    def active_turns(self) -> int:
        with self._lock:
            return len(self._active)

    def stream_durable_turn(
        self,
        request: DurableAgentTurnRequest,
        on_event=None,
    ) -> Iterator[AgentTurnEvent]:
        del on_event
        with self._lock:
            self._active.add(request.turn_id)
            self.peak_active = max(self.peak_active, len(self._active))
            self.started_at[request.turn_id] = time.monotonic()
        sequence = 1
        try:
            if self.start_barrier is not None:
                self.start_barrier.wait(timeout=5)
            yield _event(
                request.turn_id,
                sequence,
                "turn_started",
                {"model_profile_id": request.model_profile_id, "task_kind": "interactive"},
            )
            run_id = next(
                ref.removeprefix("run:")
                for ref in request.context_refs
                if ref.startswith("run:")
            )
            arguments = (
                {"run_id": run_id},
                {"run_id": run_id, "stream": "stderr", "cursor": 0},
                {"run_id": run_id, "object_id": self.evidence_objects[run_id]},
            )
            for index, (tool_name, tool_arguments) in enumerate(
                zip(_TOOLS, arguments, strict=True), start=1
            ):
                sequence += 1
                yield _event(
                    request.turn_id,
                    sequence,
                    "tool_call_requested",
                    {"tool_call_id": f"call-{index}", "tool_name": tool_name},
                )
                sequence += 1
                yield _event(
                    request.turn_id,
                    sequence,
                    "tool_call_started",
                    {"tool_call_id": f"call-{index}", "tool_name": tool_name},
                )
                invocation = ToolInvocation(
                    schema_version="pilot107.agent-tool-invocation/v1",
                    invocation_id=f"inv-{request.turn_id}-{index}",
                    idempotency_key=f"idem-{request.turn_id}-{index}",
                    owner=request.owner,
                    session_id=request.session_id,
                    turn_id=request.turn_id,
                    state_version=request.state_version,
                    profile_id="hpc-readonly-v1",
                    tool_name=tool_name,
                    arguments=tool_arguments,
                    deadline="2099-01-01T00:00:00Z",
                )
                result = self.gateway.invoke(request.capability_token, invocation)
                with self._lock:
                    self.invocations.append(invocation)
                    self.bytes_returned.append(result.bytes_returned)
                sequence += 1
                yield _event(
                    request.turn_id,
                    sequence,
                    "tool_call_completed",
                    {
                        "tool_call_id": f"call-{index}",
                        "tool_name": tool_name,
                        "result": result.result,
                        "is_error": False,
                    },
                )
                if self.fail_after_tools == index:
                    raise AgentdClientError(
                        "pilot-agentd transport failed",
                        code="transport_error",
                        retryable=True,
                    )
            sequence += 1
            yield _event(
                request.turn_id,
                sequence,
                "message_delta",
                {"delta": f"{run_id} failed after bounded evidence review."},
            )
            checkpoint = {
                "schema_version": "pilot107.agent-checkpoint/v1",
                "turn_id": request.turn_id,
                "digest": "a" * 64,
                "messages": [],
            }
            sequence += 1
            yield _event(
                request.turn_id,
                sequence,
                "checkpoint",
                {"checkpoint": checkpoint},
            )
            sequence += 1
            yield _event(
                request.turn_id,
                sequence,
                "turn_completed",
                {
                    "result": f"{run_id} failed after bounded evidence review.",
                    "provider": "faux-default",
                    "model": "faux-1",
                    "model_profile_id": request.model_profile_id,
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 8,
                        "cache_read_tokens": 0,
                        "cache_write_tokens": 0,
                    },
                    "provider_calls": 4,
                    "checkpoint_digest": "a" * 64,
                    "duration_ms": 5,
                    "checkpoint": checkpoint,
                },
            )
        finally:
            with self._lock:
                self._active.discard(request.turn_id)
                self.finished_at[request.turn_id] = time.monotonic()

    def cancel_turn(self, turn_id: str) -> str:
        self.cancelled.append(turn_id)
        return "accepted"


def _event(turn_id: str, sequence: int, event_type: str, payload: dict) -> AgentTurnEvent:
    return AgentTurnEvent(
        turn_id=turn_id,
        sequence=sequence,
        type=event_type,
        timestamp=f"2026-08-19T00:00:{sequence:02d}Z",
        payload=payload,
    )


def _seed_run(
    run_store: RunStore,
    evidence_store: EvidenceStore,
    *,
    owner: str,
    run_id: str,
    object_id: str,
    workspace: Path,
) -> None:
    run_store.create_run(
        run_id=run_id,
        owner=owner,
        workdir=str(workspace),
        script="#!/bin/bash\nexit 1\n",
    )
    artifact = evidence_store.write_text(
        run_id=run_id,
        logical_path="logs/stderr.txt",
        content="ModuleNotFoundError: no module named torch\n",
        content_type="text/plain",
    )
    run_store.upsert_evidence_objects(
        run_id,
        [
            {
                "object_id": object_id,
                "category": "logs",
                "logical_path": artifact.logical_path,
                "store_path": str(artifact.path),
                "source_uri": f"evidence://{run_id}/{artifact.logical_path}",
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
                "mime_type": artifact.content_type,
                "collection_status": "collected",
                "mutable_during_run": False,
            }
        ],
    )


def _workspace(root: Path) -> Path:
    workspace = root / "workspaces" / "alice" / "project"
    workspace.mkdir(parents=True)
    (workspace / "train.py").write_text("import torch\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "add", "train.py"], check=True)
    return workspace


def _stack(tmp_path: Path, *, clock: MutableClock | None = None):
    database = tmp_path / "pilot107.db"
    store_clock = None if clock is None else clock.datetime
    agent_store = SQLiteAgentSessionStore(database, clock=store_clock)
    control = SQLiteControlRepository(database, clock=store_clock)
    run_store = RunStore(database)
    evidence_store = EvidenceStore(tmp_path / "evidence")
    platform_store = PlatformSnapshotStore(database)
    workspace = _workspace(tmp_path)
    platform_store.create(
        owner="alice",
        snapshot=PlatformSnapshot(
            snapshot_id="snapshot-a1-smoke",
            scope=PlatformSnapshotScope.SIMULATOR,
            captured_at="2026-08-19T00:00:00+00:00",
            collector_version="a1-smoke",
        ),
        source_type=ObservationSourceType.SIMULATOR,
        source_name="a1-smoke",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    _seed_run(
        run_store,
        evidence_store,
        owner="alice",
        run_id="run-a1-smoke",
        object_id="object-a1-smoke",
        workspace=workspace,
    )
    signer = AgentCapabilitySigner(
        _SECRET,
        **({} if clock is None else {"clock": clock.epoch}),
    )
    gateway = AgentToolGateway(
        store=agent_store,
        signer=signer,
        handlers=build_a1_read_handlers(
            AgentReadContext(
                platform_snapshot_store=platform_store,
                run_store=run_store,
                evidence_query=EvidenceQueryService(
                    store=run_store,
                    evidence_store=evidence_store,
                ),
                workspace_reader=LocalWorkspaceReader(
                    allowed_roots=(tmp_path / "workspaces",)
                ),
                workspace_root_templates=(str(tmp_path / "workspaces" / "{user}"),),
            )
        ),
        **({} if clock is None else {"clock": clock.datetime}),
    )
    service = AgentSessionService(store=agent_store, control_repository=control)
    api = Pilot107HttpApi(
        store=run_store,
        evidence_query=EvidenceQueryService(
            store=run_store,
            evidence_store=evidence_store,
        ),
        agent_session_service=service,
        auth_required=True,
    )
    return agent_store, control, run_store, evidence_store, signer, gateway, service, api


def _submit(api: Pilot107HttpApi, *, owner: str = "alice", suffix: str = "smoke"):
    headers = {"X-Pilot107-User": owner}
    created = api.handle_post(
        "/api/v1/agent-sessions",
        body=json.dumps(
            {
                "request_key": f"session-{suffix}",
                "model_profile_id": "faux-default",
                "source": {"run_id": "run-a1-smoke"},
            }
        ).encode(),
        headers=headers,
    )
    turn = api.handle_post(
        f"/api/v1/agent-sessions/{created.payload['session_id']}/turns",
        body=json.dumps(
            {
                "request_key": f"turn-{suffix}",
                "message": "inspect the failed run, stderr, and evidence",
                "expected_state_version": created.payload["state_version"],
            }
        ).encode(),
        headers=headers,
    )
    return created, turn


def test_http_to_worker_gateway_store_vertical_and_owner_isolation(tmp_path: Path) -> None:
    agent_store, control, _, _, signer, gateway, _, api = _stack(tmp_path)
    session, turn = _submit(api)
    client = GatewayBackedFauxAgentd(
        gateway,
        evidence_objects={"run-a1-smoke": "object-a1-smoke"},
    )
    result = AgentTurnWorker(
        store=agent_store,
        control_repository=control,
        agentd_client=client,
        capability_signer=signer,
        worker_id="a1-vertical-worker",
    ).dispatch_due(limit=1)

    events = api.handle_get(
        f"/api/v1/agent-sessions/{session.payload['session_id']}/events",
        headers={"X-Pilot107-User": "alice"},
    )
    bob_session = api.handle_get(
        f"/api/v1/agent-sessions/{session.payload['session_id']}",
        headers={"X-Pilot107-User": "bob"},
    )
    bob_run = api.handle_get(
        "/api/v1/runs/run-a1-smoke",
        headers={"X-Pilot107-User": "bob"},
    )

    assert result.succeeded == 1
    assert agent_store.get_turn(turn.payload["turn_id"], owner="alice").state is (
        AgentTurnState.COMPLETED
    )
    assert [invocation.tool_name for invocation in client.invocations] == list(_TOOLS)
    assert [item["sequence"] for item in events.payload["items"]] == list(range(1, 14))
    assert events.payload["items"][-1]["event_type"] == "turn_completed"
    assert bob_session.status == 404
    assert bob_run.status in {403, 404}
    assert "ModuleNotFoundError" not in repr(bob_run.payload)
    assert sum(client.bytes_returned) <= 1024 * 1024
    with agent_store.connect() as connection:
        rows = connection.execute(
            "SELECT idempotency_key, COUNT(*) AS copies FROM agent_tool_invocations "
            "GROUP BY idempotency_key"
        ).fetchall()
    assert len(rows) == 3
    assert all(int(row["copies"]) == 1 for row in rows)


def test_fault_after_outbox_claim_is_reclaimed_without_duplicate_turn(tmp_path: Path) -> None:
    clock = MutableClock()
    agent_store, control, _, _, signer, gateway, _, api = _stack(tmp_path, clock=clock)
    _, turn = _submit(api, suffix="outbox-crash")
    message_id = f"agent-turn:{turn.payload['turn_id']}"
    claimed = control.claim_outbox(
        owner="crashed-worker",
        limit=1,
        lease_seconds=1,
        topics=("agent.turn.execute.v1",),
    )
    assert [message.message_id for message in claimed] == [message_id]
    clock.advance(2)
    client = GatewayBackedFauxAgentd(
        gateway,
        evidence_objects={"run-a1-smoke": "object-a1-smoke"},
    )

    result = AgentTurnWorker(
        store=agent_store,
        control_repository=control,
        agentd_client=client,
        capability_signer=signer,
        worker_id="replacement-worker",
        clock=clock.epoch,
    ).dispatch_due(limit=1)

    assert result.succeeded == 1
    assert control.get_outbox(message_id).state == "succeeded"
    assert len(client.invocations) == 3


def test_fault_after_one_tool_result_replays_idempotently(tmp_path: Path) -> None:
    agent_store, control, _, _, signer, gateway, _, api = _stack(tmp_path)
    session, turn = _submit(api, suffix="agentd-crash")
    first_client = GatewayBackedFauxAgentd(
        gateway,
        evidence_objects={"run-a1-smoke": "object-a1-smoke"},
        fail_after_tools=1,
    )
    first = AgentTurnWorker(
        store=agent_store,
        control_repository=control,
        agentd_client=first_client,
        capability_signer=signer,
        worker_id="worker-before-agentd-restart",
    ).dispatch_due(limit=1)
    assert len(first.errors) == 1
    second_client = GatewayBackedFauxAgentd(
        gateway,
        evidence_objects={"run-a1-smoke": "object-a1-smoke"},
    )

    second = AgentTurnWorker(
        store=agent_store,
        control_repository=control,
        agentd_client=second_client,
        capability_signer=signer,
        worker_id="worker-after-agentd-restart",
    ).dispatch_due(limit=1)
    events = api.handle_get(
        f"/api/v1/agent-sessions/{session.payload['session_id']}/events",
        headers={"X-Pilot107-User": "alice"},
    )

    assert second.succeeded == 1
    assert [item["sequence"] for item in events.payload["items"]] == list(range(1, 14))
    with agent_store.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM agent_tool_invocations WHERE turn_id = ?",
            (turn.payload["turn_id"],),
        ).fetchone()[0]
    assert count == 3


def test_100_idle_and_10_active_turns_stay_within_resource_budgets(tmp_path: Path) -> None:
    agent_store, control, run_store, evidence_store, signer, gateway, service, _ = _stack(
        tmp_path
    )
    for index in range(100):
        service.create_session(
            owner=f"idle{index}",
            request_key=f"idle-session-{index}",
            model_profile_id="faux-default",
            source={},
        )
    evidence_objects = {"run-a1-smoke": "object-a1-smoke"}
    queued_at: dict[str, float] = {}
    turn_ids: list[str] = []
    for index in range(10):
        owner = f"active{index}"
        run_id = f"run-active-{index}"
        object_id = f"object-active-{index}"
        workspace = tmp_path / "workspaces" / "alice" / "project"
        _seed_run(
            run_store,
            evidence_store,
            owner=owner,
            run_id=run_id,
            object_id=object_id,
            workspace=workspace,
        )
        evidence_objects[run_id] = object_id
        session, _ = service.create_session(
            owner=owner,
            request_key=f"active-session-{index}",
            model_profile_id="faux-default",
            source={"run_id": run_id},
        )
        turn, _ = service.submit_message(
            session_id=session.session_id,
            owner=owner,
            request_key=f"active-turn-{index}",
            message="inspect bounded evidence",
            expected_state_version=session.state_version,
        )
        queued_at[turn.turn_id] = time.monotonic()
        turn_ids.append(turn.turn_id)
    client = GatewayBackedFauxAgentd(
        gateway,
        evidence_objects=evidence_objects,
        start_barrier=threading.Barrier(10),
    )
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    cpu_before = resource.getrusage(resource.RUSAGE_SELF)
    cpu_seconds_before = cpu_before.ru_utime + cpu_before.ru_stime
    started = time.monotonic()

    def dispatch(index: int):
        return AgentTurnWorker(
            store=agent_store,
            control_repository=control,
            agentd_client=client,
            capability_signer=signer,
            worker_id=f"performance-worker-{index}",
        ).dispatch_due(limit=1)

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(dispatch, range(10)))
    elapsed = time.monotonic() - started
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    cpu_after = resource.getrusage(resource.RUSAGE_SELF)
    cpu_seconds = cpu_after.ru_utime + cpu_after.ru_stime - cpu_seconds_before
    queue_waits = [client.started_at[turn_id] - queued_at[turn_id] for turn_id in turn_ids]
    event_lags = [client.finished_at[turn_id] - client.started_at[turn_id] for turn_id in turn_ids]
    with agent_store.connect() as connection:
        idle_count = connection.execute(
            "SELECT COUNT(*) FROM agent_sessions WHERE owner LIKE 'idle%' AND state = 'idle'"
        ).fetchone()[0]
        budgets = connection.execute(
            "SELECT turn_id, COUNT(*) AS invocations, SUM(bytes_returned) AS bytes "
            "FROM agent_tool_invocations GROUP BY turn_id"
        ).fetchall()
    report = {
        "idle_sessions": idle_count,
        "active_turns": len(turn_ids),
        "elapsed_seconds": elapsed,
        "cpu_seconds": cpu_seconds,
        "rss_delta_kib": max(0, rss_after - rss_before),
        "max_queue_wait_seconds": max(queue_waits),
        "max_event_lag_seconds": max(event_lags),
        "tool_invocations": len(client.invocations),
        "tool_bytes": sum(client.bytes_returned),
        "peak_active_faux_turns": client.peak_active,
    }
    print(f"pilot Agent A1 D0 resource baseline: {json.dumps(report, sort_keys=True)}")

    assert all(result.succeeded == 1 for result in results), report
    assert report["idle_sessions"] == 100
    assert report["peak_active_faux_turns"] == 10
    assert client.active_turns == 0
    assert len(budgets) == 10
    assert all(int(row["invocations"]) <= 32 for row in budgets)
    assert all(int(row["bytes"]) <= 1024 * 1024 for row in budgets)
    assert max(client.bytes_returned) <= 64 * 1024
    assert set(client.finished_at) == set(turn_ids)


@pytest.mark.parametrize(
    "barrier",
    [
        "api_after_turn_commit",
        "worker_after_outbox_claim",
        "agentd_after_tool_result",
        "browser_after_event_n",
    ],
)
def test_fault_matrix_has_objective_recovery_evidence(barrier: str) -> None:
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    fault_script = (scripts / "fault-pilot-agent-a1.sh").read_text(encoding="utf-8")
    smoke_script = (scripts / "smoke-pilot-agent-a1.py").read_text(encoding="utf-8")

    assert barrier in fault_script
    assert "contiguous" in smoke_script
    assert "idempotency" in smoke_script
    assert "one_turn" in smoke_script
    assert "stale_fence_rejected" in smoke_script
