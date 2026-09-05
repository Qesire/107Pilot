"""PostgreSQL-backed reconciliation for durable Agent mutation operations.

This module is intentionally separate from Workspace state. It may only repair
an ``agent_operations`` record from already-persisted domain facts. It never
scans or mutates Workspace files to guess whether a patch or sandbox command ran.

Recovery authorities are deliberately narrow:

* ``builder_build_submit`` -> terminal ``agent_builder_submissions.receipt_json``
* ``validation_schedule`` -> an AgentTask that has advanced beyond the raw
  ``pending/version=0`` creation boundary
* ``workspace_patch`` -> a COMMITTED AC4 Workspace mutation journal bound to the
  exact same durable operation id and file plan

The competition runtime has one persistence authority: PostgreSQL. SQLite is
not a supported reconciliation backend or fallback.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from pilot107.agent.operation_ledger import (
    AgentOperationLedger,
    AgentOperationRecord,
    AgentOperationState,
)
from pilot107.agent.postgres_workspace_durability import PostgresWorkspaceDurabilitySchema
from pilot107.agent.protocol import ToolInvocation


class AgentOperationReconciler(Protocol):
    def reconcile(
        self,
        record: AgentOperationRecord,
        *,
        invocation: ToolInvocation,
        expected_fencing_token: int,
    ) -> AgentOperationRecord | None: ...


def build_agent_operation_reconciler(
    store: object,
    ledger: AgentOperationLedger | None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> AgentOperationReconciler:
    """Build reconciliation against the PostgreSQL Agent authority."""

    if ledger is None:
        raise RuntimeError("Agent reconciliation requires an operation ledger")
    dsn = getattr(store, "dsn", None)
    if not isinstance(dsn, str) or not dsn:
        raise RuntimeError("Agent reconciliation requires a PostgreSQL-backed store")
    return PostgresAgentOperationReconciler(dsn, ledger=ledger, clock=clock)


class PostgresAgentOperationReconciler:
    def __init__(
        self,
        dsn: str,
        *,
        ledger: AgentOperationLedger,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not dsn or any(character in dsn for character in "\r\n\0"):
            raise ValueError("PostgreSQL DSN is invalid")
        # Reconciliation may be constructed before ProjectAgentService/editor.
        # Installing/verifying AC4 schema here removes startup-order coupling.
        schema = PostgresWorkspaceDurabilitySchema(dsn)
        self.dsn = dsn
        self.ledger = ledger
        self._clock = clock or (lambda: datetime.now(UTC))
        self._psycopg = schema._psycopg
        self._dict_row = schema._dict_row
        self._jsonb = importlib.import_module("psycopg.types.json").Jsonb

    def connect(self) -> Any:
        return self._psycopg.connect(self.dsn, row_factory=self._dict_row)

    def reconcile(
        self,
        record: AgentOperationRecord,
        *,
        invocation: ToolInvocation,
        expected_fencing_token: int,
    ) -> AgentOperationRecord | None:
        _assert_reconcile_request(record, invocation, expected_fencing_token)
        if not _is_reconcilable_state(record, invocation):
            return None
        with self.connect() as connection, connection.transaction():
            now = _clock_value(self._clock)
            valid = connection.execute(
                """
                SELECT 1 FROM agent_turns
                WHERE turn_id = %s
                  AND session_id = %s
                  AND owner = %s
                  AND state = 'running'
                  AND cancel_requested = 0
                  AND state_version = %s
                  AND fencing_token = %s
                  AND lease_expires_at > %s
                """,
                (
                    invocation.turn_id,
                    invocation.session_id,
                    invocation.owner,
                    invocation.state_version,
                    expected_fencing_token,
                    now,
                ),
            ).fetchone()
            if valid is None:
                return None
            resolution = self._resolution(connection, record, invocation)
            if resolution is None:
                _postgres_note_unresolved(
                    connection,
                    record=record,
                    invocation=invocation,
                    now=now,
                    jsonb=self._jsonb,
                )
                return None
            result, side_effect_ref = resolution
            _postgres_commit_resolution(
                connection,
                record=record,
                invocation=invocation,
                result=result,
                side_effect_ref=side_effect_ref,
                now=now,
                jsonb=self._jsonb,
            )
        return self.ledger.get(record.operation_key, owner=record.owner)

    def _resolution(
        self,
        connection: Any,
        record: AgentOperationRecord,
        invocation: ToolInvocation,
    ) -> tuple[dict[str, Any], str | None] | None:
        if record.tool_name == "builder_build_submit":
            row = connection.execute(
                """
                SELECT submission_id, change_set_id, sandbox_result_id, task_id,
                       receipt_json
                FROM agent_builder_submissions
                WHERE owner = %s AND request_key = %s
                """,
                (record.owner, record.request_key),
            ).fetchone()
            return _builder_resolution(row)
        if record.tool_name == "validation_schedule":
            row = connection.execute(
                """
                SELECT task_id, state, version, linked_run_id, schedule_receipt
                FROM agent_tasks
                WHERE owner = %s AND request_key = %s
                """,
                (record.owner, record.request_key),
            ).fetchone()
            return _task_resolution(row)
        if record.tool_name == "workspace_patch":
            return _postgres_workspace_patch_resolution(
                connection,
                record=record,
                invocation=invocation,
                jsonb=self._jsonb,
            )
        return None


def _builder_resolution(
    row: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], str | None] | None:
    if row is None:
        return None
    receipt = _json_mapping(_row_value(row, "receipt_json"))
    if receipt is None:
        return None
    submission_id = str(row["submission_id"])
    refs = [f"builder-submission:{submission_id}"]
    for column, prefix in (
        ("change_set_id", "changeset"),
        ("sandbox_result_id", "sandbox"),
        ("task_id", "agent-task"),
    ):
        value = _row_value(row, column)
        if value is not None:
            refs.append(f"{prefix}:{value}")
    return _stored_operation_result(receipt, refs), refs[0]


def _task_resolution(
    row: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], str | None] | None:
    if row is None:
        return None
    state = str(row["state"])
    version = int(row["version"])
    linked_run_id = _row_value(row, "linked_run_id")
    schedule_receipt = _row_value(row, "schedule_receipt")
    # Creation alone is not proof that the initial execute outbox was durable.
    if (
        state == "pending"
        and version == 0
        and linked_run_id is None
        and schedule_receipt is None
    ):
        return None
    task_id = str(row["task_id"])
    result = {
        "task_id": task_id,
        "state": state,
        "linked_run_id": None if linked_run_id is None else str(linked_run_id),
        "terminate": True,
    }
    ref = f"agent-task:{task_id}"
    return _stored_operation_result(result, [ref]), ref


def _postgres_workspace_patch_resolution(
    connection: Any,
    *,
    record: AgentOperationRecord,
    invocation: ToolInvocation,
    jsonb: Any,
) -> tuple[dict[str, Any], str | None] | None:
    identity = _workspace_patch_identity(invocation.arguments)
    if identity is None:
        return None
    project_id, workspace_id, approval_summary_zh, expected_files = identity
    if record.target_ref != f"workspace:{workspace_id}":
        return None
    expected_payload = json.loads(_workspace_files_json(expected_files))
    rows = connection.execute(
        """
        SELECT j.change_set_id, c.payload_json
        FROM agent_workspace_mutation_journal AS j
        JOIN agent_workspace_changesets AS c
          ON c.change_set_id = j.change_set_id
         AND c.owner = j.owner
         AND c.workspace_id = j.workspace_id
         AND c.project_id = j.project_id
        WHERE j.owner = %s
          AND j.workspace_id = %s
          AND j.project_id = %s
          AND j.state = 'committed'
          AND j.change_set_id IS NOT NULL
          AND j.request_key = %s
          AND j.files_json = %s
        ORDER BY j.updated_at DESC, j.mutation_id DESC
        """,
        (
            record.owner,
            workspace_id,
            project_id,
            record.operation_key,
            jsonb(expected_payload),
        ),
    ).fetchall()
    return _workspace_patch_result(
        rows,
        record=record,
        project_id=project_id,
        workspace_id=workspace_id,
        approval_summary_zh=approval_summary_zh,
    )


def _workspace_patch_result(
    rows: list[Any],
    *,
    record: AgentOperationRecord,
    project_id: str,
    workspace_id: str,
    approval_summary_zh: str,
) -> tuple[dict[str, Any], str | None] | None:
    if len(rows) != 1:
        return None
    row = rows[0]
    change_set = _json_mapping(row["payload_json"])
    if change_set is None:
        return None
    change_set_id = str(row["change_set_id"])
    if (
        change_set.get("change_set_id") != change_set_id
        or change_set.get("project_id") != project_id
        or change_set.get("workspace_id") != workspace_id
        or change_set.get("owner") != record.owner
        or not isinstance(change_set.get("created_at"), str)
    ):
        return None
    # workspace_patch originally returns the ChangeSet immediately after its
    # durable creation. Later transitions may have updated the same row, so
    # reconstruct that initial immutable DRAFT view.
    created_at = str(change_set["created_at"])
    initial = dict(change_set)
    initial["state"] = "draft"
    initial["version"] = 1
    initial["sandbox_results"] = []
    initial["approval"] = None
    initial["updated_at"] = created_at
    initial["approval_summary_zh"] = approval_summary_zh
    ref = f"changeset:{change_set_id}"
    return _stored_operation_result(initial, [ref]), ref


def _workspace_patch_identity(
    arguments: Mapping[str, object],
) -> tuple[str, str, str, tuple[tuple[str, str, str | None, str | None], ...]] | None:
    project_id = arguments.get("project_id")
    workspace_id = arguments.get("workspace_id")
    approval_summary_zh = arguments.get("approval_summary_zh")
    patches = arguments.get("patches")
    if (
        not isinstance(project_id, str)
        or not project_id
        or not isinstance(workspace_id, str)
        or not workspace_id
        or not isinstance(approval_summary_zh, str)
        or not isinstance(patches, list)
        or not 1 <= len(patches) <= 256
    ):
        return None
    planned: list[tuple[str, str, str | None, str | None]] = []
    seen: set[str] = set()
    for raw in patches:
        if not isinstance(raw, Mapping):
            return None
        path = raw.get("path")
        operation = raw.get("operation")
        before = raw.get("expected_source_digest")
        content = raw.get("content")
        if (
            not isinstance(path, str)
            or not path
            or path in seen
            or operation not in {"create", "modify", "delete"}
        ):
            return None
        seen.add(path)
        if operation == "create":
            if before is not None or not isinstance(content, str):
                return None
            before_digest = None
            after_digest = hashlib.sha256(content.encode()).hexdigest()
        elif operation == "modify":
            if not _sha256_text(before) or not isinstance(content, str):
                return None
            before_digest = str(before)
            after_digest = hashlib.sha256(content.encode()).hexdigest()
        else:
            if not _sha256_text(before) or content is not None:
                return None
            before_digest = str(before)
            after_digest = None
        planned.append((path, str(operation), before_digest, after_digest))
    return project_id, workspace_id, approval_summary_zh, tuple(sorted(planned))


def _workspace_files_json(
    files: tuple[tuple[str, str, str | None, str | None], ...],
) -> str:
    payload = [
        {
            "path": path,
            "operation": operation,
            "before_sha256": before,
            "after_sha256": after,
        }
        for path, operation, before, after in files
    ]
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _stored_operation_result(
    result: Mapping[str, object],
    refs: list[str],
) -> dict[str, Any]:
    payload = _finite_json_object(result, "reconciled result")
    return {
        "result": payload,
        "evidence_refs": list(refs),
        "bytes_returned": len(_canonical(payload)),
    }


def _postgres_commit_resolution(
    connection: Any,
    *,
    record: AgentOperationRecord,
    invocation: ToolInvocation,
    result: Mapping[str, object],
    side_effect_ref: str | None,
    now: datetime,
    jsonb: Any,
) -> None:
    result_payload = _finite_json_object(result, "operation result")
    digest = hashlib.sha256(_canonical(result_payload)).hexdigest()
    row = connection.execute(
        """
        UPDATE agent_operations
        SET state = 'completed',
            result_json = %s,
            error_json = NULL,
            receipt_ref = %s,
            result_digest = %s,
            side_effect_ref = %s,
            reconciliation_attempt = reconciliation_attempt + 1,
            last_invocation_id = %s,
            updated_at = %s
        WHERE operation_key = %s
          AND owner = %s
          AND session_id = %s
          AND state IN ('running', 'stale', 'unknown', 'reconciling')
        RETURNING operation_key
        """,
        (
            jsonb(result_payload),
            f"agent-operation:{record.operation_key}:sha256:{digest}",
            digest,
            side_effect_ref,
            invocation.invocation_id,
            now,
            record.operation_key,
            record.owner,
            record.session_id,
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("operation reconciliation lost its state race")


def _postgres_note_unresolved(
    connection: Any,
    *,
    record: AgentOperationRecord,
    invocation: ToolInvocation,
    now: datetime,
    jsonb: Any,
) -> None:
    row = connection.execute(
        """
        UPDATE agent_operations
        SET state = %s,
            error_json = %s,
            reconciliation_attempt = reconciliation_attempt + 1,
            last_invocation_id = %s,
            updated_at = %s
        WHERE operation_key = %s
          AND owner = %s
          AND session_id = %s
          AND state IN ('running', 'stale', 'unknown', 'reconciling')
        RETURNING operation_key
        """,
        (
            _unresolved_state(record, invocation).value,
            jsonb(_unresolved_error()),
            invocation.invocation_id,
            now,
            record.operation_key,
            record.owner,
            record.session_id,
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("operation unresolved reconciliation lost its state race")


def _unresolved_state(
    record: AgentOperationRecord,
    invocation: ToolInvocation,
) -> AgentOperationState:
    if (
        record.state is AgentOperationState.RUNNING
        and record.origin_turn_id != invocation.turn_id
    ):
        return AgentOperationState.STALE
    if record.state is AgentOperationState.UNKNOWN:
        return AgentOperationState.UNKNOWN
    return record.state


def _unresolved_error() -> dict[str, object]:
    return {
        "code": "AGENT.TOOL.OPERATION_UNKNOWN",
        "message": "No authoritative domain receipt can prove the mutation outcome",
        "retryable": False,
    }


def _is_reconcilable_state(
    record: AgentOperationRecord,
    invocation: ToolInvocation,
) -> bool:
    if record.state in {AgentOperationState.UNKNOWN, AgentOperationState.STALE}:
        return True
    return (
        record.state is AgentOperationState.RUNNING
        and record.origin_turn_id != invocation.turn_id
    )


def _assert_reconcile_request(
    record: AgentOperationRecord,
    invocation: ToolInvocation,
    expected_fencing_token: int,
) -> None:
    if not isinstance(record, AgentOperationRecord):
        raise TypeError("record must be an AgentOperationRecord")
    if (
        invocation.owner != record.owner
        or invocation.session_id != record.session_id
        or invocation.tool_name != record.tool_name
        or isinstance(expected_fencing_token, bool)
        or not isinstance(expected_fencing_token, int)
        or expected_fencing_token < 1
    ):
        raise ValueError("operation reconciliation binding is invalid")


def _row_value(row: Mapping[str, Any], key: str) -> Any:
    return row[key]


def _json_mapping(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise ValueError("durable receipt is not a JSON object")
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


def _clock_value(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None:
        raise ValueError("operation reconciler clock must be timezone-aware")
    return value.astimezone(UTC)
