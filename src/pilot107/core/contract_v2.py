"""Canonical ContractV2 normalization and structural validation."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, TypeGuard

CONTRACT_SCHEMA_V2 = "pilot107.contract/v2"
LEGACY_CONTRACT_SCHEMA = "pilot107.contract/v1"

_TOP_LEVEL_FIELDS = {
    "schema_version",
    "recipe_version_id",
    "project",
    "entry",
    "runtime",
    "resources",
    "workflow",
    "outputs",
    "policy",
    "extensions",
}
_AUTOMATION_LEVELS = {"explain", "suggest", "approved_execute", "bounded_auto"}

# Round-8 P2-3: success conditions that the evaluation layer actually enforces.
# ``slurm_exit_code_zero`` is the only condition remediation's ``_evaluate_run``
# checks (via the run exit code). Any other condition is accepted by the
# schema today but silently ignored at evaluation time — the audit's
# "accept-then-ignore" anti-pattern. Reject unsupported conditions at
# normalize time so contracts can't declare conditions we won't enforce.
_SUPPORTED_SUCCESS_CONDITIONS = {"slurm_exit_code_zero"}


def parse_expected_output(item: Any) -> str:
    """Extract the relative path string from one ``outputs.expected`` entry.

    The ContractV2 schema allows expected outputs as either a plain path
    string (``"metrics.json"``) or a typed object (``{"path": "metrics.json",
    "type": "json"}``). Three call sites (evidence collector, baseline capture,
    remediation verifier) need the path string to match against inventory rows;
    naively calling ``str(item)`` on a dict turns it into a Python repr like
    ``"{'path': 'metrics.json', 'type': 'json'}"`` — a garbage path that never
    matches any real file. This shared parser is the single enforcement point
    for the dict shape at USE time.

    Rules:
      * ``str`` item → returned stripped; empty-after-strip raises.
      * ``dict`` item → return ``item["path"]`` if it's a non-empty string,
        else raise ``ContractV2Error(code="CONTRACT.OUTPUTS_INVALID")``.
      * any other type → raise the same error.

    NOTE: the ``type`` key on a dict item is RESERVED for future use (e.g.
    validating the file IS valid JSON when ``type == "json"``). It is accepted
    and currently IGNORED — do not implement type-based validation here, the
    audit only requires that object-type items extract their ``path`` instead
    of becoming garbage.
    """
    if isinstance(item, str):
        path = item.strip()
        if not path:
            raise ContractV2Error(
                "outputs.expected string path must be non-empty",
                code="CONTRACT.OUTPUTS_INVALID",
            )
        return path
    if isinstance(item, dict):
        raw_path: Any = item.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ContractV2Error(
                "outputs.expected typed object missing string 'path'",
                code="CONTRACT.OUTPUTS_INVALID",
            )
        return raw_path.strip()
    raise ContractV2Error(
        "outputs.expected entries must be a string or an object with a 'path' string",
        code="CONTRACT.OUTPUTS_INVALID",
    )


class ContractV2Error(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def normalize_contract(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a canonical V2 copy while preserving advanced section fields."""

    if not isinstance(payload, dict):
        raise ContractV2Error("contract must be an object", code="CONTRACT.INVALID_OBJECT")
    normalized = copy.deepcopy(payload)
    normalized.pop("owner", None)
    schema_version = normalized.get("schema_version")
    if schema_version is None:
        schema_version = LEGACY_CONTRACT_SCHEMA
    if schema_version not in {LEGACY_CONTRACT_SCHEMA, CONTRACT_SCHEMA_V2}:
        raise ContractV2Error(
            f"unsupported contract schema: {schema_version}",
            code="CONTRACT.SCHEMA_UNSUPPORTED",
        )
    unknown = sorted(set(normalized) - _TOP_LEVEL_FIELDS)
    if unknown:
        raise ContractV2Error(
            f"unknown top-level contract fields: {', '.join(unknown)}",
            code="CONTRACT.UNKNOWN_FIELD",
        )

    normalized["schema_version"] = CONTRACT_SCHEMA_V2
    for section in ("project", "entry", "resources"):
        value = normalized.get(section)
        if value is not None and not isinstance(value, dict):
            raise ContractV2Error(
                f"{section} must be an object",
                code=f"CONTRACT.{section.upper()}_INVALID",
            )

    runtime = _object_section(normalized, "runtime")
    workflow = _object_section(normalized, "workflow")
    outputs = _object_section(normalized, "outputs")
    policy = _object_section(normalized, "policy")
    _object_section(normalized, "extensions")

    entry = normalized.get("entry")
    if isinstance(entry, dict) and "expected_outputs" in entry and "expected" not in outputs:
        outputs["expected"] = copy.deepcopy(entry["expected_outputs"])

    runtime.setdefault("modules", [])
    runtime.setdefault("environment", {})
    workflow.setdefault("dependencies", [])
    retry = workflow.setdefault("retry", {})
    if isinstance(retry, dict):
        retry.setdefault("max_attempts", 1)
        retry.setdefault("backoff_seconds", 0)
    outputs.setdefault("expected", [])
    outputs.setdefault("success_conditions", ["slurm_exit_code_zero"])
    policy.setdefault("automation_level", "explain")
    policy.setdefault("max_remediation_attempts", 0)
    policy.setdefault("require_approval", True)

    resources = normalized.get("resources")
    if isinstance(resources, dict) and "memory" not in resources:
        memory_value = resources.pop("memory_value", None)
        memory_unit = resources.pop("memory_unit", None)
        if memory_value is not None:
            resources["memory"] = f"{memory_value}{memory_unit or 'M'}"

    _validate_runtime(runtime)
    _validate_workflow(workflow)
    _validate_outputs(outputs)
    _validate_policy(policy)
    return normalized


def contract_digest(payload: dict[str, Any]) -> str:
    canonical = normalize_contract(payload)
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def contract_v2_schema() -> dict[str, Any]:
    """Machine-readable field contract used by editors and Agent tooling."""

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": CONTRACT_SCHEMA_V2,
        "title": "107Pilot ContractV2",
        "type": "object",
        "required": ["schema_version", "recipe_version_id", "project", "entry", "resources"],
        "properties": {
            "schema_version": {"const": CONTRACT_SCHEMA_V2},
            "recipe_version_id": {"type": "string", "minLength": 1},
            "project": {
                "type": "object",
                "required": ["workdir"],
                "properties": {
                    "workdir": {"type": "string", "minLength": 1},
                    "name": {"type": "string"},
                },
                "additionalProperties": True,
            },
            "entry": {
                "type": "object",
                "required": ["command"],
                "properties": {
                    "command": {"type": "string", "minLength": 1},
                    "expected_outputs": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "additionalProperties": True,
            },
            "runtime": {
                "type": "object",
                "properties": {
                    "conda_env": {"type": ["string", "null"]},
                    "container_image": {"type": ["string", "null"]},
                    "modules": {"type": "array", "items": {"type": "string"}},
                    "environment": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "additionalProperties": True,
            },
            "resources": {
                "type": "object",
                "required": ["partition", "time_limit"],
                "properties": {
                    "partition": {"type": "string", "minLength": 1},
                    "qos": {"type": ["string", "null"]},
                    "nodes": {"type": "integer", "minimum": 1},
                    "ntasks": {"type": "integer", "minimum": 1},
                    "cpus_per_task": {"type": "integer", "minimum": 1},
                    "memory": {
                        "type": ["string", "integer", "null"],
                        "description": "Slurm memory value such as 4096M or 32G.",
                    },
                    "gpus_per_node": {"type": ["integer", "null"], "minimum": 0},
                    "gpus_total": {"type": ["integer", "null"], "minimum": 0},
                    "gpu_type": {"type": ["string", "null"]},
                    "time_limit": {
                        "type": "string",
                        "pattern": "^(?:\\d+-)?\\d{1,2}:\\d{2}:\\d{2}$",
                    },
                    "array": {
                        "type": ["object", "null"],
                        "required": ["expression"],
                        "properties": {
                            "expression": {"type": "string", "minLength": 1},
                            "max_concurrency": {
                                "type": ["integer", "null"],
                                "minimum": 1,
                            },
                        },
                        "additionalProperties": True,
                    },
                },
                "additionalProperties": True,
            },
            "workflow": {
                "type": "object",
                "properties": {
                    "dependencies": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "uniqueItems": True,
                    },
                    "retry": {
                        "type": "object",
                        "properties": {
                            "max_attempts": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 10,
                                "default": 1,
                            },
                            "backoff_seconds": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 86400,
                                "default": 0,
                            },
                        },
                        "additionalProperties": True,
                    },
                },
                "additionalProperties": True,
            },
            "outputs": {
                "type": "object",
                "properties": {
                    "expected": {
                        "type": "array",
                        "items": {"type": ["string", "object"]},
                    },
                    "success_conditions": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "additionalProperties": True,
            },
            "policy": {
                "type": "object",
                "properties": {
                    "automation_level": {
                        "type": "string",
                        "enum": sorted(_AUTOMATION_LEVELS),
                        "default": "explain",
                    },
                    "max_remediation_attempts": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 10,
                        "default": 0,
                    },
                    "require_approval": {"type": "boolean", "default": True},
                },
                "additionalProperties": True,
            },
            "extensions": {"type": "object"},
        },
        "additionalProperties": False,
    }


def _object_section(payload: dict[str, Any], name: str) -> dict[str, Any]:
    value = payload.setdefault(name, {})
    if not isinstance(value, dict):
        raise ContractV2Error(
            f"{name} must be an object",
            code=f"CONTRACT.{name.upper()}_INVALID",
        )
    return value


def _validate_runtime(runtime: dict[str, Any]) -> None:
    modules = runtime.get("modules")
    if not _string_list(modules):
        raise ContractV2Error(
            "runtime.modules must be an array of strings",
            code="CONTRACT.RUNTIME_MODULES_INVALID",
        )
    environment = runtime.get("environment")
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in environment.items()
    ):
        raise ContractV2Error(
            "runtime.environment must contain string keys and values",
            code="CONTRACT.RUNTIME_ENVIRONMENT_INVALID",
        )
    for field in ("conda_env", "container_image"):
        value = runtime.get(field)
        if value is not None and not isinstance(value, str):
            raise ContractV2Error(
                f"runtime.{field} must be a string or null",
                code="CONTRACT.RUNTIME_INVALID",
            )


def _validate_workflow(workflow: dict[str, Any]) -> None:
    raw_dependencies = workflow.get("dependencies")
    if not _string_list(raw_dependencies):
        raise ContractV2Error(
            "workflow.dependencies must contain unique non-empty run ids",
            code="CONTRACT.WORKFLOW_DEPENDENCIES_INVALID",
        )
    dependencies = list(raw_dependencies)
    if any(not item for item in dependencies) or len(set(dependencies)) != len(dependencies):
        raise ContractV2Error(
            "workflow.dependencies must contain unique non-empty run ids",
            code="CONTRACT.WORKFLOW_DEPENDENCIES_INVALID",
        )
    retry = workflow.get("retry")
    if not isinstance(retry, dict):
        raise ContractV2Error(
            "workflow.retry must be an object",
            code="CONTRACT.WORKFLOW_RETRY_INVALID",
        )
    max_attempts = retry.get("max_attempts", 1)
    backoff = retry.get("backoff_seconds", 0)
    if (
        not isinstance(max_attempts, int)
        or isinstance(max_attempts, bool)
        or max_attempts < 1
        or max_attempts > 10
        or not isinstance(backoff, int)
        or isinstance(backoff, bool)
        or backoff < 0
        or backoff > 86400
    ):
        raise ContractV2Error(
            "workflow retry values are invalid",
            code="CONTRACT.WORKFLOW_RETRY_INVALID",
        )


def _validate_outputs(outputs: dict[str, Any]) -> None:
    expected = outputs.get("expected")
    if not isinstance(expected, list) or not all(
        isinstance(item, (str, dict)) for item in expected
    ):
        raise ContractV2Error(
            "outputs.expected must contain paths or typed output objects",
            code="CONTRACT.OUTPUTS_INVALID",
        )
    # Round-8 P2-2: defensively enforce the dict shape at normalize time too
    # (the parser is the use-time enforcement, but catching bad contracts at
    # creation/update is the audit's "reject at creation" intent). A dict item
    # without a non-empty string ``path`` is rejected here.
    for item in expected:
        # Raises CONTRACT.OUTPUTS_INVALID on bad shape; safe to call for both
        # str and dict items.
        parse_expected_output(item)
    if not _string_list(outputs.get("success_conditions")):
        raise ContractV2Error(
            "outputs.success_conditions must be an array of strings",
            code="CONTRACT.OUTPUTS_INVALID",
        )
    # Round-8 P2-3: reject success conditions the evaluation layer does not
    # enforce. The default ``["slurm_exit_code_zero"]`` is valid; an empty
    # list is valid (no conditions, evaluation falls back to exit-code-only);
    # any unknown condition fails at normalize time so we stop accepting
    # conditions we won't enforce.
    conditions = outputs.get("success_conditions") or []
    unsupported = [c for c in conditions if c not in _SUPPORTED_SUCCESS_CONDITIONS]
    if unsupported:
        raise ContractV2Error(
            "unsupported outputs.success_conditions: "
            f"{', '.join(sorted(set(unsupported)))} "
            f"(supported: {sorted(_SUPPORTED_SUCCESS_CONDITIONS)})",
            code="CONTRACT.SUCCESS_CONDITION_UNSUPPORTED",
        )


def _validate_policy(policy: dict[str, Any]) -> None:
    if policy.get("automation_level") not in _AUTOMATION_LEVELS:
        raise ContractV2Error(
            "policy.automation_level is invalid",
            code="CONTRACT.POLICY_INVALID",
        )
    attempts = policy.get("max_remediation_attempts")
    approval = policy.get("require_approval")
    if (
        not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or attempts < 0
        or attempts > 10
        or not isinstance(approval, bool)
    ):
        raise ContractV2Error(
            "contract policy values are invalid",
            code="CONTRACT.POLICY_INVALID",
        )


def _string_list(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)
