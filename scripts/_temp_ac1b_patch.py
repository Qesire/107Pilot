from __future__ import annotations

from pathlib import Path


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def patch_gateway() -> None:
    path = Path("src/pilot107/agent/tool_gateway.py")
    text = path.read_text()
    text = replace_once(
        text,
        """from pilot107.agent.operation_ledger import (\n    AgentOperationConflict,\n    AgentOperationIntent,\n    AgentOperationLedger,\n    AgentOperationRecord,\n    AgentOperationState,\n    build_agent_operation_ledger,\n    operation_intent_for_invocation,\n)\nfrom pilot107.agent.project import is_project_agent_profile\n""",
        """from pilot107.agent.operation_ledger import (\n    AgentOperationConflict,\n    AgentOperationIntent,\n    AgentOperationLedger,\n    AgentOperationRecord,\n    AgentOperationState,\n    build_agent_operation_ledger,\n    operation_intent_for_invocation,\n)\nfrom pilot107.agent.operation_reconciler import (\n    AgentOperationReconciler,\n    build_agent_operation_reconciler,\n)\nfrom pilot107.agent.project import is_project_agent_profile\n""",
        label="reconciler import",
    )
    text = replace_once(
        text,
        """        profile_handlers: Mapping[str, Mapping[str, AgentReadHandler]] | None = None,\n        operation_ledger: AgentOperationLedger | None = None,\n        clock: Callable[[], datetime] | None = None,\n""",
        """        profile_handlers: Mapping[str, Mapping[str, AgentReadHandler]] | None = None,\n        operation_ledger: AgentOperationLedger | None = None,\n        operation_reconciler: AgentOperationReconciler | None = None,\n        clock: Callable[[], datetime] | None = None,\n""",
        label="gateway init signature",
    )
    text = replace_once(
        text,
        """        self.operation_ledger = operation_ledger or build_agent_operation_ledger(\n            store,\n            clock=self._clock,\n        )\n""",
        """        self.operation_ledger = operation_ledger or build_agent_operation_ledger(\n            store,\n            clock=self._clock,\n        )\n        self.operation_reconciler = operation_reconciler or build_agent_operation_reconciler(\n            store,\n            self.operation_ledger,\n            clock=self._clock,\n        )\n""",
        label="gateway reconciler wiring",
    )
    text = replace_once(
        text,
        """        arguments_digest = hashlib.sha256(_canonical(invocation.arguments)).hexdigest()\n        try:\n""",
        """        arguments_digest = hashlib.sha256(_canonical(invocation.arguments)).hexdigest()\n        operation_arguments_digest = _semantic_operation_digest(invocation)\n        try:\n""",
        label="semantic digest declaration",
    )
    text = replace_once(
        text,
        """        operation_intent = self._operation_intent(invocation, claims, arguments_digest)\n""",
        """        operation_intent = self._operation_intent(\n            invocation,\n            claims,\n            operation_arguments_digest,\n        )\n""",
        label="semantic digest use",
    )
    anchor = """        if record.state is AgentOperationState.FAILED:\n            self._replay_failed_operation(record, invocation, claims)\n        if record.state is AgentOperationState.RUNNING:\n"""
    replacement = """        if record.state is AgentOperationState.FAILED:\n            self._replay_failed_operation(record, invocation, claims)\n        if self.operation_reconciler is not None and (\n            record.state in {AgentOperationState.UNKNOWN, AgentOperationState.STALE}\n            or (\n                record.state is AgentOperationState.RUNNING\n                and record.origin_turn_id != invocation.turn_id\n            )\n        ):\n            try:\n                reconciled = self.operation_reconciler.reconcile(\n                    record,\n                    invocation=invocation,\n                    expected_fencing_token=claims.fencing_token,\n                )\n            except Exception:\n                # Recovery is fail-closed: a reconciler outage must never cause\n                # the mutation handler to be executed again.\n                reconciled = None\n            if reconciled is not None and reconciled.state is AgentOperationState.COMPLETED:\n                return self._replay_completed_operation(\n                    reconciled,\n                    invocation,\n                    claims,\n                    usage_bytes=usage_bytes,\n                )\n            record = self.operation_ledger.get(intent.operation_key, owner=invocation.owner)\n        if record.state is AgentOperationState.RUNNING:\n"""
    text = replace_once(text, anchor, replacement, label="reconciliation replay branch")
    text = replace_once(
        text,
        """\ndef _canonical(value: object) -> bytes:\n""",
        """\ndef _semantic_operation_digest(invocation: ToolInvocation) -> str:\n    # ``turn_id`` is an execution-carrier identity.  A durable domain request\n    # that resumes in a later Turn must still compare the same mutation intent.\n    arguments = dict(invocation.arguments)\n    arguments.pop("turn_id", None)\n    return hashlib.sha256(_canonical(arguments)).hexdigest()\n\n\ndef _canonical(value: object) -> bytes:\n""",
        label="semantic digest helper",
    )
    path.write_text(text)


def patch_reconciler() -> None:
    path = Path("src/pilot107/agent/operation_reconciler.py")
    text = path.read_text()
    text = text.replace('row.get("receipt_json")', '_row_value(row, "receipt_json")')
    text = text.replace('row.get(column)', '_row_value(row, column)')
    text = text.replace('row.get("linked_run_id")', '_row_value(row, "linked_run_id")')
    text = text.replace('row.get("schedule_receipt")', '_row_value(row, "schedule_receipt")')
    marker = """\ndef _json_mapping(value: object) -> dict[str, Any] | None:\n"""
    helper = """\ndef _row_value(row: Mapping[str, Any], key: str) -> Any:\n    return row[key]\n\n\ndef _json_mapping(value: object) -> dict[str, Any] | None:\n"""
    text = replace_once(text, marker, helper, label="row helper")
    path.write_text(text)


if __name__ == "__main__":
    patch_gateway()
    patch_reconciler()
