"""Evidence-bound agent explanations for runs."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from pilot107.agent.client import AgentdClient
from pilot107.agent.config import config_from_env as agentd_config_from_env
from pilot107.agent.protocol import AgentdClientError, AgentdTurnResult
from pilot107.agent.providers import AgentdConstrainedProvider
from pilot107.core.code_context import CodeContextBundle, CodeContextService
from pilot107.core.evidence_binding import BoundEvidence, EvidenceBinder, EvidenceBundle
from pilot107.core.run_store import DiagnosisRecord, RunRecord, RunStore, utc_now_iso

# Strict whitelist of Contract dot-paths the agent may suggest patching.
# Mirrors pilot107.core.advice._PATCHABLE_FIELDS exactly (a test in
# tests/core/test_agent_suggest.py asserts the two sets stay equal so that
# the agent can only suggest fields the remediation layer can apply).
# Imported directly would create a circular import (advice imports agent),
# so the set is mirrored here and kept in sync by that test.
_CONTRACT_PATCH_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "project.workdir",
        "entry.command",
        "resources.partition",
        "resources.qos",
        "resources.nodes",
        "resources.ntasks",
        "resources.cpus_per_task",
        "resources.time_limit",
        "resources.memory",
        "resources.gpus_per_node",
        "resources.array",
    }
)

_CONTRACT_PATCH_FORBIDDEN_SEGMENTS: frozenset[str] = frozenset(
    {"__proto__", "prototype", "constructor"}
)

_CONTRACT_PATCH_FALLBACK_EXPLANATION_ZH = "LLM 未配置，请手动编辑 Contract 字段。"


@dataclass(frozen=True)
class AgentFact:
    fact_id: str
    statement: str
    evidence_refs: tuple[str, ...]
    confidence: str
    evidence_object_ids: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "statement": self.statement,
            "evidence_refs": list(self.evidence_refs),
            "evidence_object_ids": list(self.evidence_object_ids),
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class AgentCitation:
    fact_id: str
    evidence_object_ids: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "evidence_object_ids": list(self.evidence_object_ids),
        }


@dataclass(frozen=True)
class AgentExplanation:
    run_id: str
    provider: str
    status: str
    summary: str
    facts: tuple[AgentFact, ...]
    diagnoses: tuple[DiagnosisRecord, ...]
    model: str | None = None
    narrative: str | None = None
    recommendations: tuple[str, ...] = ()
    citations: tuple[AgentCitation, ...] = ()
    warnings: tuple[str, ...] = ()
    evidence_bundle_sha256: str | None = None
    bound_evidence: tuple[BoundEvidence, ...] = field(default=(), repr=False)
    code_context: CodeContextBundle | None = field(default=None, repr=False)
    created_at: str = field(default_factory=utc_now_iso)

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "provider": self.provider,
            "status": self.status,
            "summary": self.summary,
            "facts": [fact.to_payload() for fact in self.facts],
            "diagnoses": [_diagnosis_payload(diagnosis) for diagnosis in self.diagnoses],
            "model": self.model,
            "narrative": self.narrative,
            "recommendations": list(self.recommendations),
            "citations": [citation.to_payload() for citation in self.citations],
            "warnings": list(self.warnings),
            "evidence_bundle_sha256": self.evidence_bundle_sha256,
            "code_context": (None if self.code_context is None else self.code_context.to_payload()),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class LLMExplanation:
    summary: str
    narrative: str
    recommendations: tuple[str, ...]
    model: str
    citations: tuple[AgentCitation, ...] = ()
    warnings: tuple[str, ...] = ()


class AgentLLMProvider(Protocol):
    provider_name: str
    model: str

    def explain(self, explanation: AgentExplanation) -> LLMExplanation:
        """Return a user-facing explanation from an evidence-bound context."""
        ...

    def suggest_contract_patch(
        self,
        *,
        current_contract: dict[str, Any],
        recipe_version_id: str,
        user_intent: str,
    ) -> dict[str, Any]:
        """Return a Contract patch suggestion from the user's intent."""
        ...


class LLMCallObserver(Protocol):
    def observe_llm_call(
        self,
        *,
        provider: str,
        model: str,
        outcome: str,
        duration_seconds: float,
        input_tokens: int,
        output_tokens: int,
    ) -> None: ...


class OpenAICompatibleLLMProvider:
    """Compatibility facade backed by the central pilot-agentd service."""

    provider_name = "local"

    def __init__(
        self,
        *,
        client: AgentdClient,
        observer: LLMCallObserver | None = None,
    ) -> None:
        self.client = client
        self.model = client.config.model_profile_id
        self.observer = observer
        self._provider = AgentdConstrainedProvider(client)

    @classmethod
    def from_env(
        cls,
        prefix: str = "PILOT107_AGENTD_",
        *,
        observer: LLMCallObserver | None = None,
    ) -> OpenAICompatibleLLMProvider:
        return cls(
            client=AgentdClient(agentd_config_from_env(prefix=prefix)),
            observer=observer,
        )

    def explain(self, explanation: AgentExplanation) -> LLMExplanation:
        started = time.monotonic()
        terminal: AgentdTurnResult | None = None
        try:
            terminal = self._provider.invoke("explain", _prompt_payload(explanation))
            parsed = _parse_llm_json(terminal.result)
            result = LLMExplanation(
                summary=parsed["summary"],
                narrative=parsed["narrative"],
                recommendations=tuple(parsed["recommendations"]),
                model=terminal.model,
                citations=tuple(
                    AgentCitation(
                        fact_id=item["fact_id"],
                        evidence_object_ids=tuple(item["evidence_object_ids"]),
                    )
                    for item in parsed["citations"]
                ),
                warnings=tuple(parsed["warnings"]),
            )
            _validate_llm_citations(result, explanation.facts)
        except AgentdClientError as exc:
            mapped = _agent_provider_error(exc)
            self._observe_failure(mapped, started=started, terminal=terminal)
            raise mapped from None
        except AgentProviderError as exc:
            self._observe_failure(exc, started=started, terminal=terminal)
            raise
        self._observe_call(
            outcome="success",
            started=started,
            model=terminal.model,
            input_tokens=_non_negative_token_count(terminal.input_tokens),
            output_tokens=_non_negative_token_count(terminal.output_tokens),
        )
        return result

    def suggest_contract_patch(
        self,
        *,
        current_contract: dict[str, Any],
        recipe_version_id: str,
        user_intent: str,
    ) -> dict[str, Any]:
        """Return a Contract patch suggestion from the user's intent."""
        started = time.monotonic()
        terminal: AgentdTurnResult | None = None
        try:
            terminal = self._provider.invoke(
                "contract_patch",
                _contract_patch_prompt_payload(
                    current_contract=current_contract,
                    recipe_version_id=recipe_version_id,
                    user_intent=user_intent,
                ),
            )
            parsed = _parse_contract_patch_json(terminal.result)
        except AgentdClientError as exc:
            mapped = _agent_provider_error(exc)
            self._observe_failure(mapped, started=started, terminal=terminal)
            raise mapped from None
        except AgentProviderError as exc:
            self._observe_failure(exc, started=started, terminal=terminal)
            raise
        self._observe_call(
            outcome="success",
            started=started,
            model=terminal.model,
            input_tokens=_non_negative_token_count(terminal.input_tokens),
            output_tokens=_non_negative_token_count(terminal.output_tokens),
        )
        return {
            "suggested_patch": parsed["suggested_patch"],
            "explanation_zh": parsed["explanation_zh"],
            "needs_user_confirmation": True,
        }

    def _observe_failure(
        self,
        error: AgentProviderError,
        *,
        started: float,
        terminal: AgentdTurnResult | None,
    ) -> None:
        self._observe_call(
            outcome=error.code,
            started=started,
            model=self.model if terminal is None else terminal.model,
            input_tokens=(
                0 if terminal is None else _non_negative_token_count(terminal.input_tokens)
            ),
            output_tokens=(
                0 if terminal is None else _non_negative_token_count(terminal.output_tokens)
            ),
        )

    def _observe_call(
        self,
        *,
        outcome: str,
        started: float,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        if self.observer is None:
            return
        try:
            self.observer.observe_llm_call(
                provider=self.provider_name,
                model=model,
                outcome=outcome,
                duration_seconds=time.monotonic() - started,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        except Exception:
            return


class AgentExplainService:
    """Produce deterministic explanations without calling an LLM."""

    def __init__(
        self,
        *,
        store: RunStore,
        llm_provider: AgentLLMProvider | None = None,
        evidence_binder: EvidenceBinder | None = None,
        code_context_service: CodeContextService | None = None,
    ) -> None:
        self.store = store
        self.llm_provider = llm_provider
        self.evidence_binder = evidence_binder
        self.code_context_service = code_context_service
        if llm_provider is not None and evidence_binder is None:
            raise ValueError("evidence_binder is required when llm_provider is configured")

    def explain(self, run_id: str, *, provider: str = "none") -> AgentExplanation:
        normalized_provider = provider.strip().lower() if provider else "none"
        if normalized_provider not in {"none", "local", "campus"}:
            raise AgentProviderError(f"unsupported agent provider: {provider}")
        run = self.store.get_run(run_id)
        diagnoses = tuple(self.store.list_diagnoses(run_id))
        evidence_bundle = self._bind_evidence(run_id, diagnoses)
        code_context = self._capture_code_context(run, evidence_bundle=evidence_bundle)
        explanation = explain_without_llm(
            run,
            diagnoses,
            evidence_bundle=evidence_bundle,
            code_context=code_context,
        )
        if code_context is not None:
            explanation = _with_code_context_facts(explanation, code_context)
        if normalized_provider == "none":
            return explanation
        if self.llm_provider is None:
            raise AgentProviderError("local llm provider is not configured")
        if not explanation.facts:
            return AgentExplanation(
                run_id=explanation.run_id,
                provider=self.llm_provider.provider_name,
                status=explanation.status,
                summary=explanation.summary,
                facts=explanation.facts,
                diagnoses=explanation.diagnoses,
                model=self.llm_provider.model,
                warnings=(
                    *explanation.warnings,
                    "local_llm_skipped:no_evidence_bound_facts",
                ),
                evidence_bundle_sha256=explanation.evidence_bundle_sha256,
                bound_evidence=explanation.bound_evidence,
                code_context=explanation.code_context,
            )
        try:
            llm = self.llm_provider.explain(explanation)
            _validate_llm_citations(llm, explanation.facts)
        except AgentProviderError as exc:
            return AgentExplanation(
                run_id=explanation.run_id,
                provider=self.llm_provider.provider_name,
                status=explanation.status,
                summary=explanation.summary,
                facts=explanation.facts,
                diagnoses=explanation.diagnoses,
                model=self.llm_provider.model,
                warnings=(*explanation.warnings, f"local_llm_fallback:{exc.code}"),
                evidence_bundle_sha256=explanation.evidence_bundle_sha256,
                bound_evidence=explanation.bound_evidence,
                code_context=explanation.code_context,
            )
        return AgentExplanation(
            run_id=explanation.run_id,
            provider=self.llm_provider.provider_name,
            status="explained",
            summary=llm.summary,
            facts=explanation.facts,
            diagnoses=explanation.diagnoses,
            model=llm.model,
            narrative=llm.narrative,
            recommendations=llm.recommendations,
            citations=llm.citations,
            warnings=(*explanation.warnings, *llm.warnings),
            evidence_bundle_sha256=explanation.evidence_bundle_sha256,
            bound_evidence=explanation.bound_evidence,
            code_context=explanation.code_context,
        )

    def _bind_evidence(
        self,
        run_id: str,
        diagnoses: tuple[DiagnosisRecord, ...],
    ) -> EvidenceBundle | None:
        if self.evidence_binder is None:
            return None
        refs = tuple(
            str(ref)
            for diagnosis in diagnoses
            for ref in diagnosis.evidence_refs
            if str(ref).strip()
        )
        return self.evidence_binder.bind(run_id, refs)

    def _capture_code_context(
        self,
        run: RunRecord,
        *,
        evidence_bundle: EvidenceBundle | None,
    ) -> CodeContextBundle | None:
        if self.code_context_service is None or evidence_bundle is None:
            return None
        return self.code_context_service.capture(
            run,
            evidence_texts=tuple(item.snippet for item in evidence_bundle.objects),
        )


class AgentProviderError(ValueError):
    """Raised when a requested agent provider is not enabled."""

    def __init__(self, message: str, *, code: str = "provider_error") -> None:
        super().__init__(message)
        self.code = code


_AGENTD_PROVIDER_ERROR_CODES = {
    "provider_rate_limited": "http_429",
    "provider_timeout": "http_408",
    "provider_unavailable": "transport_error",
    "provider_invalid_response": "invalid_response",
    "output_contract_violation": "invalid_schema_fields",
    "aborted": "transport_error",
    "internal_error": "provider_error",
    "transport_error": "transport_error",
    "protocol_error": "invalid_response",
    "invalid_request": "invalid_response",
    "http_error": "transport_error",
    "shutting_down": "transport_error",
}


def _agent_provider_error(error: AgentdClientError) -> AgentProviderError:
    if error.code in {"provider_auth", "unauthorized"}:
        code = (
            f"http_{error.provider_status}" if error.provider_status in {401, 403} else "http_401"
        )
    else:
        code = _AGENTD_PROVIDER_ERROR_CODES.get(error.code, "provider_error")
    return AgentProviderError("pilot-agentd provider call failed", code=code)


def explain_without_llm(
    run: RunRecord,
    diagnoses: tuple[DiagnosisRecord, ...] | list[DiagnosisRecord],
    *,
    evidence_bundle: EvidenceBundle | None = None,
    code_context: CodeContextBundle | None = None,
) -> AgentExplanation:
    """Build a deterministic, evidence-bound explanation from stored diagnoses."""

    diagnosis_items = tuple(diagnoses)
    facts: list[AgentFact] = []
    warnings: list[str] = []
    bound_by_ref = {} if evidence_bundle is None else evidence_bundle.by_ref()
    if evidence_bundle is not None:
        warnings.extend(evidence_bundle.warnings)
    for diagnosis in diagnosis_items:
        refs = tuple(str(ref) for ref in diagnosis.evidence_refs if str(ref).strip())
        if evidence_bundle is not None:
            refs = tuple(ref for ref in refs if ref in bound_by_ref)
        if not refs:
            warnings.append(f"diagnosis_without_evidence_refs:{diagnosis.rule_id}")
            continue
        facts.append(
            AgentFact(
                fact_id=f"fact_{diagnosis.diagnosis_id}",
                statement=_diagnosis_fact_statement(diagnosis),
                evidence_refs=refs,
                confidence=str(diagnosis.confidence),
                evidence_object_ids=(
                    ()
                    if evidence_bundle is None
                    else tuple(dict.fromkeys(bound_by_ref[ref].object_id for ref in refs))
                ),
            )
        )

    if facts:
        status = "explained"
        summary = _summary_from_diagnoses(run, diagnosis_items)
    elif diagnosis_items:
        status = "insufficient_evidence"
        summary = (
            "Stored diagnoses exist, but none have evidence references suitable for explanation."
        )
    else:
        status = "no_diagnosis"
        summary = "No stored diagnoses are available for this run yet."

    return AgentExplanation(
        run_id=run.run_id,
        provider="none",
        status=status,
        summary=summary,
        facts=tuple(facts),
        diagnoses=diagnosis_items,
        warnings=tuple(warnings),
        evidence_bundle_sha256=None if evidence_bundle is None else evidence_bundle.sha256,
        bound_evidence=() if evidence_bundle is None else evidence_bundle.objects,
        code_context=code_context,
    )


def _with_code_context_facts(
    explanation: AgentExplanation,
    code_context: CodeContextBundle,
) -> AgentExplanation:
    """Expose selected code windows as separately citable facts.

    A model receives the chunk text in ``code_context`` and may only cite the
    stable chunk id attached to the corresponding fact.  This keeps code
    claims tied to a snapshot instead of treating arbitrary repository text as
    an uncited instruction.
    """

    facts = list(explanation.facts)
    for chunk in code_context.chunks:
        facts.append(
            AgentFact(
                fact_id=f"fact_{chunk.chunk_id}",
                statement=(
                    f"Code snapshot {code_context.snapshot_id} contains the error-site window "
                    f"{chunk.path}:{chunk.start_line}-{chunk.end_line}."
                ),
                evidence_refs=(chunk.source_ref,),
                confidence="high",
                evidence_object_ids=(chunk.chunk_id,),
            )
        )
    warnings = list(explanation.warnings)
    warnings.extend(f"code_context:{warning}" for warning in code_context.warnings)
    return replace(
        explanation,
        facts=tuple(facts),
        warnings=tuple(dict.fromkeys(warnings)),
        code_context=code_context,
    )


def _summary_from_diagnoses(run: RunRecord, diagnoses: tuple[DiagnosisRecord, ...]) -> str:
    primary = _primary_diagnosis(diagnoses)
    retry = "可以修正后重试。" if primary.retryable else "建议先人工确认后再重试。"
    exit_text = f"退出码 {run.exit_code}" if run.exit_code else "无退出码"
    return (
        f"Run {run.run_id} 结束状态为 {run.state.value}，{exit_text}。"
        f"主要诊断：{primary.summary} {retry}"
    )


def _primary_diagnosis(diagnoses: tuple[DiagnosisRecord, ...]) -> DiagnosisRecord:
    severity_rank = {"error": 0, "warn": 1, "warning": 1, "info": 2}
    return sorted(
        diagnoses,
        key=lambda diagnosis: (
            severity_rank.get(str(diagnosis.severity).lower(), 3),
            diagnosis.rule_id,
        ),
    )[0]


def _diagnosis_payload(diagnosis: DiagnosisRecord) -> dict[str, Any]:
    return {
        "diagnosis_id": diagnosis.diagnosis_id,
        "rule_id": diagnosis.rule_id,
        "severity": diagnosis.severity,
        "summary": diagnosis.summary,
        "evidence_refs": diagnosis.evidence_refs,
        "suggested_patch": diagnosis.suggested_patch,
        "retryable": diagnosis.retryable,
        "confidence": diagnosis.confidence,
        "category": diagnosis.category,
        "stage": diagnosis.stage,
        "fix_guide": diagnosis.fix_guide,
    }


def _diagnosis_fact_statement(diagnosis: DiagnosisRecord) -> str:
    parts = [f"{diagnosis.rule_id}: {diagnosis.summary}"]
    fix = diagnosis.fix_guide.get("fix")
    prevention = diagnosis.fix_guide.get("prevention")
    automation = diagnosis.fix_guide.get("automation")
    if fix:
        parts.append(f"修复: {fix}")
    if prevention:
        parts.append(f"预防: {prevention}")
    if automation:
        parts.append(f"自动化: {automation}")
    return " ".join(parts)


def _prompt_payload(explanation: AgentExplanation) -> dict[str, Any]:
    bound_diagnosis_ids = {fact.fact_id.removeprefix("fact_") for fact in explanation.facts}
    return {
        "run_id": explanation.run_id,
        "status": explanation.status,
        "deterministic_summary": explanation.summary,
        "facts": [fact.to_payload() for fact in explanation.facts],
        "bound_evidence": [item.to_payload() for item in explanation.bound_evidence],
        "code_context": (
            None if explanation.code_context is None else explanation.code_context.to_payload()
        ),
        "diagnoses": [
            _diagnosis_payload(diagnosis)
            for diagnosis in explanation.diagnoses
            if diagnosis.diagnosis_id in bound_diagnosis_ids
        ],
        "required_output": {
            "summary": "one sentence, grounded in facts",
            "narrative": "short Chinese explanation for the user",
            "recommendations": "array of concrete next actions",
            "warnings": "array of uncertainty notes",
            "citations": ("one item per fact_id; evidence_object_ids must come from that fact"),
        },
    }


def _parse_llm_json(content: object) -> dict[str, Any]:
    if not isinstance(content, dict):
        raise AgentProviderError(
            "local llm output must be an object",
            code="invalid_schema_object",
        )
    decoded = content
    expected_keys = {"summary", "narrative", "recommendations", "warnings", "citations"}
    if set(decoded) != expected_keys:
        raise AgentProviderError(
            "local llm output has missing or additional fields",
            code="invalid_schema_fields",
        )
    summary = decoded["summary"]
    narrative = decoded["narrative"]
    recommendations = decoded["recommendations"]
    warnings = decoded["warnings"]
    citations = decoded["citations"]
    if not isinstance(summary, str) or not summary.strip():
        raise AgentProviderError(
            "local llm summary is invalid",
            code="invalid_schema_summary",
        )
    if not isinstance(narrative, str) or not narrative.strip():
        raise AgentProviderError(
            "local llm narrative is invalid",
            code="invalid_schema_narrative",
        )
    if not _is_string_list(recommendations) or not _is_string_list(warnings):
        raise AgentProviderError(
            "local llm list fields are invalid",
            code="invalid_schema_lists",
        )
    if not isinstance(citations, list):
        raise AgentProviderError(
            "local llm citations are invalid",
            code="invalid_schema_citations",
        )
    normalized_citations: list[dict[str, Any]] = []
    for citation in citations:
        if not isinstance(citation, dict) or set(citation) != {
            "fact_id",
            "evidence_object_ids",
        }:
            raise AgentProviderError(
                "local llm citation is invalid",
                code="invalid_schema_citation_item",
            )
        fact_id = citation["fact_id"]
        object_ids = citation["evidence_object_ids"]
        if not isinstance(fact_id, str) or not fact_id.strip() or not _is_string_list(object_ids):
            raise AgentProviderError(
                "local llm citation is invalid",
                code="invalid_schema_citation_item",
            )
        normalized_citations.append(
            {
                "fact_id": fact_id.strip(),
                "evidence_object_ids": [item.strip() for item in object_ids],
            }
        )
    return {
        "summary": summary.strip(),
        "narrative": narrative.strip(),
        "recommendations": [item.strip() for item in recommendations],
        "warnings": [item.strip() for item in warnings],
        "citations": normalized_citations,
    }


def _is_string_list(value: object) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, str) and bool(item.strip()) for item in value
    )


def _non_negative_token_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _validate_llm_citations(
    explanation: LLMExplanation,
    facts: tuple[AgentFact, ...],
) -> None:
    facts_by_id = {fact.fact_id: fact for fact in facts}
    citations_by_fact: dict[str, AgentCitation] = {}
    for citation in explanation.citations:
        fact = facts_by_id.get(citation.fact_id)
        if fact is None or citation.fact_id in citations_by_fact:
            raise AgentProviderError("local llm cited an unknown fact", code="invalid_citation")
        if not citation.evidence_object_ids:
            raise AgentProviderError("local llm citation is empty", code="invalid_citation")
        if not set(citation.evidence_object_ids).issubset(set(fact.evidence_object_ids)):
            raise AgentProviderError(
                "local llm cited evidence outside the fact",
                code="invalid_citation",
            )
        citations_by_fact[citation.fact_id] = citation
    if set(citations_by_fact) != set(facts_by_id):
        raise AgentProviderError(
            "local llm did not cite every fact",
            code="incomplete_citations",
        )


def suggest_contract_patch_without_llm() -> dict[str, Any]:
    """Deterministic fallback when no LLM provider is configured."""

    return {
        "suggested_patch": {},
        "explanation_zh": _CONTRACT_PATCH_FALLBACK_EXPLANATION_ZH,
        "needs_user_confirmation": False,
    }


def _contract_patch_prompt_payload(
    *,
    current_contract: dict[str, Any],
    recipe_version_id: str,
    user_intent: str,
) -> dict[str, Any]:
    return {
        "recipe_version_id": str(recipe_version_id),
        "user_intent": str(user_intent),
        "current_contract": current_contract,
        "required_output": {
            "suggested_patch": (
                "object mapping Contract dot-path (e.g. entry.command, "
                "resources.cpus_per_task, resources.memory) to new values; "
                "empty object if the intent is unclear or unsafe"
            ),
            "explanation_zh": "简短的中文说明，解释这次建议的改动",
        },
    }


def _parse_contract_patch_json(content: object) -> dict[str, Any]:
    if not isinstance(content, dict):
        raise AgentProviderError(
            "local llm output must be an object",
            code="invalid_schema_object",
        )
    decoded = content
    expected_keys = {"suggested_patch", "explanation_zh"}
    if set(decoded) != expected_keys:
        raise AgentProviderError(
            "local llm output has missing or additional fields",
            code="invalid_schema_fields",
        )
    suggested_patch = decoded["suggested_patch"]
    explanation_zh = decoded["explanation_zh"]
    if not isinstance(suggested_patch, dict):
        raise AgentProviderError(
            "local llm suggested_patch must be an object",
            code="invalid_schema_patch",
        )
    if not isinstance(explanation_zh, str) or not explanation_zh.strip():
        raise AgentProviderError(
            "local llm explanation_zh is invalid",
            code="invalid_schema_explanation",
        )
    normalized_patch: dict[str, Any] = {}
    for key, value in suggested_patch.items():
        if not isinstance(key, str) or not key.strip():
            raise AgentProviderError(
                "local llm patch key is invalid",
                code="invalid_schema_patch_key",
            )
        dot_path = key.strip()
        segments = dot_path.split(".")
        if dot_path not in _CONTRACT_PATCH_ALLOWED_FIELDS or any(
            segment in _CONTRACT_PATCH_FORBIDDEN_SEGMENTS for segment in segments
        ):
            raise AgentProviderError(
                f"local llm patch targets a non-whitelisted field: {dot_path}",
                code="invalid_schema_patch_field",
            )
        normalized_patch[dot_path] = value
    return {
        "suggested_patch": normalized_patch,
        "explanation_zh": explanation_zh.strip(),
    }
