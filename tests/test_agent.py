import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from pilot107.adapters.slurm import JobSnapshot, SubmissionStrategy, SubmitReceipt
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

    def test_openai_compatible_provider_uses_json_schema_without_api_key(self) -> None:
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
        provider = OpenAICompatibleLLMProvider(
            base_url="http://llm.internal/v1",
            api_key=None,
            model="local-model",
            structured_output_mode="json_schema",
        )
        response_body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
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
                                }
                            )
                        }
                    }
                ]
            }
        ).encode()

        with patch(
            "pilot107.core.agent.urllib.request.urlopen",
            return_value=FakeHttpResponse(response_body),
        ) as urlopen:
            result = provider.explain(explanation)

        request = urlopen.call_args.args[0]
        request_payload = json.loads(request.data)
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(request_payload["response_format"]["type"], "json_schema")
        self.assertTrue(request_payload["response_format"]["json_schema"]["strict"])
        self.assertEqual(result.citations[0].evidence_object_ids, ("ev_1",))

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

    def test_openai_provider_retries_strict_format_and_citation_validation(self) -> None:
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
        provider = OpenAICompatibleLLMProvider(
            base_url="http://llm.internal/v1",
            api_key="test-key",
            model="local-model",
            max_attempts=2,
        )
        invalid = _chat_response(json.dumps({"summary": "missing fields"}))
        valid = _chat_response(
            json.dumps(
                {
                    "summary": "Package missing.",
                    "narrative": "The evidence shows a failed import.",
                    "recommendations": ["Use a validated environment."],
                    "warnings": [],
                    "citations": [
                        {
                            "fact_id": "fact_retry",
                            "evidence_object_ids": ["ev_retry"],
                        }
                    ],
                }
            )
        )

        with patch(
            "pilot107.core.agent.urllib.request.urlopen",
            side_effect=[FakeHttpResponse(invalid), FakeHttpResponse(valid)],
        ) as urlopen:
            result = provider.explain(explanation)

        self.assertEqual(urlopen.call_count, 2)
        retry_payload = json.loads(urlopen.call_args_list[1].args[0].data)
        self.assertIn("format repair attempt", retry_payload["messages"][0]["content"])
        self.assertEqual(result.citations[0].evidence_object_ids, ("ev_retry",))

    def test_openai_provider_wraps_read_timeout_as_transport_error(self) -> None:
        provider = OpenAICompatibleLLMProvider(
            base_url="http://llm.internal/v1",
            api_key=None,
            model="local-model",
            max_attempts=1,
        )
        explanation = AgentExplanation(
            run_id="run_timeout",
            provider="none",
            status="explained",
            summary="Timeout test.",
            facts=(),
            diagnoses=(),
        )

        with (
            patch(
                "pilot107.core.agent.urllib.request.urlopen",
                side_effect=TimeoutError("read timed out"),
            ),
            self.assertRaises(AgentProviderError) as raised,
        ):
            provider.explain(explanation)

        self.assertEqual(raised.exception.code, "transport_error")

    def test_openai_provider_does_not_retry_nonretryable_http_error(self) -> None:
        provider = OpenAICompatibleLLMProvider(
            base_url="http://llm.internal/v1",
            api_key=None,
            model="missing-model",
            max_attempts=2,
        )
        explanation = AgentExplanation(
            run_id="run_http_error",
            provider="none",
            status="explained",
            summary="HTTP error test.",
            facts=(),
            diagnoses=(),
        )
        error = urllib.error.HTTPError(
            "http://llm.internal/v1/chat/completions",
            404,
            "Not Found",
            {},
            None,
        )

        with (
            patch(
                "pilot107.core.agent.urllib.request.urlopen",
                side_effect=error,
            ) as urlopen,
            self.assertRaises(AgentProviderError) as raised,
        ):
            provider.explain(explanation)

        self.assertEqual(raised.exception.code, "http_404")
        self.assertEqual(urlopen.call_count, 1)

    def test_openai_provider_accepts_closed_thinking_prefix_before_strict_json(self) -> None:
        fact = AgentFact(
            fact_id="fact_thinking",
            statement="The job timed out.",
            evidence_refs=("evidence://runs/run_thinking/logs/stderr.tail.txt",),
            confidence="high",
            evidence_object_ids=("ev_thinking",),
        )
        explanation = AgentExplanation(
            run_id="run_thinking",
            provider="none",
            status="explained",
            summary="Timeout.",
            facts=(fact,),
            diagnoses=(),
        )
        content = "<think>internal reasoning</think>\n" + json.dumps(
            {
                "summary": "Timeout.",
                "narrative": "The cited log reports a timeout.",
                "recommendations": ["Increase the validated time limit."],
                "warnings": [],
                "citations": [
                    {
                        "fact_id": "fact_thinking",
                        "evidence_object_ids": ["ev_thinking"],
                    }
                ],
            }
        )
        provider = OpenAICompatibleLLMProvider(
            base_url="http://llm.internal/v1",
            api_key=None,
            model="thinking-model",
            max_attempts=1,
        )

        with patch(
            "pilot107.core.agent.urllib.request.urlopen",
            return_value=FakeHttpResponse(_chat_response(content)),
        ):
            result = provider.explain(explanation)

        self.assertEqual(result.citations[0].fact_id, "fact_thinking")

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


class FakeHttpResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self.body if limit < 0 else self.body[:limit]


def _chat_response(content: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


if __name__ == "__main__":
    unittest.main()
