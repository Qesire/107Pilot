#!/usr/bin/env python3
"""Prove every public consumer reads the same VM-local Slurm facts."""

from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ApiClient:
    def __init__(self, base_url: str, *, owner: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.owner = owner
        self.context = (
            ssl._create_unverified_context() if self.base_url.startswith("https://") else None
        )

    def get(self, path: str) -> dict[str, Any]:
        return self.request("GET", path)

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", path, payload=payload)

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json", "X-Pilot107-User": self.owner}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=30, context=self.context) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{method} {path} transport failed: {exc.reason}") from None
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{method} {path} returned invalid JSON: {exc}") from None
        if not isinstance(decoded, dict):
            raise RuntimeError(f"{method} {path} returned a non-object JSON document")
        return decoded


def _encoded(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"response is missing {key}: {payload}")
    return value


def _identity(payload: object, *, consumer: str) -> tuple[str, str, str]:
    current = payload
    for _ in range(4):
        if not isinstance(current, dict):
            break
        authority_id = current.get("authority_id")
        snapshot_id = current.get("snapshot_id")
        content_sha256 = current.get("content_sha256")
        if all(isinstance(value, str) and value for value in (authority_id, snapshot_id)):
            if not isinstance(content_sha256, str) or _SHA256.fullmatch(content_sha256) is None:
                raise RuntimeError(f"{consumer} returned an invalid content_sha256")
            return authority_id, snapshot_id, content_sha256
        nested = current.get("latest_snapshot")
        if not isinstance(nested, dict):
            nested = current.get("result")
        current = nested
    raise RuntimeError(f"{consumer} did not expose the authority snapshot identity")


def _all_events(client: ApiClient, session_id: str) -> list[dict[str, Any]]:
    after = 0
    events: list[dict[str, Any]] = []
    while True:
        page = client.get(
            f"/agent-sessions/{_encoded(session_id)}/events?after_event_id={after}&limit=100"
        )
        items = page.get("items")
        if not isinstance(items, list):
            raise RuntimeError(f"Agent event page has no items: {page}")
        events.extend(item for item in items if isinstance(item, dict))
        paging = page.get("page")
        if not isinstance(paging, dict) or not paging.get("has_more"):
            return events
        next_after = paging.get("next_after_event_id")
        if isinstance(next_after, bool) or not isinstance(next_after, int) or next_after <= after:
            raise RuntimeError(f"Agent event cursor did not advance: {page}")
        after = next_after


def _agent_platform_result(
    client: ApiClient,
    *,
    model_profile_id: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    request_suffix = f"{int(time.time())}-{os.getpid()}"
    session = client.post(
        "/agent-sessions",
        {
            "request_key": f"vm-slurm-authority-session-{request_suffix}",
            "profile_id": "hpc-readonly-v1",
            "model_profile_id": model_profile_id,
            "source": {},
        },
    )
    session_id = _required_string(session, "session_id")
    state_version = session.get("state_version")
    if isinstance(state_version, bool) or not isinstance(state_version, int):
        raise RuntimeError(f"Agent Session has no state_version: {session}")
    turn = client.post(
        f"/agent-sessions/{_encoded(session_id)}/turns",
        {
            "request_key": f"vm-slurm-authority-turn-{request_suffix}",
            "message": (
                "Call platform_get_snapshot exactly once with {} and then stop. "
                "Do not call any other tool. Report the returned platform facts."
            ),
            "expected_state_version": state_version,
        },
    )
    turn_id = _required_string(turn, "turn_id")

    deadline = time.monotonic() + timeout_seconds
    events: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        events = _all_events(client, session_id)
        terminal = next(
            (
                event
                for event in reversed(events)
                if event.get("turn_id") == turn_id
                and event.get("event_type") in {"turn_completed", "turn_failed"}
            ),
            None,
        )
        if terminal is not None:
            if terminal.get("event_type") != "turn_completed":
                raise RuntimeError(f"Agent Turn failed: {terminal.get('payload')}")
            break
        time.sleep(poll_interval_seconds)
    else:
        raise RuntimeError(f"timed out waiting for Agent Turn {turn_id}")

    completed = [
        event
        for event in events
        if event.get("turn_id") == turn_id
        and event.get("event_type") == "tool_call_completed"
        and isinstance(event.get("payload"), dict)
        and event["payload"].get("tool_name") == "platform_get_snapshot"
    ]
    if len(completed) != 1:
        raise RuntimeError(
            "Agent Turn did not complete platform_get_snapshot exactly once: "
            f"count={len(completed)}"
        )
    payload = completed[0]["payload"]
    if payload.get("is_error") is not False or not isinstance(payload.get("result"), dict):
        raise RuntimeError(f"platform_get_snapshot failed: {payload}")
    return payload["result"]


def run_smoke(
    client: ApiClient,
    *,
    model_profile_id: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    latest = client.get("/platform/snapshots/latest?scope=login_node")
    capabilities = client.get("/platform/capabilities")

    snapshot = latest.get("snapshot")
    if not isinstance(snapshot, dict):
        raise RuntimeError("latest platform response has no snapshot")
    partitions = snapshot.get("partitions")
    nodes = snapshot.get("nodes")
    if not isinstance(partitions, list) or not partitions:
        raise RuntimeError("authoritative VM Slurm facts contain no partitions")
    if not isinstance(nodes, list) or not nodes:
        raise RuntimeError("authoritative VM Slurm facts contain no nodes")
    command_results = snapshot.get("command_results")
    if not isinstance(command_results, list):
        raise RuntimeError("authoritative VM Slurm facts contain no command results")
    results = {
        item.get("name"): item.get("returncode")
        for item in command_results
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    if results.get("scontrol_show_part") != 0 or not any(
        results.get(name) == 0 for name in ("scontrol_show_nodes", "sinfo_pipe")
    ):
        raise RuntimeError(f"authoritative VM Slurm probes are unhealthy: {results}")

    agent = _agent_platform_result(
        client,
        model_profile_id=model_profile_id,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    identities = {
        "latest": _identity(latest, consumer="latest platform"),
        "capabilities": _identity(capabilities, consumer="platform capabilities"),
        "agent": _identity(agent, consumer="Agent platform tool"),
    }
    consumer_ids_equal = len(set(identities.values())) == 1
    if not consumer_ids_equal:
        raise RuntimeError(f"platform consumers disagree: {identities}")
    authority_id, snapshot_id, content_sha256 = identities["latest"]
    if authority_id != "vm-slurm":
        raise RuntimeError(f"unexpected platform authority: {authority_id}")
    return {
        "status": "PASS",
        "authority_id": authority_id,
        "snapshot_id": snapshot_id,
        "content_sha256": content_sha256,
        "partition_count": len(partitions),
        "node_count": len(nodes),
        "consumer_ids_equal": consumer_ids_equal,
    }


def main() -> int:
    base_url = os.environ.get("PILOT107_COMPETITION_BASE_URL", "").rstrip("/")
    if not base_url:
        print(
            json.dumps(
                {"status": "FAIL", "error": "PILOT107_COMPETITION_BASE_URL is required"}
            )
        )
        return 2
    try:
        report = run_smoke(
            ApiClient(
                base_url,
                owner=os.environ.get("PILOT107_VM_SLURM_AUTHORITY_OWNER", "alice"),
            ),
            model_profile_id=os.environ.get(
                "PILOT107_VM_SLURM_AUTHORITY_MODEL_PROFILE", "campus-default"
            ),
            timeout_seconds=float(
                os.environ.get("PILOT107_VM_SLURM_AUTHORITY_TIMEOUT_SECONDS", "180")
            ),
            poll_interval_seconds=float(
                os.environ.get("PILOT107_VM_SLURM_AUTHORITY_POLL_SECONDS", "1")
            ),
        )
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
