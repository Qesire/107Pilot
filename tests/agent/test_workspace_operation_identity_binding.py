from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pilot107.agent.operation_context import bind_agent_operation_key
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
from pilot107.services.project_agent_service import ProjectAgentService


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 5, 3, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


def _semantic_digest(arguments: dict[str, object]) -> str:
    encoded = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def test_operation_bound_journal_recovers_second_identical_file_plan(tmp_path: Path) -> None:
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
        goal="bind repeated workspace receipt",
        request_key="project-request",
    )
    project_id = view.project.project_id
    workspace_id = view.workspace.workspace_id

    first = service.apply_patches(
        project_id=project_id,
        workspace_id=workspace_id,
        owner="alice",
        patches=(("script.py", None, "create", "value = 1\n"),),
    )
    source_digest = hashlib.sha256(b"value = 1\n").hexdigest()
    service.apply_patches(
        project_id=project_id,
        workspace_id=workspace_id,
        owner="alice",
        patches=(("script.py", source_digest, "delete", None),),
    )

    clock = FixedClock()
    session_store = SQLiteAgentSessionStore(database, clock=clock)
    session, _ = session_store.create_session(
        owner="alice",
        request_key="session-request",
        profile_id="experiment_builder",
        model_profile_id="faux-default",
        source={"project_id": project_id, "workspace_id": workspace_id},
    )
    turn, _ = session_store.create_turn(
        session_id=session.session_id,
        owner="alice",
        request_key="turn-second-create",
        message="create the same script again",
        expected_state_version=session.state_version,
    )
    claim = session_store.claim_turn(turn.turn_id, worker_id="worker-1", lease_seconds=120)
    assert claim is not None
    arguments: dict[str, object] = {
        "project_id": project_id,
        "workspace_id": workspace_id,
        "approval_summary_zh": "再次创建同内容验证脚本。",
        "patches": [
            {
                "path": "script.py",
                "expected_source_digest": None,
                "operation": "create",
                "content": "value = 1\n",
            }
        ],
    }
    invocation = ToolInvocation(
        schema_version=TOOL_INVOCATION_PROTOCOL_VERSION,
        invocation_id="invocation-second-create",
        idempotency_key="idempotency-second-create",
        owner="alice",
        session_id=session.session_id,
        turn_id=turn.turn_id,
        state_version=claim.state_version,
        profile_id="experiment_builder",
        tool_name="workspace_patch",
        arguments=arguments,
        deadline="2026-09-05T03:01:30Z",
    )
    ledger = SQLiteAgentOperationLedger(database, clock=clock)
    intent = operation_intent_for_invocation(
        session_store,
        invocation,
        arguments_digest=_semantic_digest(arguments),
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

    with bind_agent_operation_key(intent.operation_key):
        second = service.apply_patches(
            project_id=project_id,
            workspace_id=workspace_id,
            owner="alice",
            patches=(("script.py", None, "create", "value = 1\n"),),
        )
    assert second.digest == first.digest
    assert second.change_set_id != first.change_set_id

    ledger.mark_unknown(
        intent.operation_key,
        owner="alice",
        expected_state_version=claim.state_version,
        expected_fencing_token=claim.fencing_token,
        error={"code": "AGENT.TOOL.OPERATION_UNKNOWN", "message": "simulated crash"},
    )
    reconciler = SQLiteAgentOperationReconciler(database, ledger=ledger, clock=clock)
    resolved = reconciler.reconcile(
        ledger.get(intent.operation_key, owner="alice"),
        invocation=invocation,
        expected_fencing_token=claim.fencing_token,
    )

    assert resolved is not None
    assert resolved.state is AgentOperationState.COMPLETED
    assert resolved.side_effect_ref == f"changeset:{second.change_set_id}"
    assert resolved.result is not None
    assert resolved.result["result"]["change_set_id"] == second.change_set_id
