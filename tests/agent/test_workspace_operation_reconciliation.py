from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pilot107.agent.durable_workspace import DurableWorkspaceEditor
from pilot107.agent.operation_ledger import (
    AgentOperationState,
    SQLiteAgentOperationLedger,
    operation_intent_for_invocation,
)
from pilot107.agent.operation_reconciler import SQLiteAgentOperationReconciler
from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.protocol import TOOL_INVOCATION_PROTOCOL_VERSION, ToolInvocation
from pilot107.agent.sandbox import SandboxExecutor
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.agent.workspace import WorkspacePatch
from pilot107.services.project_agent_service import ProjectAgentService


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 5, 1, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


class SimulatedHardCrash(BaseException):
    pass


def _project(tmp_path: Path):
    database = tmp_path / "pilot107.db"
    project_store = SQLiteProjectStore(database)
    service = ProjectAgentService(
        store=project_store,
        workspace_root=tmp_path / "agent-workspaces",
        sandbox=SandboxExecutor(store=project_store),
    )
    view = service.create_project(
        owner="alice",
        origin="blank",
        goal="test durable workspace reconciliation",
        request_key="project-request",
    )
    assert isinstance(service.editor, DurableWorkspaceEditor)
    return database, project_store, service, view.project.project_id, view.workspace.workspace_id


def _running_turn(database: Path, project_id: str, workspace_id: str, clock: FixedClock):
    store = SQLiteAgentSessionStore(database, clock=clock)
    session, _ = store.create_session(
        owner="alice",
        request_key="session-request",
        profile_id="experiment_builder",
        model_profile_id="faux-default",
        source={"project_id": project_id, "workspace_id": workspace_id},
    )
    turn, _ = store.create_turn(
        session_id=session.session_id,
        owner="alice",
        request_key="turn-workspace-patch",
        message="apply workspace patch",
        expected_state_version=session.state_version,
    )
    claim = store.claim_turn(turn.turn_id, worker_id="worker-1", lease_seconds=120)
    assert claim is not None
    return store, session, turn, claim


def _invocation(session, turn, claim, *, project_id: str, workspace_id: str, call: str):
    arguments: dict[str, object] = {
        "project_id": project_id,
        "workspace_id": workspace_id,
        "approval_summary_zh": "创建验证脚本。",
        "patches": [
            {
                "path": "script.py",
                "expected_source_digest": None,
                "operation": "create",
                "content": "value = 1\n",
            }
        ],
    }
    return ToolInvocation(
        schema_version=TOOL_INVOCATION_PROTOCOL_VERSION,
        invocation_id=f"invocation-{call}",
        idempotency_key=f"idempotency-{call}",
        owner="alice",
        session_id=session.session_id,
        turn_id=turn.turn_id,
        state_version=claim.state_version,
        profile_id="experiment_builder",
        tool_name="workspace_patch",
        arguments=arguments,
        deadline="2026-09-05T01:01:30Z",
    )


def _semantic_digest(arguments: dict[str, object]) -> str:
    value = dict(arguments)
    value.pop("turn_id", None)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _seed_unknown(store, ledger, invocation, claim):
    intent = operation_intent_for_invocation(
        store,
        invocation,
        arguments_digest=_semantic_digest(invocation.arguments),
    )
    assert intent is not None
    _, created = ledger.reserve(
        intent,
        invocation_id=invocation.invocation_id,
        expected_state_version=claim.state_version,
        expected_fencing_token=claim.fencing_token,
    )
    assert created
    ledger.start(
        intent.operation_key,
        owner="alice",
        invocation_id=invocation.invocation_id,
        expected_state_version=claim.state_version,
        expected_fencing_token=claim.fencing_token,
    )
    ledger.mark_unknown(
        intent.operation_key,
        owner="alice",
        expected_state_version=claim.state_version,
        expected_fencing_token=claim.fencing_token,
        error={"code": "AGENT.TOOL.OPERATION_UNKNOWN", "message": "crash"},
    )
    return intent


def test_committed_workspace_journal_recovers_operation_receipt(tmp_path: Path) -> None:
    database, _, service, project_id, workspace_id = _project(tmp_path)
    change_set = service.apply_patches(
        project_id=project_id,
        workspace_id=workspace_id,
        owner="alice",
        patches=(("script.py", None, "create", "value = 1\n"),),
    )

    clock = FixedClock()
    session_store, session, turn, claim = _running_turn(
        database, project_id, workspace_id, clock
    )
    ledger = SQLiteAgentOperationLedger(database, clock=clock)
    invocation = _invocation(
        session,
        turn,
        claim,
        project_id=project_id,
        workspace_id=workspace_id,
        call="seed",
    )
    intent = _seed_unknown(session_store, ledger, invocation, claim)

    reconciler = SQLiteAgentOperationReconciler(database, ledger=ledger, clock=clock)
    resolved = reconciler.reconcile(
        ledger.get(intent.operation_key, owner="alice"),
        invocation=invocation,
        expected_fencing_token=claim.fencing_token,
    )

    assert resolved is not None
    assert resolved.state is AgentOperationState.COMPLETED
    assert resolved.side_effect_ref == f"changeset:{change_set.change_set_id}"
    assert resolved.result is not None
    assert resolved.result["evidence_refs"] == [f"changeset:{change_set.change_set_id}"]
    payload = resolved.result["result"]
    assert payload["change_set_id"] == change_set.change_set_id
    assert payload["project_id"] == project_id
    assert payload["workspace_id"] == workspace_id
    assert payload["state"] == "draft"
    assert payload["version"] == 1
    assert payload["sandbox_results"] == []
    assert payload["approval"] is None
    assert payload["approval_summary_zh"] == "创建验证脚本。"
    assert payload["updated_at"] == payload["created_at"]


def test_files_applied_journal_is_not_an_operation_receipt(tmp_path: Path) -> None:
    database, project_store, service, project_id, workspace_id = _project(tmp_path)
    workspace = project_store.get_workspace(workspace_id, owner="alice")

    def crash(stage: str) -> None:
        if stage == "after_files_applied":
            raise SimulatedHardCrash

    editor = DurableWorkspaceEditor(
        store=project_store,
        state_root=tmp_path / "crash-state",
        crash_hook=crash,
    )
    with pytest.raises(SimulatedHardCrash):
        editor.apply_patches(
            workspace.workspace_id,
            "alice",
            (("script.py", None, WorkspacePatch(operation="create", content="value = 1\n")),),
        )
    assert (Path(workspace.local_root) / "script.py").read_text() == "value = 1\n"

    clock = FixedClock()
    session_store, session, turn, claim = _running_turn(
        database, project_id, workspace_id, clock
    )
    ledger = SQLiteAgentOperationLedger(database, clock=clock)
    invocation = _invocation(
        session,
        turn,
        claim,
        project_id=project_id,
        workspace_id=workspace_id,
        call="seed-files-applied",
    )
    intent = _seed_unknown(session_store, ledger, invocation, claim)

    reconciler = SQLiteAgentOperationReconciler(database, ledger=ledger, clock=clock)
    resolved = reconciler.reconcile(
        ledger.get(intent.operation_key, owner="alice"),
        invocation=invocation,
        expected_fencing_token=claim.fencing_token,
    )

    assert resolved is None
    assert ledger.get(intent.operation_key, owner="alice").state is AgentOperationState.UNKNOWN
    with sqlite3.connect(database) as connection:
        state = connection.execute(
            "SELECT state FROM agent_workspace_mutation_journal"
        ).fetchone()
    assert state is not None
    assert state[0] == "files_applied"
    assert isinstance(service.editor, DurableWorkspaceEditor)


def test_multiple_matching_committed_journals_fail_closed(tmp_path: Path) -> None:
    database, _, service, project_id, workspace_id = _project(tmp_path)
    service.apply_patches(
        project_id=project_id,
        workspace_id=workspace_id,
        owner="alice",
        patches=(("script.py", None, "create", "value = 1\n"),),
    )
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT * FROM agent_workspace_mutation_journal WHERE state = 'committed'"
        ).fetchone()
        assert row is not None
        columns = [item[1] for item in connection.execute(
            "PRAGMA table_info(agent_workspace_mutation_journal)"
        ).fetchall()]
        values = dict(zip(columns, row, strict=True))
        values["mutation_id"] = "workspace-mutation-" + "f" * 64
        values["request_key"] = str(values["request_key"]) + ":duplicate"
        placeholders = ",".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO agent_workspace_mutation_journal ({','.join(columns)}) VALUES ({placeholders})",
            tuple(values[column] for column in columns),
        )

    clock = FixedClock()
    session_store, session, turn, claim = _running_turn(
        database, project_id, workspace_id, clock
    )
    ledger = SQLiteAgentOperationLedger(database, clock=clock)
    invocation = _invocation(
        session,
        turn,
        claim,
        project_id=project_id,
        workspace_id=workspace_id,
        call="seed-ambiguous",
    )
    intent = _seed_unknown(session_store, ledger, invocation, claim)

    reconciler = SQLiteAgentOperationReconciler(database, ledger=ledger, clock=clock)
    assert (
        reconciler.reconcile(
            ledger.get(intent.operation_key, owner="alice"),
            invocation=invocation,
            expected_fencing_token=claim.fencing_token,
        )
        is None
    )
    record = ledger.get(intent.operation_key, owner="alice")
    assert record.state is AgentOperationState.UNKNOWN
    assert record.reconciliation_attempt == 1
