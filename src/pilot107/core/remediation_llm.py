"""Provider-neutral, fail-closed LLM proposals for remediation sessions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from pilot107.agent.client import AgentdClient
from pilot107.agent.protocol import AgentdClientError
from pilot107.agent.providers import AgentdConstrainedProvider
from pilot107.core.agent import AgentFact

REMEDIATION_PLAN_SCHEMA_VERSION = "pilot107.remediation-plan/v1"
SUPPORTED_REMEDIATION_ACTIONS = frozenset(
    {
        "path_probe",
        "runtime_probe",
        "contract_patch",
        "environment_select",
        "retry_run",
        "dependency_plan",
    }
)
PATCHABLE_CONTRACT_FIELDS = frozenset(
    {
        "entry.workdir",
        "environment.kind",
        "environment.name",
        "resources.partition",
        "resources.qos",
        "resources.cpus",
        "resources.gpus",
        "resources.memory",
        "resources.time_limit",
        "success.expected_outputs",
    }
)

REMEDIATION_PLAN_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schema_version": {"const": REMEDIATION_PLAN_SCHEMA_VERSION},
        "summary": {"type": "string"},
        "fact_ids": {"type": "array", "items": {"type": "string"}},
        "required_inputs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["key", "reason"],
                "additionalProperties": False,
            },
        },
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "proposal_key": {"type": "string"},
                    "action_type": {"type": "string"},
                    "rationale": {"type": "string"},
                    "evidence_fact_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "parameters": {"type": "object"},
                },
                "required": [
                    "proposal_key",
                    "action_type",
                    "rationale",
                    "evidence_fact_ids",
                    "parameters",
                ],
                "additionalProperties": False,
            },
        },
        "stop_conditions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "schema_version",
        "summary",
        "fact_ids",
        "required_inputs",
        "proposals",
        "stop_conditions",
    ],
    "additionalProperties": False,
}


class RemediationPlanError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RequiredInput:
    key: str
    reason: str


@dataclass(frozen=True)
class RemediationPlanProposal:
    proposal_key: str
    action_type: str
    rationale: str
    evidence_fact_ids: tuple[str, ...]
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RemediationPlan:
    schema_version: str
    summary: str
    fact_ids: tuple[str, ...]
    required_inputs: tuple[RequiredInput, ...]
    proposals: tuple[RemediationPlanProposal, ...]
    stop_conditions: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "summary": self.summary,
            "fact_ids": list(self.fact_ids),
            "required_inputs": [vars(item) for item in self.required_inputs],
            "proposals": [
                {
                    "proposal_key": item.proposal_key,
                    "action_type": item.action_type,
                    "rationale": item.rationale,
                    "evidence_fact_ids": list(item.evidence_fact_ids),
                    "parameters": item.parameters,
                }
                for item in self.proposals
            ],
            "stop_conditions": list(self.stop_conditions),
        }


@dataclass(frozen=True)
class RemediationPlanningContext:
    run_id: str
    facts: tuple[AgentFact, ...]
    allowed_action_types: frozenset[str] = SUPPORTED_REMEDIATION_ACTIONS
    allowed_patch_fields: frozenset[str] = PATCHABLE_CONTRACT_FIELDS

    def prompt_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "facts": [
                {
                    "fact_id": fact.fact_id,
                    "statement": fact.statement,
                    "evidence_object_ids": list(fact.evidence_object_ids),
                    "confidence": fact.confidence,
                }
                for fact in self.facts
            ],
            "policy": {
                "allowed_action_types": sorted(self.allowed_action_types),
                "allowed_contract_patch_fields": sorted(self.allowed_patch_fields),
                "arbitrary_shell": False,
                "proposal_is_execution_authority": False,
            },
        }


class RemediationPlanProvider(Protocol):
    provider_name: str
    model: str
    owns_format_repair: bool

    def propose(
        self,
        context: RemediationPlanningContext,
        *,
        format_repair: bool = False,
    ) -> str: ...


class RemediationPlanService:
    def __init__(self, *, provider: RemediationPlanProvider, max_attempts: int = 2) -> None:
        if max_attempts <= 0 or max_attempts > 2:
            raise ValueError("max_attempts must be one or two")
        self.provider = provider
        self.max_attempts = max_attempts

    def plan(self, context: RemediationPlanningContext) -> RemediationPlan:
        if not context.facts:
            raise RemediationPlanError(
                "remediation planning requires evidence-bound facts",
                code="insufficient_evidence",
            )
        last_error: RemediationPlanError | None = None
        attempts = 1 if getattr(self.provider, "owns_format_repair", False) else self.max_attempts
        for attempt in range(attempts):
            try:
                raw = self.provider.propose(context, format_repair=attempt > 0)
                plan = parse_remediation_plan(raw)
                validate_remediation_plan(plan, context)
                return plan
            except RemediationPlanError as exc:
                last_error = exc
                if not _retryable_plan_error(exc):
                    raise
        if last_error is None:
            raise RuntimeError("remediation plan attempt loop produced no result")
        raise last_error


class ReplayRemediationPlanProvider:
    provider_name = "replay"
    model = "fixture"
    owns_format_repair = False

    def __init__(self, responses: list[str | RemediationPlanError]) -> None:
        if not responses:
            raise ValueError("at least one replay response is required")
        self.responses = responses
        self.calls = 0

    def propose(
        self,
        context: RemediationPlanningContext,
        *,
        format_repair: bool = False,
    ) -> str:
        del context, format_repair
        value = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        if isinstance(value, RemediationPlanError):
            raise value
        return value


class OpenAICompatibleRemediationPlanProvider:
    provider_name = "openai-compatible"
    owns_format_repair = True

    def __init__(self, *, client: AgentdClient) -> None:
        self.client = client
        self.model = client.config.model_profile_id
        self._provider = AgentdConstrainedProvider(client)

    def propose(
        self,
        context: RemediationPlanningContext,
        *,
        format_repair: bool = False,
    ) -> str:
        del format_repair
        try:
            terminal = self._provider.invoke("remediation_plan", context.prompt_payload())
        except AgentdClientError as exc:
            raise RemediationPlanError(
                "pilot-agentd remediation failed",
                code=_remediation_error_code(exc),
            ) from None
        self.model = terminal.model
        return json.dumps(terminal.result, ensure_ascii=False, sort_keys=True)


def parse_remediation_plan(raw: str) -> RemediationPlan:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RemediationPlanError("remediation plan is not JSON", code="invalid_json") from exc
    if not isinstance(value, dict):
        raise RemediationPlanError("remediation plan must be an object", code="invalid_schema")
    expected = {
        "schema_version",
        "summary",
        "fact_ids",
        "required_inputs",
        "proposals",
        "stop_conditions",
    }
    if set(value) != expected or value.get("schema_version") != REMEDIATION_PLAN_SCHEMA_VERSION:
        raise RemediationPlanError("remediation plan fields are invalid", code="invalid_schema")
    summary = _required_text(value["summary"], "summary")
    fact_ids = _string_tuple(value["fact_ids"], "fact_ids")
    stop_conditions = _string_tuple(value["stop_conditions"], "stop_conditions")
    required_inputs_raw = value["required_inputs"]
    proposals_raw = value["proposals"]
    if not isinstance(required_inputs_raw, list) or not isinstance(proposals_raw, list):
        raise RemediationPlanError("plan arrays are invalid", code="invalid_schema")
    if len(required_inputs_raw) > 20 or len(proposals_raw) > 10:
        raise RemediationPlanError("plan exceeds item limits", code="invalid_schema")
    required_inputs: list[RequiredInput] = []
    for item in required_inputs_raw:
        if not isinstance(item, dict) or set(item) != {"key", "reason"}:
            raise RemediationPlanError("required input is invalid", code="invalid_schema")
        required_inputs.append(
            RequiredInput(
                key=_required_text(item["key"], "required input key"),
                reason=_required_text(item["reason"], "required input reason"),
            )
        )
    proposals: list[RemediationPlanProposal] = []
    proposal_fields = {
        "proposal_key",
        "action_type",
        "rationale",
        "evidence_fact_ids",
        "parameters",
    }
    for item in proposals_raw:
        if not isinstance(item, dict) or set(item) != proposal_fields:
            raise RemediationPlanError("proposal fields are invalid", code="invalid_schema")
        parameters = item["parameters"]
        if not isinstance(parameters, dict):
            raise RemediationPlanError(
                "proposal parameters must be an object",
                code="invalid_schema",
            )
        proposals.append(
            RemediationPlanProposal(
                proposal_key=_required_text(item["proposal_key"], "proposal key"),
                action_type=_required_text(item["action_type"], "action type"),
                rationale=_required_text(item["rationale"], "rationale"),
                evidence_fact_ids=_string_tuple(
                    item["evidence_fact_ids"],
                    "evidence_fact_ids",
                ),
                parameters=parameters,
            )
        )
    if len(json.dumps(value, ensure_ascii=False).encode()) > 64 * 1024:
        raise RemediationPlanError("remediation plan is too large", code="invalid_schema")
    return RemediationPlan(
        schema_version=REMEDIATION_PLAN_SCHEMA_VERSION,
        summary=summary,
        fact_ids=fact_ids,
        required_inputs=tuple(required_inputs),
        proposals=tuple(proposals),
        stop_conditions=stop_conditions,
    )


def validate_remediation_plan(
    plan: RemediationPlan,
    context: RemediationPlanningContext,
) -> None:
    facts = {fact.fact_id for fact in context.facts}
    if not plan.fact_ids or set(plan.fact_ids) != facts:
        raise RemediationPlanError(
            "plan must cite every supplied fact and no unknown facts",
            code="invalid_evidence",
        )
    if len(set(plan.fact_ids)) != len(plan.fact_ids):
        raise RemediationPlanError("duplicate fact IDs are not allowed", code="invalid_evidence")
    proposal_keys: set[str] = set()
    for proposal in plan.proposals:
        if proposal.proposal_key in proposal_keys:
            raise RemediationPlanError("proposal keys must be unique", code="invalid_schema")
        proposal_keys.add(proposal.proposal_key)
        if proposal.action_type not in context.allowed_action_types:
            raise RemediationPlanError(
                f"action type is outside policy: {proposal.action_type}",
                code="policy_escape",
            )
        cited = set(proposal.evidence_fact_ids)
        if not cited or not cited.issubset(facts):
            raise RemediationPlanError(
                "proposal cites unknown or no facts",
                code="invalid_evidence",
            )
        _validate_action_parameters(proposal, context)


def _validate_action_parameters(
    proposal: RemediationPlanProposal,
    context: RemediationPlanningContext,
) -> None:
    parameters = proposal.parameters
    forbidden_keys = {"shell", "command", "argv", "token", "api_key", "password"}
    if _nested_keys(parameters) & forbidden_keys:
        raise RemediationPlanError(
            "proposal contains an arbitrary command or secret-bearing field",
            code="policy_escape",
        )
    if proposal.action_type == "contract_patch":
        if set(parameters) != {"patch"} or not isinstance(parameters.get("patch"), dict):
            raise RemediationPlanError(
                "contract_patch requires only patch",
                code="invalid_parameters",
            )
        patch = parameters["patch"]
        if not patch or not set(patch).issubset(context.allowed_patch_fields):
            raise RemediationPlanError(
                "contract patch fields are outside policy",
                code="policy_escape",
            )
        if any(value is None for value in patch.values()):
            raise RemediationPlanError(
                "contract patch values must be concrete",
                code="invalid_parameters",
            )
    elif proposal.action_type in {"path_probe", "runtime_probe"}:
        if set(parameters) != {"probe_kind"} or not isinstance(parameters.get("probe_kind"), str):
            raise RemediationPlanError(
                "probe requires a structured probe_kind",
                code="invalid_parameters",
            )
    elif proposal.action_type == "environment_select":
        if set(parameters) != {"environment_name"}:
            raise RemediationPlanError(
                "environment_select requires a name",
                code="invalid_parameters",
            )
    elif proposal.action_type == "retry_run":
        if parameters not in ({}, {"submit": True}, {"submit": False}):
            raise RemediationPlanError(
                "retry_run parameters are invalid",
                code="invalid_parameters",
            )
    elif proposal.action_type == "dependency_plan" and set(parameters) - {"packages"}:
        raise RemediationPlanError(
            "dependency_plan parameters are invalid",
            code="invalid_parameters",
        )


def _nested_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(key).lower() for key in value} | {
            nested for item in value.values() for nested in _nested_keys(item)
        }
    if isinstance(value, list):
        return {nested for item in value for nested in _nested_keys(item)}
    return set()


def _retryable_plan_error(error: RemediationPlanError) -> bool:
    if error.code in {"invalid_json", "invalid_schema", "invalid_response", "transport_error"}:
        return True
    if not error.code.startswith("http_"):
        return False
    try:
        status = int(error.code.removeprefix("http_"))
    except ValueError:
        return False
    return status in {408, 429} or status >= 500


_AGENTD_REMEDIATION_ERROR_CODES = {
    "provider_rate_limited": "http_429",
    "provider_timeout": "http_408",
    "provider_unavailable": "transport_error",
    "provider_invalid_response": "invalid_response",
    "output_contract_violation": "invalid_schema",
    "aborted": "transport_error",
    "internal_error": "provider_error",
    "transport_error": "transport_error",
    "protocol_error": "invalid_response",
    "invalid_request": "invalid_response",
    "http_error": "transport_error",
    "shutting_down": "transport_error",
}


def _remediation_error_code(error: AgentdClientError) -> str:
    if error.code in {"provider_auth", "unauthorized"}:
        if error.provider_status in {401, 403}:
            return f"http_{error.provider_status}"
        return "http_401"
    return _AGENTD_REMEDIATION_ERROR_CODES.get(error.code, "provider_error")


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 2000:
        raise RemediationPlanError(f"{name} is invalid", code="invalid_schema")
    return value.strip()


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() and len(item.strip()) <= 512 for item in value
    ):
        raise RemediationPlanError(f"{name} must be a string array", code="invalid_schema")
    return tuple(item.strip() for item in value)
