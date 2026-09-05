#!/usr/bin/env python3
"""D1 smoke for durable A1 read-only Agent Turns."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from pilot107.agent.session import AgentSessionConflict
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.core.platform_snapshot import (
    ObservationSourceType,
    PlatformSnapshot,
    PlatformSnapshotScope,
)
from pilot107.core.platform_snapshot_store import PlatformSnapshotStore
from pilot107.core.proxy_auth import load_proxy_hmac_secret, signed_proxy_headers
from pilot107.core.run_store import RunStore
from pilot107.worker.evidence import EvidenceStore

RUN_ID = "run-a1-smoke"
OBJECT_ID = "object-a1-smoke"


def _request(
    base_url: str,
    method: str,
    path: str,
    *,
    owner: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    body = None if payload is None else json.dumps(payload).encode()
    proxy_secret = load_proxy_hmac_secret(
        secret=os.environ.get("PILOT107_PROXY_HMAC_SECRET"),
        secret_file=os.environ.get("PILOT107_PROXY_HMAC_SECRET_FILE"),
    )
    identity_headers = {"X-Pilot107-User": owner}
    if proxy_secret is not None:
        identity_headers = signed_proxy_headers(
            secret=proxy_secret,
            method=method,
            target=path,
            user=owner,
            body=body or b"",
        )
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=body,
        method=method,
        headers={
            **identity_headers,
            **({} if body is None else {"Content-Type": "application/json"}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def seed_fixtures(database: Path, evidence_root: Path, workspace: Path) -> None:
    run_store = RunStore(database)
    evidence_store = EvidenceStore(evidence_root)
    try:
        run_store.get_run(RUN_ID)
    except KeyError:
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "train.py").write_text("import torch\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        subprocess.run(["git", "-C", str(workspace), "add", "train.py"], check=True)
        run_store.create_run(
            run_id=RUN_ID,
            owner="alice",
            workdir=str(workspace),
            script="#!/bin/bash\nexit 1\n",
        )
        artifact = evidence_store.write_text(
            run_id=RUN_ID,
            logical_path="logs/stderr.txt",
            content="ModuleNotFoundError: no module named torch\n",
            content_type="text/plain",
        )
        run_store.upsert_evidence_objects(
            RUN_ID,
            [
                {
                    "object_id": OBJECT_ID,
                    "category": "logs",
                    "logical_path": artifact.logical_path,
                    "store_path": str(artifact.path),
                    "source_uri": f"evidence://{RUN_ID}/{artifact.logical_path}",
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "mime_type": artifact.content_type,
                    "collection_status": "collected",
                    "mutable_during_run": False,
                }
            ],
        )
    platform_store = PlatformSnapshotStore(database)
    try:
        platform_store.get("snapshot-a1-smoke", owner="alice")
    except KeyError:
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


def submit(base_url: str) -> dict[str, Any]:
    status, session = _request(
        base_url,
        "POST",
        "/api/v1/agent-sessions",
        owner="alice",
        payload={
            "request_key": "a1-d1-smoke-session",
            "model_profile_id": "faux-default",
            "source": {"run_id": RUN_ID},
        },
    )
    if status not in {200, 201}:
        raise AssertionError(f"Session create failed: {status} {session}")
    status, turn = _request(
        base_url,
        "POST",
        f"/api/v1/agent-sessions/{session['session_id']}/turns",
        owner="alice",
        payload={
            "request_key": "a1-d1-smoke-turn",
            "message": "Inspect the failed Run, stderr, and evidence.",
            "expected_state_version": session["state_version"],
        },
    )
    if status not in {200, 202}:
        raise AssertionError(f"Turn create failed: {status} {turn}")
    return {"session": session, "turn": turn, "submitted_at": time.time()}


def verify(
    base_url: str,
    database: Path,
    state: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    session = state["session"]
    turn = state["turn"]
    deadline = time.monotonic() + timeout_seconds
    payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status, payload = _request(
            base_url,
            "GET",
            f"/api/v1/agent-sessions/{session['session_id']}/events",
            owner="alice",
        )
        if status == 200 and payload.get("items") and payload["items"][-1][
            "event_type"
        ] in {"turn_completed", "turn_failed"}:
            break
        time.sleep(0.25)
    else:
        raise AssertionError(f"Turn did not become terminal: {payload}")

    events = payload["items"]
    sequences = [int(item["sequence"]) for item in events]
    contiguous = sequences == list(range(1, len(sequences) + 1))
    if not contiguous:
        raise AssertionError(f"durable event sequence is not contiguous: {sequences}")
    requested_tools = [
        item["payload"].get("tool_name")
        for item in events
        if item["event_type"] == "tool_call_requested"
    ]
    required_tools = {"run_get", "run_log_read", "evidence_read"}
    if not required_tools.issubset(set(requested_tools)):
        raise AssertionError(f"A1 faux trajectory is incomplete: {requested_tools}")

    first_status, first_page = _request(
        base_url,
        "GET",
        f"/api/v1/agent-sessions/{session['session_id']}/events?limit=4",
        owner="alice",
    )
    if first_status != 200:
        raise AssertionError("first durable event page failed")
    after = first_page["page"]["last_event_id"]
    resumed_status, resumed = _request(
        base_url,
        "GET",
        f"/api/v1/agent-sessions/{session['session_id']}/events?after_event_id={after}",
        owner="alice",
    )
    if resumed_status != 200:
        raise AssertionError("browser event replay failed")
    replay_ids = [item["event_id"] for item in first_page["items"] + resumed["items"]]
    if replay_ids != [item["event_id"] for item in events] or len(replay_ids) != len(
        set(replay_ids)
    ):
        raise AssertionError("browser replay lost or duplicated durable events")

    bob_session_status, bob_session = _request(
        base_url,
        "GET",
        f"/api/v1/agent-sessions/{session['session_id']}",
        owner="bob",
    )
    bob_run_status, bob_run = _request(
        base_url,
        "GET",
        f"/api/v1/runs/{RUN_ID}",
        owner="bob",
    )
    forbidden_text = json.dumps({"session": bob_session, "run": bob_run})
    if bob_session_status not in {403, 404} or bob_run_status not in {403, 404}:
        raise AssertionError("Bob owner isolation failed")
    if "ModuleNotFoundError" in forbidden_text:
        raise AssertionError("Bob response leaked Alice fixture content")

    agent_store = SQLiteAgentSessionStore(database)
    with agent_store.connect() as connection:
        rows = connection.execute(
            "SELECT idempotency_key, COUNT(*) AS copies, bytes_returned "
            "FROM agent_tool_invocations WHERE turn_id = ? "
            "GROUP BY idempotency_key, bytes_returned",
            (turn["turn_id"],),
        ).fetchall()
        turn_rows = connection.execute(
            "SELECT state_version, fencing_token, state FROM agent_turns "
            "WHERE session_id = ?",
            (session["session_id"],),
        ).fetchall()
    if len(turn_rows) != 1:
        raise AssertionError(f"expected one durable Turn, found {len(turn_rows)}")
    persisted_turn = turn_rows[0]
    if persisted_turn["state"] not in {"completed", "failed", "cancelled"}:
        raise AssertionError(f"Turn is not terminal: {persisted_turn['state']}")
    try:
        agent_store.reserve_tool_invocation(
            invocation_id="inv-a1-smoke-stale-fence",
            idempotency_key="idem-a1-smoke-stale-fence",
            owner="alice",
            session_id=session["session_id"],
            turn_id=turn["turn_id"],
            expected_state_version=int(persisted_turn["state_version"]),
            expected_fencing_token=int(persisted_turn["fencing_token"]) + 1,
            tool_name="run_get",
            arguments_digest="sha256:a1-smoke-stale-fence",
        )
    except AgentSessionConflict:
        stale_fence_rejected = True
    else:
        raise AssertionError("stale-fence tool write was accepted")
    idempotency_ok = len(rows) == len({row["idempotency_key"] for row in rows})
    if not idempotency_ok or any(int(row["copies"]) != 1 for row in rows):
        raise AssertionError("tool invocation idempotency failed")
    total_bytes = sum(int(row["bytes_returned"]) for row in rows)
    if len(rows) > 32 or total_bytes > 1024 * 1024:
        raise AssertionError("A1 tool budget exceeded")
    return {
        "session_id": session["session_id"],
        "turn_id": turn["turn_id"],
        "events": len(events),
        "tools": requested_tools,
        "tool_invocations": len(rows),
        "tool_bytes": total_bytes,
        "queue_and_turn_seconds": round(time.time() - float(state["submitted_at"]), 6),
        "contiguous": contiguous,
        "idempotency": idempotency_ok,
        "browser_replay": True,
        "owner_isolation": True,
        "one_turn": True,
        "stale_fence_rejected": stale_fence_rejected,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--submit-only", action="store_true")
    parser.add_argument("--verify-existing", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    args = parser.parse_args()
    base_url = os.environ.get("PILOT107_A1_BASE_URL", "http://pilot107-api:8080")
    database = Path(os.environ.get("PILOT107_DB_PATH", "/var/lib/pilot107/pilot107.db"))
    evidence_root = Path(
        os.environ.get("PILOT107_EVIDENCE_ROOT", "/var/lib/pilot107/evidence")
    )
    workspace = Path(
        os.environ.get("PILOT107_A1_WORKSPACE", "/var/lib/pilot107/a1-workspace")
    )
    state_path = Path(
        os.environ.get("PILOT107_A1_STATE_FILE", "/var/lib/pilot107/a1-smoke-state.json")
    )
    try:
        if args.verify_existing:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        else:
            seed_fixtures(database, evidence_root, workspace)
            state = submit(base_url)
            state_path.write_text(json.dumps(state), encoding="utf-8")
        if args.submit_only:
            print(json.dumps({"status": "submitted", **state}, sort_keys=True))
            return 0
        report = verify(
            base_url,
            database,
            state,
            timeout_seconds=args.timeout_seconds,
        )
    except (AssertionError, OSError, ValueError, urllib.error.URLError) as error:
        print(f"pilot Agent A1 smoke failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", **report}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
