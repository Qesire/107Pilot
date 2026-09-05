import os
import tempfile
import traceback
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pilot107.adapters.slurm import JobSnapshot, SubmissionStrategy, SubmitReceipt
from pilot107.agent.config import AgentdClientConfig
from pilot107.agent.protocol import AgentdClientError, AgentdTurnResult
from pilot107.core.agent import (
    AgentCitation,
    AgentExplainService,
    AgentExplanation,
    AgentFact,
    AgentProviderError,
    LLMExplanation,
    OpenAICompatibleLLMProvider,
    explain_without_llm,
)
from pilot107.core.evidence_binding import EvidenceBinder
from pilot107.core.run_store import RunStore
from pilot107.core.states import RunState
from pilot107.worker.evidence import EvidenceStore


class AgentExplainTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = RunStore(root / "pilot107.db")
        self.evidence_store = EvidenceStore(root / "evidence")
        self.evidence_binder = EvidenceBinder(store=self.store, evidence_root=root / "evidence")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_explain_without_llm_uses_only_evidence_bound_diagnoses(self) -> None:
        run = self._failed_run()
        records = self.store.replace_diagnoses(
            run.run_id,
            [
                {
                    "diagnosis_id": "diag_with_evidence",
                    "rule_id": "RUNTIME.PYTHON_PACKAGE_MISSING",
                    "severity": "error",
                    "summary": "Python 运行环境缺少作业需要的包。",
                    "evidence_refs": [f"evidence://runs/{run.run_id}/logs/stderr.tail.txt"],
                    "suggested_patch": {"runtime.conda_env": None},
                    "retryable": True,
                    "confidence": "high",
                    "fix_guide": {
                        "fix": "切换到包含 torch 的 conda 环境。",
                        "prevention": "提交前执行 import probe。",
                        "automation": "preflight 自动校验关键 import。",
                    },
                },
                {
                    "diagnosis_id": "diag_without_evidence",
                    "rule_id": "RUNTIME.NONZERO_EXIT",
                    "severity": "error",
                    "summary": "作业以非零退出码结束。",
                    "evidence_refs": [],
                    "suggested_patch": {},
                    "retryable": True,
                    "confidence": "medium",
                },
            ],
        )

        explanation = explain_without_llm(self.store.get_run(run.run_id), records)

        self.assertEqual(explanation.status, "explained")
        self.assertEqual(len(explanation.facts), 1)
        self.assertEqual(
            explanation.facts[0].evidence_refs,
            (f"evidence://runs/{run.run_id}/logs/stderr.tail.txt",),
        )
        self.assertIn("切换到包含 torch 的 conda 环境", explanation.facts[0].statement)
        self.assertIn("diagnosis_without_evidence_refs:RUNTIME.NONZERO_EXIT", explanation.warnings)

    def test_service_rejects_non_none_provider(self) -> None:
        run = self._failed_run()

        with self.assertRaises(AgentProviderError):
            AgentExplainService(store=self.store).explain(run.run_id, provider="campus")

    def test_service_uses_campus_provider_without_changing_facts(self) -> None:
        run = self._failed_run()
        evidence_ref = self._register_stderr(run.run_id)
        self.store.replace_diagnoses(
            run.run_id,
            [
                {
                    "diagnosis_id": "diag_with_evidence",
                    "rule_id": "RUNTIME.PYTHON_PACKAGE_MISSING",
                    "severity": "error",
                    "summary": "Python 运行环境缺少作业需要的包。",
                    "evidence_refs": [evidence_ref],
                    "suggested_patch": {"runtime.conda_env": None},
                    "retryable": True,
                    "confidence": "high",
                },
            ],
        )

        explanation = AgentExplainService(
            store=self.store,
            llm_provider=FakeCampusProvider(),
            evidence_binder=self.evidence_binder,
        ).explain(run.run_id, provider="campus")

        self.assertEqual(explanation.provider, "local")
        self.assertEqual(explanation.model, "ustc-deepseek/deepseek-v4-pro")
        self.assertEqual(explanation.narrative, "检测到 Python 包缺失。")
        self.assertEqual(explanation.recommendations, ("安装缺失包",))
        self.assertEqual(explanation.facts[0].evidence_refs, (evidence_ref,))
        self.assertEqual(explanation.facts[0].evidence_object_ids, ("ev_agent_stderr",))
        self.assertEqual(explanation.citations[0].fact_id, explanation.facts[0].fact_id)

    def test_agentd_provider_preserves_domain_result_and_terminal_metrics(self) -> None:
        fact = AgentFact(
            fact_id="fact_1",
            statement="The stderr reports a missing package.",
            evidence_refs=("evidence://runs/run_1/logs/stderr.tail.txt",),
            confidence="high",
            evidence_object_ids=("ev_1",),
        )
        explanation = AgentExplanation(
            run_id="run_1",
            provider="none",
            status="explained",
            summary="Package missing.",
            facts=(fact,),
            diagnoses=(),
        )
        observer = CapturingLLMObserver()
        client = FakeAgentdClient(
            result={
                "summary": "Package missing.",
                "narrative": "The package import failed.",
                "recommendations": ["Use the validated environment."],
                "warnings": [],
                "citations": [
                    {
                        "fact_id": "fact_1",
                        "evidence_object_ids": ["ev_1"],
                    }
                ],
            },
            model="campus-model",
            input_tokens=120,
            output_tokens=35,
        )
        provider = OpenAICompatibleLLMProvider(
            client=client,
            observer=observer,
        )

        result = provider.explain(explanation)

        self.assertEqual(result.citations[0].evidence_object_ids, ("ev_1",))
        self.assertEqual(result.model, "campus-model")
        self.assertEqual(client.calls[0]["task_kind"], "explain")
        input_payload = client.calls[0]["input_payload"]
        self.assertEqual(input_payload["run_id"], "run_1")
        self.assertEqual(input_payload["facts"][0]["fact_id"], "fact_1")
        self.assertEqual(len(observer.calls), 1)
        self.assertEqual(observer.calls[0]["outcome"], "success")
        self.assertEqual(observer.calls[0]["model"], "campus-model")
        self.assertEqual(observer.calls[0]["input_tokens"], 120)
        self.assertEqual(observer.calls[0]["output_tokens"], 35)

    def test_service_falls_back_when_llm_citation_is_invalid(self) -> None:
        run = self._failed_run()
        evidence_ref = self._register_stderr(run.run_id)
        self.store.replace_diagnoses(
            run.run_id,
            [
                {
                    "diagnosis_id": "diag_with_evidence",
                    "rule_id": "RUNTIME.PYTHON_PACKAGE_MISSING",
                    "severity": "error",
                    "summary": "Python package missing.",
                    "evidence_refs": [evidence_ref],
                    "retryable": True,
                    "confidence": "high",
                }
            ],
        )

        explanation = AgentExplainService(
            store=self.store,
            llm_provider=InvalidCitationProvider(),
            evidence_binder=self.evidence_binder,
        ).explain(run.run_id, provider="local")

        self.assertIsNone(explanation.narrative)
        self.assertIn("local_llm_fallback:invalid_citation", explanation.warnings)
        self.assertEqual(explanation.provider, "local")

    def test_agentd_provider_does_not_retry_python_citation_validation(self) -> None:
        fact = AgentFact(
            fact_id="fact_retry",
            statement="The stderr reports a missing package.",
            evidence_refs=("evidence://runs/run_retry/logs/stderr.tail.txt",),
            confidence="high",
            evidence_object_ids=("ev_retry",),
        )
        explanation = AgentExplanation(
            run_id="run_retry",
            provider="none",
            status="explained",
            summary="Package missing.",
            facts=(fact,),
            diagnoses=(),
        )
        client = FakeAgentdClient(
            result={
                "summary": "Package missing.",
                "narrative": "The evidence shows a failed import.",
                "recommendations": ["Use a validated environment."],
                "warnings": [],
                "citations": [
                    {
                        "fact_id": "fact_retry",
                        "evidence_object_ids": ["ev_outside_fact"],
                    }
                ],
            }
        )
        provider = OpenAICompatibleLLMProvider(client=client)

        with self.assertRaises(AgentProviderError) as raised:
            provider.explain(explanation)

        self.assertEqual(raised.exception.code, "invalid_citation")
        self.assertEqual(len(client.calls), 1)

    def test_agentd_failure_maps_once_without_leaking_details(self) -> None:
        client = FakeAgentdClient(
            error=AgentdClientError(
                "upstream leaked api-key=secret",
                code="provider_auth",
                retryable=False,
                provider_status=401,
            )
        )
        observer = CapturingLLMObserver()
        provider = OpenAICompatibleLLMProvider(client=client, observer=observer)
        explanation = AgentExplanation(
            run_id="run_auth",
            provider="none",
            status="explained",
            summary="Auth test.",
            facts=(),
            diagnoses=(),
        )

        with self.assertRaises(AgentProviderError) as raised:
            provider.explain(explanation)

        rendered = "".join(
            traceback.format_exception(
                type(raised.exception),
                raised.exception,
                raised.exception.__traceback__,
            )
        )
        self.assertEqual(raised.exception.code, "http_401")
        self.assertNotIn("api-key=secret", rendered)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(observer.calls[0]["outcome"], "http_401")

    def test_agentd_error_codes_map_to_existing_provider_codes(self) -> None:
        expected_codes = {
            "provider_rate_limited": "http_429",
            "provider_timeout": "http_408",
            "provider_unavailable": "transport_error",
            "provider_invalid_response": "invalid_response",
            "output_contract_violation": "invalid_schema_fields",
            "aborted": "transport_error",
            "internal_error": "provider_error",
            "transport_error": "transport_error",
            "protocol_error": "invalid_response",
        }
        explanation = AgentExplanation(
            run_id="run_error_map",
            provider="none",
            status="explained",
            summary="Error map test.",
            facts=(),
            diagnoses=(),
        )

        for agentd_code, provider_code in expected_codes.items():
            with self.subTest(agentd_code=agentd_code):
                client = FakeAgentdClient(
                    error=AgentdClientError(
                        "pilot-agentd failed",
                        code=agentd_code,
                    )
                )
                provider = OpenAICompatibleLLMProvider(client=client)
                with self.assertRaises(AgentProviderError) as raised:
                    provider.explain(explanation)
                self.assertEqual(raised.exception.code, provider_code)
                self.assertEqual(len(client.calls), 1)

    def test_from_env_uses_only_agentd_configuration(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PILOT107_AGENTD_URL": "http://pilot-agentd:8091",
                "PILOT107_AGENTD_TOKEN": "internal-secret",
                "PILOT107_AGENTD_MODEL_PROFILE": "campus-default",
                "PILOT107_LLM_BASE_URL": "ftp://must-not-be-read",
                "PILOT107_LLM_API_KEY": "must-not-be-read",
                "PILOT107_LLM_MODEL": "must-not-be-read",
            },
            clear=True,
        ):
            provider = OpenAICompatibleLLMProvider.from_env()

        self.assertEqual(provider.client.config.base_url, "http://pilot-agentd:8091")
        self.assertEqual(provider.model, "campus-default")

    def _register_stderr(self, run_id: str) -> str:
        artifact = self.evidence_store.write_text(
            run_id=run_id,
            logical_path="logs/stderr.tail.txt",
            content="ModuleNotFoundError: No module named 'torch'\n",
            content_type="text/plain",
        )
        evidence_ref = f"evidence://runs/{run_id}/{artifact.logical_path}"
        self.store.upsert_evidence_objects(
            run_id,
            [
                {
                    "object_id": "ev_agent_stderr",
                    "category": "logs",
                    "logical_path": artifact.logical_path,
                    "store_path": str(artifact.path),
                    "source_uri": evidence_ref,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "mime_type": artifact.content_type,
                    "collection_status": "collected",
                    "mutable_during_run": False,
                }
            ],
        )
        return evidence_ref

    def _failed_run(self):
        run = self.store.create_run(
            run_id="run_agent_failed",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\npython train.py\n",
        )
        self.store.apply_submit_receipt(
            run.run_id,
            SubmitReceipt(
                job_id="2001",
                run_state=RunState.SUBMITTED,
                strategy=SubmissionStrategy.COMMAND,
            ),
        )
        return self.store.apply_snapshot(
            run.run_id,
            JobSnapshot(
                job_id="2001",
                owner="alice",
                run_state=RunState.FAILED,
                raw_state_flags=["FAILED"],
                exit_code="1:0",
            ),
        )


class FakeCampusProvider:
    provider_name = "local"
    model = "ustc-deepseek/deepseek-v4-pro"

    def explain(self, explanation):
        fact = explanation.facts[0]
        return LLMExplanation(
            summary=explanation.summary,
            narrative="检测到 Python 包缺失。",
            recommendations=("安装缺失包",),
            model=self.model,
            citations=(
                AgentCitation(
                    fact_id=fact.fact_id,
                    evidence_object_ids=fact.evidence_object_ids,
                ),
            ),
        )


class InvalidCitationProvider(FakeCampusProvider):
    def explain(self, explanation):
        result = super().explain(explanation)
        return LLMExplanation(
            summary=result.summary,
            narrative=result.narrative,
            recommendations=result.recommendations,
            model=result.model,
            citations=(
                AgentCitation(
                    fact_id=explanation.facts[0].fact_id,
                    evidence_object_ids=("ev_not_registered",),
                ),
            ),
        )


class FakeAgentdClient:
    def __init__(
        self,
        *,
        result: dict[str, Any] | None = None,
        error: AgentdClientError | None = None,
        model: str = "campus-model",
        input_tokens: int | None = 12,
        output_tokens: int | None = 8,
    ) -> None:
        self.config = AgentdClientConfig(
            base_url="http://pilot-agentd:8091",
            token="internal-secret",
            model_profile_id="campus-default",
        )
        self.result = result or {}
        self.error = error
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.calls: list[dict[str, Any]] = []

    def run_turn(self, **kwargs: Any) -> AgentdTurnResult:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return AgentdTurnResult(
            result=self.result,
            provider="campus-openai-compatible",
            model=self.model,
            model_profile_id="campus-default",
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=None,
            cache_write_tokens=None,
            provider_calls=1,
            checkpoint_digest="c" * 64,
            duration_ms=42,
            checkpoint=None,
        )


class CapturingLLMObserver:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def observe_llm_call(self, **values: object) -> None:
        self.calls.append(values)


if __name__ == "__main__":
    unittest.main()
