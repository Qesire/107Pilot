from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pilot107.agent.capabilities import AgentCapabilityClaims, AgentCapabilitySigner
from pilot107.agent.protocol import TOOL_INVOCATION_PROTOCOL_VERSION
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.agent.tasks import AgentResourceEnvelope
from pilot107.agent.tool_gateway import AgentReadResult, AgentToolGateway
from pilot107.api.asgi_app import build_asgi_app
from pilot107.api.http_app import build_api
from pilot107.api.service import build_api_service, config_from_env


def _configured_api(root: Path):
    from pilot107.api.agent_tool_routes import AgentToolRoutes

    now = datetime(2026, 8, 14, 0, 0, tzinfo=UTC)
    store = SQLiteAgentSessionStore(root / "agent.db", clock=lambda: now)
    session, _ = store.create_session(
        owner="alice",
        request_key="session-1",
        profile_id="hpc-readonly-v1",
        model_profile_id="faux-default",
        source={},
    )
    turn, _ = store.create_turn(
        session_id=session.session_id,
        owner="alice",
        request_key="turn-1",
        message="inspect run",
        expected_state_version=session.state_version,
    )
    claim = store.claim_turn(turn.turn_id, worker_id="worker-1", lease_seconds=30)
    assert claim is not None
    reads: list[str] = []

    def read_run(owner, arguments):
        reads.append(owner)
        return AgentReadResult(
            result={"run_id": arguments["run_id"], "state": "pending"},
            evidence_refs=("run:run-1",),
        )

    signer = AgentCapabilitySigner(b"s" * 32, clock=lambda: int(now.timestamp()))
    token = signer.sign(
        AgentCapabilityClaims(
            owner="alice",
            session_id=session.session_id,
            turn_id=turn.turn_id,
            state_version=claim.state_version,
            fencing_token=claim.fencing_token,
            profile_id="hpc-readonly-v1",
            tools=frozenset({"run_get"}),
            max_invocations=4,
            max_bytes=64 * 1024,
            expires_at=int(now.timestamp()) + 60,
        )
    )
    gateway = AgentToolGateway(
        store=store,
        signer=signer,
        handlers={"run_get": read_run},
        clock=lambda: now,
    )
    api = build_api(db_path=root / "api.db", evidence_root=root / "evidence")
    api.agent_tool_routes = AgentToolRoutes(gateway)
    payload = {
        "schema_version": TOOL_INVOCATION_PROTOCOL_VERSION,
        "invocation_id": "invocation-1",
        "idempotency_key": "tool-1",
        "owner": "alice",
        "session_id": session.session_id,
        "turn_id": turn.turn_id,
        "state_version": claim.state_version,
        "profile_id": "hpc-readonly-v1",
        "tool_name": "run_get",
        "arguments": {"run_id": "run-1"},
        "deadline": "2026-08-14T00:00:20Z",
    }
    return api, token, payload, reads


def test_private_route_requires_bearer_and_ignores_user_identity(tmp_path: Path) -> None:
    api, _, payload, reads = _configured_api(tmp_path)
    app = build_asgi_app(api)

    response = _asgi_request(
        app,
        "/internal/v1/agent-tools/invoke",
        method="POST",
        body=json.dumps(payload).encode(),
        headers={"content-type": "application/json", "x-pilot107-user": "alice"},
    )

    assert response[0] == 401
    error = json.loads(response[2])["error"]
    assert error["code"] == "AGENT.CAPABILITY.MISSING"
    assert error["message"] == "Bearer capability required"
    assert reads == []


def test_private_route_parses_one_invocation_and_returns_one_result(tmp_path: Path) -> None:
    api, token, payload, reads = _configured_api(tmp_path)

    response = api.handle_post(
        "/internal/v1/agent-tools/invoke",
        body=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )

    assert response.status == 200
    assert response.payload["schema_version"] == "pilot107.agent-tool-result/v1"
    assert response.payload["result"] == {"run_id": "run-1", "state": "pending"}
    assert response.payload["evidence_refs"] == ["run:run-1"]
    assert reads == ["alice"]


def test_private_route_caps_body_and_stays_out_of_openapi(tmp_path: Path) -> None:
    api, token, _, reads = _configured_api(tmp_path)
    oversized = api.handle_post(
        "/internal/v1/agent-tools/invoke",
        body=b"x" * (1024 * 1024 + 1),
        headers={"Authorization": f"Bearer {token}"},
    )
    app = build_asgi_app(api)

    assert oversized.status == 413
    assert oversized.payload["error"]["code"] == "AGENT.TOOL.REQUEST_TOO_LARGE"
    assert "/internal/v1/agent-tools/invoke" not in app.openapi()["paths"]
    assert reads == []


def test_phase_aware_builder_context_route_uses_bound_scope(tmp_path: Path) -> None:
    secret = b"s" * 32
    api = build_api_service(
        config_from_env(
            {
                "PILOT107_AGENT_CAPABILITY_HMAC_SECRET": secret.decode(),
                "PILOT107_API_BACKEND": "in-memory",
                "PILOT107_PHASE_AWARE_BUILDER": "true",
            },
            project_root=tmp_path,
        )
    )
    assert api.project_agent_routes is not None
    assert api.agent_session_routes is not None
    assert api.agent_tool_routes is not None
    created = api.project_agent_routes.service.create_project(
        owner="alice",
        origin="blank",
        goal="build a heat diffusion experiment",
        request_key="builder-project",
    )
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    envelope = AgentResourceEnvelope(
        partition="debug",
        qos="normal",
        cpus=1,
        memory_mib=512,
        gpu_type=None,
        gpus=0,
        walltime_seconds=300,
        max_tasks=1,
        max_submissions=1,
        workspace_snapshot_digest=created.workspace.snapshot.digest,
        expires_at=expires_at.isoformat().replace("+00:00", "Z"),
        approved_by="alice",
    )
    session_service = api.agent_session_routes.service
    session, _ = session_service.create_session(
        owner="alice",
        request_key="builder-session",
        profile_id="experiment_builder",
        model_profile_id="faux-default",
        source={
            "project_id": created.project.project_id,
            "workspace_id": created.workspace.workspace_id,
            "resource_envelope": asdict(envelope),
        },
    )
    turn, _ = session_service.submit_message(
        session_id=session.session_id,
        owner="alice",
        request_key="builder-turn",
        message="build the experiment",
        expected_state_version=session.state_version,
    )
    gateway = api.agent_tool_routes.gateway
    claim = gateway.store.claim_turn(
        turn.turn_id,
        worker_id="builder-worker",
        lease_seconds=60,
    )
    assert claim is not None
    capability_expires_at = datetime.now(UTC) + timedelta(seconds=60)
    token = gateway.signer.sign(
        AgentCapabilityClaims(
            owner="alice",
            session_id=session.session_id,
            turn_id=turn.turn_id,
            state_version=claim.state_version,
            fencing_token=claim.fencing_token,
            profile_id="experiment_builder",
            tools=frozenset({"builder_context_get", "builder_build_submit"}),
            max_invocations=32,
            max_bytes=1024 * 1024,
            expires_at=int(capability_expires_at.timestamp()),
            project_id=created.project.project_id,
            workspace_id=created.workspace.workspace_id,
            operations=frozenset({"read", "write", "validate"}),
            max_commands=32,
        )
    )
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    payload = {
        "schema_version": TOOL_INVOCATION_PROTOCOL_VERSION,
        "invocation_id": "invocation-builder-context",
        "idempotency_key": "builder-context-1",
        "owner": "alice",
        "session_id": session.session_id,
        "turn_id": turn.turn_id,
        "state_version": claim.state_version,
        "profile_id": "experiment_builder",
        "tool_name": "builder_context_get",
        "arguments": {
            "project_id": created.project.project_id,
            "workspace_id": created.workspace.workspace_id,
            "session_id": session.session_id,
        },
        "deadline": deadline.isoformat().replace("+00:00", "Z"),
    }

    response = api.handle_post(
        "/internal/v1/agent-tools/invoke",
        body=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )

    assert response.status == 200
    assert response.payload["result"]["phase"] == "drafting"
    assert response.payload["result"]["next_action"] == "builder_build_submit"


def _asgi_request(
    app,
    target: str,
    *,
    method: str,
    body: bytes,
    headers: dict[str, str],
) -> tuple[int, dict[str, str], bytes]:
    async def invoke() -> tuple[int, dict[str, str], bytes]:
        sent = False
        messages: list[dict] = []

        async def receive() -> dict:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict) -> None:
            messages.append(message)

        raw_headers = [(key.lower().encode(), value.encode()) for key, value in headers.items()]
        raw_headers.append((b"content-length", str(len(body)).encode()))
        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": target,
                "raw_path": target.encode(),
                "query_string": b"",
                "headers": raw_headers,
                "client": ("127.0.0.1", 1234),
                "server": ("test", 80),
            },
            receive,
            send,
        )
        start = next(message for message in messages if message["type"] == "http.response.start")
        response_body = b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )
        response_headers = {
            key.decode().lower(): value.decode() for key, value in start["headers"]
        }
        return start["status"], response_headers, response_body

    return asyncio.run(invoke())
