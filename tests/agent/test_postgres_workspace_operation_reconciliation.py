from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pilot107.agent.operation_context import bind_agent_operation_key
from pilot107.agent.operation_ledger import (
    AgentOperationState,
    PostgresAgentOperationLedger,
    operation_intent_for_invocation,
)
from pilot107.agent.operation_reconciler import PostgresAgentOperationReconciler
from pilot107.agent.postgres_project_store import PostgresProjectStore
from pilot107.agent.postgres_store import PostgresAgentSessionStore
from pilot107.agent.postgres_workspace_atomic import PostgresAtomicDurableWorkspaceEditor
from pilot107.agent.project import ExperimentProjectOrigin
from pilot107.agent.protocol import TOOL_INVOCATION_PROTOCOL_VERSION, ToolInvocation
from pilot107.agent.workspace import AgentWorkspaceRecord, WorkspacePatch, WorkspaceSnapshot


PG_ENABLED = bool(
    os.environ.get("PILOT107_TEST_POSTGRES_DSN")
    and os.environ.get("PILOT107_TEST_POSTGRES_ALLOW_RESET") == "1"
)


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 5, 4, 0, tzinfo=UTC)

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


def _reset(editor: PostgresAtomicDurableWorkspaceEditor) -> None:
    with editor.journal_store.connect() as connection:
        connection.execute(
            "TRUNCATE agent_operations, agent_workspace_mutation_journal, "
            "agent_workspace_live_heads, agent_workspace_changesets, agent_workspaces, "
            "agent_experiment_projects, agent_turn_events, agent_tool_invocations, "
            "agent_turns, agent_sessions RESTART IDENTITY CASCADE"
        )


@pytest.mark.skipif(
    not PG_ENABLED,
    reason="set a dedicated PostgreSQL DSN and explicit reset opt-in",
)
def test_committed_postgres_workspace_journal_recovers_unknown_operation(
    tmp_path: Path,
) -> None:
    dsn = os.environ["PILOT107_TEST_POSTGRES_DSN"]
    clock = FixedClock()
    project_store = PostgresProjectStore(dsn)
    session_store = PostgresAgentSessionStore(dsn, clock=clock)
    ledger = PostgresAgentOperationLedger(dsn, clock=clock)
    editor = PostgresAtomicDurableWorkspaceEditor(
        store=project_store,
        state_root=tmp_path / "agent-workspace-state",
        clock=clock,
    )
    _reset(editor)

    project = project_store.create_project(
        owner="alice",
        origin=ExperimentProjectOrigin.BLANK,
        goal="recover a committed Agent Workspace mutation",
        request_key="pg-reconcile-project",
    )
    root = tmp_path / "agent-workspaces" / "alice" / "workspace-pg-reconcile"
    root.mkdir(parents=True)
    now = clock().isoformat().replace("+00:00", "Z")
    workspace = project_store.save_workspace(
        AgentWorkspaceRecord(
            workspace_id="workspace-pg-reconcile",
            project_id=project.project_id,
            owner="alice",
            local_root=str(root),
            snapshot=WorkspaceSnapshot(
                source_ref="/__pilot107_blank__/pg-reconcile-project",
                digest="a" * 64,
                entries=(),
                captured_at=now,
            ),
            created_at=now,
            updated_at=now,
        )
    )
    session, _ = session_store.create_session(
        owner="alice",
        request_key="pg-reconcile-session",
        profile_id="experiment_builder",
        model_profile_id="faux-default",
        source={"project_id": project.project_id, "workspace_id": workspace.workspace_id},
    )
    turn, _ = session_store.create_turn(
        session_id=session.session_id,
        owner="alice",
        request_key="pg-reconcile-turn",
        message="create the validation script",
        expected_state_version=session.state_version,
    )
    claim = session_store.claim_turn(
        turn.turn_id,
        worker_id="worker-pg-reconcile",
        lease_seconds=120,
    )
    assert claim is not None

    arguments: dict[str, object] = {
        "project_id": project.project_id,
        "workspace_id": workspace.workspace_id,
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
    invocation = ToolInvocation(
        schema_version=TOOL_INVOCATION_PROTOCOL_VERSION,
        invocation_id="invocation-pg-reconcile",
        idempotency_key="idempotency-pg-reconcile",
        owner="alice",
        session_id=session.session_id,
        turn_id=turn.turn_id,
        state_version=claim.state_version,
        profile_id="experiment_builder",
        tool_name="workspace_patch",
        arguments=arguments,
        deadline="2026-09-05T04:01:30Z",
    )
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

    # This is the AC1/AC4 crash boundary: the domain mutation is committed under
    # the operation key, but the Gateway-side operation receipt is not yet durable.
    with bind_agent_operation_key(intent.operation_key):
        change_set = editor.apply_patches(
            workspace.workspace_id,
            "alice",
            (("script.py", None, WorkspacePatch(operation="create", content="value = 1\n")),),
        )
    ledger.mark_unknown(
        intent.operation_key,
        owner="alice",
        expected_state_version=claim.state_version,
        expected_fencing_token=claim.fencing_token,
        error={"code": "AGENT.TOOL.OPERATION_UNKNOWN", "message": "simulated crash"},
    )

    reconciler = PostgresAgentOperationReconciler(dsn, ledger=ledger, clock=clock)
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
    assert payload["workspace_id"] == workspace.workspace_id
    assert payload["project_id"] == project.project_id
    assert payload["state"] == "draft"
    assert payload["version"] == 1
    assert payload["sandbox_results"] == []
    assert payload["approval"] is None
    assert payload["approval_summary_zh"] == "创建验证脚本。"
    assert (root / "script.py").read_text() == "value = 1\n"

    with editor.journal_store.connect() as connection:
        journal = connection.execute(
            """
            SELECT state, request_key, change_set_id
            FROM agent_workspace_mutation_journal
            WHERE workspace_id = %s
            """,
            (workspace.workspace_id,),
        ).fetchone()
    assert journal is not None
    assert journal["state"] == "committed"
    assert journal["request_key"] == intent.operation_key
    assert journal["change_set_id"] == change_set.change_set_id
