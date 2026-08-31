from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "schemas"

GOLDENS: dict[str, dict[str, Any]] = {
    "agent/v2/project-session.schema.json": {
        "schema_version": "pilot107.experiment-project-session/v1",
        "project_id": "project-1",
        "owner": "alice",
        "origin": "blank",
        "state": "drafting",
        "version": 0,
        "goal": "Create a small numerical Slurm experiment.",
        "source": None,
        "blueprint": {
            "goal": "Read parameters and sum a numeric series.",
            "entrypoints": ["main.py"],
            "files": [
                {
                    "path": "main.py",
                    "purpose": "Experiment entrypoint",
                    "classification": "editable",
                }
            ],
            "validations": [
                {
                    "validation_id": "syntax",
                    "execution": "sandbox",
                    "argv": ["python", "-m", "py_compile", "main.py"],
                    "expected_outputs": [],
                }
            ],
            "contract_intent": {
                "recipe_version_id": "recipe_python_cpu@1.0.0",
                "resource_hints": {"cpus_per_task": 1, "time_limit": "00:05:00"},
            },
            "expected_outputs": [
                {"path": "result.json", "kind": "json", "required": True}
            ],
            "dependencies": [
                {"name": "python", "version": ">=3.12", "source": "runtime"}
            ],
            "open_questions": [],
        },
        "created_at": "2026-08-19T00:00:00Z",
        "updated_at": "2026-08-19T00:00:00Z",
    },
    "agent/v2/workspace-changeset.schema.json": {
        "schema_version": "pilot107.workspace-changeset/v1",
        "change_set_id": "changeset-1",
        "project_id": "project-1",
        "workspace_id": "workspace-1",
        "owner": "alice",
        "base_snapshot_digest": "a" * 64,
        "digest": "b" * 64,
        "state": "reviewable",
        "version": 2,
        "files": [
            {
                "path": "main.py",
                "operation": "create",
                "before_sha256": None,
                "after_sha256": "c" * 64,
                "diff_sha256": "d" * 64,
                "size_bytes": 128,
            }
        ],
        "sandbox_results": [
            {
                "result_id": "sandbox-1",
                "argv": ["python", "-m", "py_compile", "main.py"],
                "status": "succeeded",
                "exit_code": 0,
                "stdout_sha256": "e" * 64,
                "stderr_sha256": "f" * 64,
            }
        ],
        "approval": None,
        "created_at": "2026-08-19T00:00:00Z",
        "updated_at": "2026-08-19T00:01:00Z",
    },
    "agent/v2/agent-task.schema.json": {
        "schema_version": "pilot107.agent-task/v1",
        "task_id": "task-1",
        "owner": "alice",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "project_id": "project-1",
        "workspace_id": "workspace-1",
        "task_kind": "slurm_validation",
        "state": "pending",
        "version": 0,
        "request_key": "validate-1",
        "cancel_requested": False,
        "resource_envelope": {
            "partition": "debug",
            "qos": "normal",
            "cpus": 1,
            "memory_mib": 1024,
            "gpu_type": None,
            "gpus": 0,
            "walltime_seconds": 300,
            "max_tasks": 1,
            "max_submissions": 1,
            "workspace_snapshot_digest": "a" * 64,
            "expires_at": "2026-08-19T01:00:00Z",
            "approved_by": "alice",
        },
        "linked_run_id": None,
        "result": None,
        "lease": None,
        "created_at": "2026-08-19T00:00:00Z",
        "updated_at": "2026-08-19T00:00:00Z",
    },
    "runtime-watch/v1/runtime-watch.schema.json": {
        "schema_version": "pilot107.runtime-watch/v1",
        "watch_id": "watch-1",
        "run_id": "run-1",
        "owner": "alice",
        "connection_id": "simulator",
        "state": "watching",
        "version": 0,
        "next_poll_at": "2026-08-19T00:00:05Z",
        "lease_owner": None,
        "lease_expires_at": None,
        "fencing_token": 0,
        "cursors": [
            {
                "stream": "stdout",
                "generation": 0,
                "offset": 0,
                "source_size": 0,
                "source_mtime": None,
                "source_file_identity": None,
                "source_prefix_fingerprint": None,
                "decoder_remainder_base64": "",
                "last_data_at": None,
                "last_checked_at": None,
                "quiet_polls": 0,
                "version": 0,
            }
        ],
        "created_at": "2026-08-19T00:00:00Z",
        "updated_at": "2026-08-19T00:00:00Z",
        "stopped_at": None,
        "last_error_code": None,
        "last_error_at": None,
    },
    "observability/v1/resource-observation.schema.json": {
        "schema_version": "pilot107.resource-observation/v1",
        "observation_id": "observation-1",
        "kind": "run_resource_summary",
        "connection_id": "simulator",
        "owner": "alice",
        "run_id": "run-1",
        "attempt": 0,
        "cycle_id": "cycle-1",
        "captured_at": "2026-08-19T00:05:00Z",
        "freshness": "terminal",
        "partial": False,
        "warnings": [],
        "measures": {
            "allocated_cpus": {
                "value": 1,
                "unit": "count",
                "availability": "available",
                "source_adapter": "slurm_cli",
                "source_operation": "sacct",
                "captured_at": "2026-08-19T00:05:00Z",
                "quality": "verified",
                "coverage": 1.0,
                "warning": None,
            },
            "gpu_utilization": {
                "value": None,
                "unit": "percent",
                "availability": "unsupported",
                "source_adapter": "slurm_cli",
                "source_operation": "sacct",
                "captured_at": "2026-08-19T00:05:00Z",
                "quality": "unavailable",
                "coverage": None,
                "warning": "GPU accounting is not configured.",
            },
        },
        "evaluations": [],
    },
}


@pytest.mark.parametrize("relative", sorted(GOLDENS))
def test_lifecycle_schema_accepts_its_golden_payload(relative: str) -> None:
    schema_path = SCHEMA_ROOT / relative
    assert schema_path.is_file(), f"missing lifecycle schema: {relative}"

    from jsonschema import Draft202012Validator

    schema = json.loads(schema_path.read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(GOLDENS[relative])


@pytest.mark.parametrize("relative", sorted(GOLDENS))
def test_lifecycle_schemas_reject_unknown_authority_fields(relative: str) -> None:
    schema_path = SCHEMA_ROOT / relative
    assert schema_path.is_file(), f"missing lifecycle schema: {relative}"

    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import ValidationError

    payload = copy.deepcopy(GOLDENS[relative])
    payload["authorization"] = "Bearer secret"
    schema = json.loads(schema_path.read_text())

    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(payload)


@pytest.mark.parametrize(
    ("relative", "field", "invalid"),
    [
        ("agent/v2/project-session.schema.json", "origin", "imported_magic"),
        ("agent/v2/workspace-changeset.schema.json", "state", "silently_published"),
        ("agent/v2/agent-task.schema.json", "state", "waiting_forever"),
        ("runtime-watch/v1/runtime-watch.schema.json", "state", "auto_fixing"),
        ("observability/v1/resource-observation.schema.json", "kind", "raw_shell"),
    ],
)
def test_lifecycle_schemas_reject_unknown_discriminants(
    relative: str,
    field: str,
    invalid: str,
) -> None:
    schema_path = SCHEMA_ROOT / relative
    assert schema_path.is_file(), f"missing lifecycle schema: {relative}"

    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import ValidationError

    payload = copy.deepcopy(GOLDENS[relative])
    payload[field] = invalid
    schema = json.loads(schema_path.read_text())

    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(payload)


def test_agent_task_schema_accepts_schedule_and_terminal_gate_receipts() -> None:
    from jsonschema import Draft202012Validator

    payload = copy.deepcopy(GOLDENS["agent/v2/agent-task.schema.json"])
    payload.update(
        {
            "completion_policy": "evidence_required",
            "gate_state": "awaiting_evidence",
            "schedule_receipt": {
                "receipt_id": "receipt-1",
                "task_id": "task-1",
                "owner": "alice",
                "session_id": "session-1",
                "originating_turn_id": "turn-1",
                "request_digest": "a" * 64,
                "idempotency_key": "validate-1",
                "run_id": "run-1",
                "submit_state": "admitted",
                "slurm_job_id": None,
                "resource_envelope_id": "envelope-1",
                "workspace_revision": None,
                "workspace_digest": "b" * 64,
                "completion_policy": "evidence_required",
                "created_at": "2026-08-19T00:00:00Z",
                "legacy_boundary": True,
            },
            "gate_receipt": None,
            "legacy_gate_unverified": True,
        }
    )
    schema = json.loads(
        (SCHEMA_ROOT / "agent/v2/agent-task.schema.json").read_text()
    )

    Draft202012Validator(schema).validate(payload)


def test_agent_task_schema_accepts_optional_evidence_seal_binding() -> None:
    from jsonschema import Draft202012Validator

    payload = copy.deepcopy(GOLDENS["agent/v2/agent-task.schema.json"])
    payload.update(
        {
            "completion_policy": "evidence_required",
            "gate_state": "completed",
            "schedule_receipt": None,
            "gate_receipt": {
                "task_id": "task-1",
                "run_id": "run-1",
                "run_terminal_state": "completed",
                "evidence_state": "finalized",
                "evidence_refs": ["evidence-1"],
                "evidence_digest": "a" * 64,
                "integrity_verified_at": "2026-08-19T00:05:00Z",
                "integrity_state": "verified",
                "workspace_revision": None,
                "workspace_digest": "b" * 64,
                "legacy_boundary": True,
                "capsule_ref": None,
                "capsule_state": "not_required",
                "seal_digest": "c" * 64,
                "seal_marker_ref": "evidence-seal://runs/run-1/seal.json",
            },
            "legacy_gate_unverified": False,
        }
    )
    schema = json.loads((SCHEMA_ROOT / "agent/v2/agent-task.schema.json").read_text())

    Draft202012Validator(schema).validate(payload)


def test_agent_task_schema_rejects_terminal_gate_receipt_without_integrity_timestamp() -> None:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import ValidationError

    payload = copy.deepcopy(GOLDENS["agent/v2/agent-task.schema.json"])
    payload.update(
        {
            "completion_policy": "evidence_required",
            "gate_state": "completed",
            "schedule_receipt": None,
            "gate_receipt": {
                "run_id": "run-1",
                "evidence_refs": ["evidence-1"],
                "evidence_digest": "a" * 64,
                "integrity_verified_at": None,
                "workspace_revision": None,
                "workspace_digest": "b" * 64,
                "legacy_boundary": True,
                "capsule_ref": None,
                "capsule_state": "not_required",
            },
            "legacy_gate_unverified": False,
        }
    )
    schema = json.loads(
        (SCHEMA_ROOT / "agent/v2/agent-task.schema.json").read_text()
    )

    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(payload)


def test_new_agent_task_payload_requires_all_gate_fields() -> None:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import ValidationError

    payload = copy.deepcopy(GOLDENS["agent/v2/agent-task.schema.json"])
    payload["completion_policy"] = "evidence_required"
    schema = json.loads(
        (SCHEMA_ROOT / "agent/v2/agent-task.schema.json").read_text()
    )

    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(payload)


def test_agent_task_schema_restricts_terminal_gate_states() -> None:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import ValidationError

    schema = json.loads(
        (SCHEMA_ROOT / "agent/v2/agent-task.schema.json").read_text()
    )
    for field, invalid in (("evidence_state", "collected"), ("integrity_state", "pending")):
        payload = copy.deepcopy(GOLDENS["agent/v2/agent-task.schema.json"])
        payload.update(
            {
                "completion_policy": "evidence_required",
                "gate_state": "completed",
                "schedule_receipt": None,
                "gate_receipt": {
                    "run_id": "run-1",
                    "evidence_refs": ["evidence-1"],
                    "evidence_digest": "a" * 64,
                    "integrity_verified_at": "2026-08-19T00:05:00Z",
                    "workspace_revision": None,
                    "workspace_digest": "b" * 64,
                    "legacy_boundary": True,
                    "capsule_ref": None,
                    "capsule_state": "not_required",
                    "task_id": "task-1",
                    "run_terminal_state": "completed",
                    field: invalid,
                },
                "legacy_gate_unverified": False,
            }
        )
        with pytest.raises(ValidationError):
            Draft202012Validator(schema).validate(payload)


@pytest.mark.parametrize(
    ("capsule_ref", "capsule_state"), [(None, "READY"), ("capsule-1", "not_required")]
)
def test_agent_task_schema_restricts_capsule_state_and_ref(
    capsule_ref: str | None, capsule_state: str
) -> None:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import ValidationError

    payload = copy.deepcopy(GOLDENS["agent/v2/agent-task.schema.json"])
    payload.update(
        {
            "completion_policy": "evidence_required",
            "gate_state": "completed",
            "schedule_receipt": None,
            "gate_receipt": {
                "run_id": "run-1",
                "evidence_refs": ["evidence-1"],
                "evidence_digest": "a" * 64,
                "integrity_verified_at": "2026-08-19T00:05:00Z",
                "workspace_revision": None,
                "workspace_digest": "b" * 64,
                "legacy_boundary": True,
                "capsule_ref": capsule_ref,
                "capsule_state": capsule_state,
                "task_id": "task-1",
                "run_terminal_state": "completed",
            },
            "legacy_gate_unverified": False,
        }
    )
    schema = json.loads(
        (SCHEMA_ROOT / "agent/v2/agent-task.schema.json").read_text()
    )

    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(payload)


@pytest.mark.parametrize("run_terminal_state", ["completed", "failed", "cancelled", "orphaned"])
def test_agent_task_schema_accepts_each_wire_run_terminal_state(
    run_terminal_state: str,
) -> None:
    from jsonschema import Draft202012Validator

    payload = copy.deepcopy(GOLDENS["agent/v2/agent-task.schema.json"])
    payload.update(
        {
            "completion_policy": "evidence_required",
            "gate_state": "completed",
            "schedule_receipt": None,
            "gate_receipt": {
                "task_id": "task-1",
                "run_id": "run-1",
                "run_terminal_state": run_terminal_state,
                "evidence_state": "finalized",
                "evidence_refs": ["evidence-1"],
                "evidence_digest": "a" * 64,
                "integrity_verified_at": "2026-08-19T00:05:00Z",
                "integrity_state": "verified",
                "workspace_revision": None,
                "workspace_digest": "b" * 64,
                "legacy_boundary": True,
                "capsule_ref": None,
                "capsule_state": "not_required",
            },
            "legacy_gate_unverified": False,
        }
    )
    schema = json.loads(
        (SCHEMA_ROOT / "agent/v2/agent-task.schema.json").read_text()
    )
    Draft202012Validator(schema).validate(payload)


@pytest.mark.parametrize("run_terminal_state", ["running", "bogus"])
def test_agent_task_schema_rejects_non_terminal_run_state(run_terminal_state: str) -> None:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import ValidationError

    payload = copy.deepcopy(GOLDENS["agent/v2/agent-task.schema.json"])
    payload.update(
        {
            "completion_policy": "evidence_required",
            "gate_state": "completed",
            "schedule_receipt": None,
            "gate_receipt": {
                "task_id": "task-1",
                "run_id": "run-1",
                "run_terminal_state": run_terminal_state,
                "evidence_state": "finalized",
                "evidence_refs": ["evidence-1"],
                "evidence_digest": "a" * 64,
                "integrity_verified_at": "2026-08-19T00:05:00Z",
                "integrity_state": "verified",
                "workspace_revision": None,
                "workspace_digest": "b" * 64,
                "legacy_boundary": True,
                "capsule_ref": None,
                "capsule_state": "not_required",
            },
            "legacy_gate_unverified": False,
        }
    )
    schema = json.loads(
        (SCHEMA_ROOT / "agent/v2/agent-task.schema.json").read_text()
    )
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(payload)


def test_agent_task_schema_requires_ready_capsule_for_capsule_policy() -> None:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import ValidationError

    payload = copy.deepcopy(GOLDENS["agent/v2/agent-task.schema.json"])
    payload.update(
        {
            "completion_policy": "evidence_and_capsule_required",
            "gate_state": "completed",
            "schedule_receipt": None,
            "gate_receipt": {
                "task_id": "task-1",
                "run_id": "run-1",
                "run_terminal_state": "completed",
                "evidence_state": "finalized",
                "evidence_refs": ["evidence-1"],
                "evidence_digest": "a" * 64,
                "integrity_verified_at": "2026-08-19T00:05:00Z",
                "integrity_state": "verified",
                "workspace_revision": None,
                "workspace_digest": "b" * 64,
                "legacy_boundary": True,
                "capsule_ref": None,
                "capsule_state": "not_required",
            },
            "legacy_gate_unverified": False,
        }
    )
    schema = json.loads(
        (SCHEMA_ROOT / "agent/v2/agent-task.schema.json").read_text()
    )

    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(payload)


def test_agent_task_schema_allows_evidence_policy_with_optional_capsule() -> None:
    from jsonschema import Draft202012Validator

    payload = copy.deepcopy(GOLDENS["agent/v2/agent-task.schema.json"])
    payload.update(
        {
            "completion_policy": "evidence_required",
            "gate_state": "completed",
            "schedule_receipt": None,
            "gate_receipt": {
                "task_id": "task-1",
                "run_id": "run-1",
                "run_terminal_state": "completed",
                "evidence_state": "finalized",
                "evidence_refs": ["evidence-1"],
                "evidence_digest": "a" * 64,
                "integrity_verified_at": "2026-08-19T00:05:00Z",
                "integrity_state": "verified",
                "workspace_revision": None,
                "workspace_digest": "b" * 64,
                "legacy_boundary": True,
                "capsule_ref": "capsule-1",
                "capsule_state": "READY",
            },
            "legacy_gate_unverified": False,
        }
    )
    schema = json.loads(
        (SCHEMA_ROOT / "agent/v2/agent-task.schema.json").read_text()
    )

    Draft202012Validator(schema).validate(payload)
