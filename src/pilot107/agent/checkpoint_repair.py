"""Build fail-closed checkpoint repair deltas from durable Agent facts.

The rebuilder never inspects Workspace files and never executes a tool. It only
joins already-persisted ``tool_call_requested`` Turn events with terminal
``agent_tool_invocations`` rows. The resulting delta is applied by pilot-agentd
at checkpoint restore time, where the existing TypeScript checkpoint
canonicalizer remains authoritative for the next checkpoint digest.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from pilot107.agent.repair_protocol import ToolReceiptRepair

_MAX_REPAIRS = 256
_PROJECT_TOOLS = frozenset(
    {
        "project_get",
        "project_blueprint_save",
        "workspace_list",
        "workspace_read",
        "workspace_patch",
        "workspace_diff",
        "sandbox_exec",
        "validation_schedule",
        "builder_context_get",
        "builder_build_submit",
    }
)
_SESSION_BOUND_TOOLS = frozenset(
    {"validation_schedule", "builder_context_get", "builder_build_submit"}
)
_TURN_BOUND_TOOLS = frozenset({"validation_schedule", "builder_build_submit"})


class ToolReceiptCheckpointRebuilder(Protocol):
    def build(
        self,
        *,
        turn_id: str,
        session_id: str,
        owner: str,
        checkpoint: Mapping[str, object] | None,
        session_source: Mapping[str, object],
    ) -> tuple[ToolReceiptRepair, ...]: ...


def build_tool_receipt_checkpoint_rebuilder(
    store: object,
) -> ToolReceiptCheckpointRebuilder | None:
    db_path = getattr(store, "db_path", None)
    if isinstance(db_path, Path):
        return SQLiteToolReceiptCheckpointRebuilder(db_path)
    dsn = getattr(store, "dsn", None)
    if isinstance(dsn, str) and dsn:
        return PostgresToolReceiptCheckpointRebuilder(dsn)
    return None


class SQLiteToolReceiptCheckpointRebuilder:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def build(
        self,
        *,
        turn_id: str,
        session_id: str,
        owner: str,
        checkpoint: Mapping[str, object] | None,
        session_source: Mapping[str, object],
    ) -> tuple[ToolReceiptRepair, ...]:
        connection = sqlite3.connect(self.db_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        try:
            events = connection.execute(
                """
                SELECT sequence, event_type, payload_json
                FROM agent_turn_events
                WHERE turn_id = ? AND session_id = ? AND owner = ?
                ORDER BY sequence
                """,
                (turn_id, session_id, owner),
            ).fetchall()
            invocations = connection.execute(
                """
                SELECT invocation_id, turn_id, session_id, owner, tool_name,
                       arguments_digest, state, result_json, error_json,
                       bytes_returned
                FROM agent_tool_invocations
                WHERE turn_id = ? AND session_id = ? AND owner = ?
                """,
                (turn_id, session_id, owner),
            ).fetchall()
        finally:
            connection.close()
        return _build_repairs(
            events=events,
            invocations=invocations,
            turn_id=turn_id,
            session_id=session_id,
            owner=owner,
            checkpoint=checkpoint,
            session_source=session_source,
        )


class PostgresToolReceiptCheckpointRebuilder:
    def __init__(self, dsn: str) -> None:
        if not dsn or any(character in dsn for character in "\r\n\0"):
            raise ValueError("PostgreSQL DSN is invalid")
        self.dsn = dsn
        self._psycopg = importlib.import_module("psycopg")
        self._dict_row = importlib.import_module("psycopg.rows").dict_row

    def build(
        self,
        *,
        turn_id: str,
        session_id: str,
        owner: str,
        checkpoint: Mapping[str, object] | None,
        session_source: Mapping[str, object],
    ) -> tuple[ToolReceiptRepair, ...]:
        with self._psycopg.connect(self.dsn, row_factory=self._dict_row) as connection:
            events = connection.execute(
                """
                SELECT sequence, event_type, payload_json
                FROM agent_turn_events
                WHERE turn_id = %s AND session_id = %s AND owner = %s
                ORDER BY sequence
                """,
                (turn_id, session_id, owner),
            ).fetchall()
            invocations = connection.execute(
                """
                SELECT invocation_id, turn_id, session_id, owner, tool_name,
                       arguments_digest, state, result_json, error_json,
                       bytes_returned
                FROM agent_tool_invocations
                WHERE turn_id = %s AND session_id = %s AND owner = %s
                """,
                (turn_id, session_id, owner),
            ).fetchall()
        return _build_repairs(
            events=events,
            invocations=invocations,
            turn_id=turn_id,
            session_id=session_id,
            owner=owner,
            checkpoint=checkpoint,
            session_source=session_source,
        )


def _build_repairs(
    *,
    events: list[Mapping[str, Any]],
    invocations: list[Mapping[str, Any]],
    turn_id: str,
    session_id: str,
    owner: str,
    checkpoint: Mapping[str, object] | None,
    session_source: Mapping[str, object],
) -> tuple[ToolReceiptRepair, ...]:
    parent_digest, base_sequence, completed_ids = _base_checkpoint(
        events,
        checkpoint,
        turn_id=turn_id,
    )
    if checkpoint is not None and base_sequence is None:
        return ()
    after_sequence = 0 if base_sequence is None else base_sequence
    invocation_by_id = {str(row["invocation_id"]): row for row in invocations}
    repairs: list[ToolReceiptRepair] = []
    seen_requested: set[str] = set()
    pending_text: list[str] = []

    for event in events:
        sequence = int(event["sequence"])
        if sequence <= after_sequence:
            continue
        event_type = str(event["event_type"])
        payload = _json_object(event["payload_json"])
        if event_type == "message_delta":
            delta = payload.get("delta")
            if isinstance(delta, str):
                pending_text.append(delta)
            continue
        if event_type != "tool_call_requested":
            continue
        tool_call_id = payload.get("tool_call_id")
        tool_name = payload.get("tool_name")
        arguments = payload.get("arguments")
        if (
            not isinstance(tool_call_id, str)
            or not tool_call_id
            or not isinstance(tool_name, str)
            or not tool_name
            or not isinstance(arguments, Mapping)
            or tool_call_id in completed_ids
        ):
            pending_text.clear()
            continue
        if tool_call_id in seen_requested:
            break
        seen_requested.add(tool_call_id)

        invocation_id = _invocation_id(turn_id, tool_call_id)
        row = invocation_by_id.get(invocation_id)
        if row is None or str(row["state"]) != "completed":
            break
        if (
            str(row["turn_id"]) != turn_id
            or str(row["session_id"]) != session_id
            or str(row["owner"]) != owner
            or str(row["tool_name"]) != tool_name
        ):
            break

        provider_arguments = _finite_json_object(arguments, "tool arguments")
        bound_arguments = _bound_arguments(
            provider_arguments,
            tool_name=tool_name,
            turn_id=turn_id,
            session_id=session_id,
            session_source=session_source,
        )
        expected_arguments_digest = hashlib.sha256(_canonical(bound_arguments)).hexdigest()
        if str(row["arguments_digest"]) != expected_arguments_digest:
            break

        stored_result = _json_object(row["result_json"])
        result = stored_result.get("result")
        evidence_refs = stored_result.get("evidence_refs")
        if (
            not isinstance(result, Mapping)
            or not isinstance(evidence_refs, list)
            or not all(isinstance(reference, str) for reference in evidence_refs)
        ):
            break
        public_result = _finite_json_object(result, "tool result")
        bytes_returned = int(row["bytes_returned"])
        if bytes_returned < 0:
            break
        receipt_ref = _receipt_ref(
            invocation_id=invocation_id,
            tool_name=tool_name,
            arguments_digest=expected_arguments_digest,
            result=stored_result,
            bytes_returned=bytes_returned,
        )
        details = {
            "result": public_result,
            "evidence_refs": list(evidence_refs),
            "bytes_returned": bytes_returned,
        }
        repairs.append(
            ToolReceiptRepair(
                parent_checkpoint_digest=parent_digest,
                invocation_id=invocation_id,
                receipt_ref=receipt_ref,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments=provider_arguments,
                assistant_text="".join(pending_text),
                content=json.dumps(
                    public_result,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                details=_finite_json_object(details, "tool repair details"),
                is_error=False,
            )
        )
        pending_text.clear()
        if len(repairs) >= _MAX_REPAIRS:
            break

    return tuple(repairs)


def _base_checkpoint(
    events: list[Mapping[str, Any]],
    checkpoint: Mapping[str, object] | None,
    *,
    turn_id: str,
) -> tuple[str | None, int | None, set[str]]:
    if checkpoint is None:
        return None, 0, set()
    digest = checkpoint.get("digest")
    checkpoint_turn_id = checkpoint.get("turn_id")
    completed = checkpoint.get("completed_tools")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or not isinstance(checkpoint_turn_id, str)
        or not checkpoint_turn_id
        or not isinstance(completed, list)
    ):
        return "", None, set()
    completed_ids = {
        str(item["tool_call_id"])
        for item in completed
        if isinstance(item, Mapping) and isinstance(item.get("tool_call_id"), str)
    }
    if checkpoint_turn_id != turn_id:
        return digest, 0, completed_ids

    matched_sequence: int | None = None
    for event in events:
        payload = _json_object(event["payload_json"])
        candidate = payload.get("checkpoint")
        if isinstance(candidate, Mapping) and candidate.get("digest") == digest:
            sequence = int(event["sequence"])
            if matched_sequence is None or sequence > matched_sequence:
                matched_sequence = sequence
    return digest, matched_sequence, completed_ids


def _bound_arguments(
    arguments: dict[str, Any],
    *,
    tool_name: str,
    turn_id: str,
    session_id: str,
    session_source: Mapping[str, object],
) -> dict[str, Any]:
    result = dict(arguments)
    if tool_name in _PROJECT_TOOLS:
        project_id = session_source.get("project_id")
        workspace_id = session_source.get("workspace_id")
        if not isinstance(project_id, str) or not project_id:
            raise ValueError("Project Session source is missing project_id")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError("Project Session source is missing workspace_id")
        result["project_id"] = project_id
        result["workspace_id"] = workspace_id
    if tool_name in _SESSION_BOUND_TOOLS:
        result["session_id"] = session_id
    if tool_name in _TURN_BOUND_TOOLS:
        result["turn_id"] = turn_id
    return _finite_json_object(result, "bound tool arguments")


def _invocation_id(turn_id: str, tool_call_id: str) -> str:
    digest = hashlib.sha256(f"{turn_id}\0{tool_call_id}".encode()).hexdigest()
    return f"inv-{digest}"


def _receipt_ref(
    *,
    invocation_id: str,
    tool_name: str,
    arguments_digest: str,
    result: Mapping[str, object],
    bytes_returned: int,
) -> str:
    payload = {
        "invocation_id": invocation_id,
        "tool_name": tool_name,
        "arguments_digest": arguments_digest,
        "state": "completed",
        "result": dict(result),
        "bytes_returned": bytes_returned,
    }
    digest = hashlib.sha256(_canonical(payload)).hexdigest()
    return f"agent-tool-receipt:{invocation_id}:sha256:{digest}"


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise ValueError("durable Agent JSON payload is not an object")
    return dict(value)


def _finite_json_object(value: Mapping[str, object], label: str) -> dict[str, Any]:
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be finite JSON") from exc
    if len(encoded.encode("utf-8")) > 1_048_576:
        raise ValueError(f"{label} exceeds 1 MiB")
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise TypeError(f"{label} must be an object")
    return decoded


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
