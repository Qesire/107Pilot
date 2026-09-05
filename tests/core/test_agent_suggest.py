"""Tests for the contract suggest LLM method and HTTP route."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from pilot107.agent.config import AgentdClientConfig
from pilot107.agent.protocol import AgentdClientError, AgentdTurnResult
from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.http_app import Pilot107HttpApi
from pilot107.api.http_types import ApiResponse
from pilot107.core.advice import _PATCHABLE_FIELDS
from pilot107.core.agent import (
    _CONTRACT_PATCH_ALLOWED_FIELDS,
    AgentProviderError,
    OpenAICompatibleLLMProvider,
    _parse_contract_patch_json,
    suggest_contract_patch_without_llm,
)
from pilot107.core.evidence_binding import EvidenceBinder
from pilot107.core.run_store import RunStore
from pilot107.worker.evidence import EvidenceStore


class _FakeAgentdClient:
    def __init__(
        self,
        *,
        result: Any = None,
        error: AgentdClientError | None = None,
    ) -> None:
        self.config = AgentdClientConfig(
            base_url="http://pilot-agentd:8091",
            token="internal-secret",
            model_profile_id="campus-default",
        )
        self.result = {} if result is None else result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def run_turn(self, **kwargs: Any) -> AgentdTurnResult:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return AgentdTurnResult(
            result=self.result,
            provider="campus-openai-compatible",
            model="campus-model",
            model_profile_id="campus-default",
            input_tokens=12,
            output_tokens=8,
            cache_read_tokens=None,
            cache_write_tokens=None,
            provider_calls=1,
            checkpoint_digest="d" * 64,
            duration_ms=42,
            checkpoint=None,
        )


_VALID_CONTRACT = {
    "entry": {"command": "python3 main.py"},
    "resources": {"cpus_per_task": 1, "memory": "1G"},
}


class SuggestContractPatchTests(unittest.TestCase):
    def test_returns_validated_patch_from_agentd_and_forces_confirmation(self) -> None:
        client = _FakeAgentdClient(
            result={
                "suggested_patch": {
                    "entry.command": "python3 train.py",
                    "resources.cpus_per_task": 2,
                    "resources.memory": "4G",
                },
                "explanation_zh": "把命令改成 python3 train.py，资源调整为 2 CPU 4G 内存。",
            }
        )
        provider = OpenAICompatibleLLMProvider(client=client)

        result = provider.suggest_contract_patch(
            current_contract=_VALID_CONTRACT,
            recipe_version_id="recipe_python_cpu@1.0.0",
            user_intent="我要跑一个 python 训练脚本，需要 2 个 CPU 和 4G 内存",
        )

        self.assertEqual(
            result["suggested_patch"],
            {
                "entry.command": "python3 train.py",
                "resources.cpus_per_task": 2,
                "resources.memory": "4G",
            },
        )
        self.assertIn("python3 train.py", result["explanation_zh"])
        self.assertTrue(result["needs_user_confirmation"])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["task_kind"], "contract_patch")
        self.assertEqual(
            client.calls[0]["input_payload"],
            {
                "recipe_version_id": "recipe_python_cpu@1.0.0",
                "user_intent": "我要跑一个 python 训练脚本，需要 2 个 CPU 和 4G 内存",
                "current_contract": _VALID_CONTRACT,
                "required_output": {
                    "suggested_patch": (
                        "object mapping Contract dot-path (e.g. entry.command, "
                        "resources.cpus_per_task, resources.memory) to new values; "
                        "empty object if the intent is unclear or unsafe"
                    ),
                    "explanation_zh": "简短的中文说明，解释这次建议的改动",
                },
            },
        )

    def test_does_not_retry_an_invalid_agentd_result_in_python(self) -> None:
        client = _FakeAgentdClient(
            result={
                "suggested_patch": {"entry.command": "python3 train.py"},
                "explanation_zh": "已调整命令。",
                "extra_field": "not allowed",
            }
        )
        provider = OpenAICompatibleLLMProvider(client=client)

        with self.assertRaises(AgentProviderError) as raised:
            provider.suggest_contract_patch(
                current_contract=_VALID_CONTRACT,
                recipe_version_id="recipe_python_cpu@1.0.0",
                user_intent="改一下命令",
            )

        self.assertEqual(raised.exception.code, "invalid_schema_fields")
        self.assertEqual(len(client.calls), 1)

    def test_agentd_timeout_maps_to_the_existing_provider_timeout_code(self) -> None:
        client = _FakeAgentdClient(
            error=AgentdClientError(
                "pilot-agentd Turn failed",
                code="provider_timeout",
                retryable=True,
                provider_status=408,
            )
        )
        provider = OpenAICompatibleLLMProvider(client=client)

        with self.assertRaises(AgentProviderError) as raised:
            provider.suggest_contract_patch(
                current_contract=_VALID_CONTRACT,
                recipe_version_id="recipe_python_cpu@1.0.0",
                user_intent="改一下",
            )

        self.assertEqual(raised.exception.code, "http_408")
        self.assertEqual(len(client.calls), 1)

    def test_without_llm_fallback_returns_empty_patch(self) -> None:
        result = suggest_contract_patch_without_llm()
        self.assertEqual(result["suggested_patch"], {})
        self.assertEqual(result["needs_user_confirmation"], False)
        self.assertIn("LLM", result["explanation_zh"])


class ContractAgentSuggestRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = RunStore(root / "pilot107.db")
        self.evidence_store = EvidenceStore(root / "evidence")
        self.evidence_query = EvidenceQueryService(
            store=self.store,
            evidence_store=self.evidence_store,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _api_with_llm(self, client: _FakeAgentdClient | None = None) -> Pilot107HttpApi:
        api = Pilot107HttpApi(
            store=self.store,
            evidence_query=self.evidence_query,
            agent_explain_service=_build_explain_service(
                self.store,
                self.evidence_store,
                client=client,
            ),
        )
        return api

    def _api_without_llm(self) -> Pilot107HttpApi:
        # Force from_env() to fail by clearing env vars and using a custom service.
        from pilot107.core.agent import AgentExplainService

        service = AgentExplainService(
            store=self.store,
            llm_provider=None,
            evidence_binder=EvidenceBinder(
                store=self.store,
                evidence_root=self.evidence_store.root,
            ),
        )
        return Pilot107HttpApi(
            store=self.store,
            evidence_query=self.evidence_query,
            agent_explain_service=service,
        )

    def _post(self, api: Pilot107HttpApi, body: dict) -> ApiResponse:
        return api.handle_post(
            "/api/v1/contracts/agent/suggest",
            body=json.dumps(body).encode("utf-8"),
            headers={"X-Pilot107-User": "alice"},
        )

    def test_route_returns_patch_from_llm(self) -> None:
        api = self._api_with_llm()
        response = self._post(
            api,
            {
                "current_contract": _VALID_CONTRACT,
                "recipe_version_id": "recipe_python_cpu@1.0.0",
                "user_intent": "需要 2 个 CPU 和 4G 内存",
                "provider": "local",
            },
        )

        self.assertEqual(response.status, 200)
        payload = response.payload
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(
            payload["suggested_patch"],
            {
                "entry.command": "python3 train.py",
                "resources.cpus_per_task": 2,
                "resources.memory": "4G",
            },
        )
        self.assertTrue(payload["needs_user_confirmation"])
        self.assertIn("调整", payload["explanation_zh"])

    def test_route_provider_none_returns_ok_empty_patch(self) -> None:
        api = self._api_with_llm()
        response = self._post(
            api,
            {
                "current_contract": _VALID_CONTRACT,
                "recipe_version_id": "recipe_python_cpu@1.0.0",
                "user_intent": "需要 2 个 CPU 和 4G 内存",
                "provider": "none",
            },
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["status"], "ok")
        self.assertEqual(response.payload["suggested_patch"], {})
        self.assertFalse(response.payload["needs_user_confirmation"])

    def test_route_llm_not_configured_returns_degraded(self) -> None:
        api = self._api_without_llm()
        response = self._post(
            api,
            {
                "current_contract": _VALID_CONTRACT,
                "recipe_version_id": "recipe_python_cpu@1.0.0",
                "user_intent": "需要 2 个 CPU 和 4G 内存",
                "provider": "local",
            },
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["status"], "degraded")
        self.assertEqual(response.payload["reason"], "provider_unconfigured")
        self.assertEqual(response.payload["suggested_patch"], {})
        self.assertFalse(response.payload["needs_user_confirmation"])

    def test_route_transport_error_returns_degraded(self) -> None:
        api = self._api_with_llm(
            _FakeAgentdClient(
                error=AgentdClientError(
                    "pilot-agentd Turn failed",
                    code="provider_timeout",
                    retryable=True,
                )
            )
        )
        response = self._post(
            api,
            {
                "current_contract": _VALID_CONTRACT,
                "recipe_version_id": "recipe_python_cpu@1.0.0",
                "user_intent": "改一下",
                "provider": "local",
            },
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["status"], "degraded")
        self.assertEqual(response.payload["reason"], "provider_timeout")
        self.assertEqual(response.payload["suggested_patch"], {})

    def test_route_invalid_json_returns_degraded(self) -> None:
        api = self._api_with_llm(
            _FakeAgentdClient(
                error=AgentdClientError(
                    "pilot-agentd protocol error",
                    code="provider_invalid_response",
                )
            )
        )
        response = self._post(
            api,
            {
                "current_contract": _VALID_CONTRACT,
                "recipe_version_id": "recipe_python_cpu@1.0.0",
                "user_intent": "改一下",
                "provider": "local",
            },
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["status"], "degraded")
        self.assertEqual(response.payload["reason"], "provider_parse_error")
        self.assertEqual(response.payload["suggested_patch"], {})

    def test_route_invalid_key_returns_degraded(self) -> None:
        api = self._api_with_llm(
            _FakeAgentdClient(
                error=AgentdClientError(
                    "pilot-agentd Turn failed",
                    code="provider_auth",
                    provider_status=401,
                )
            )
        )
        response = self._post(
            api,
            {
                "current_contract": _VALID_CONTRACT,
                "recipe_version_id": "recipe_python_cpu@1.0.0",
                "user_intent": "改一下",
                "provider": "local",
            },
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["status"], "degraded")
        self.assertEqual(response.payload["reason"], "provider_invalid_key")

    def test_route_missing_body_returns_400(self) -> None:
        api = self._api_with_llm()
        response = api.handle_post(
            "/api/v1/contracts/agent/suggest",
            body=b"not-json",
            headers={"X-Pilot107-User": "alice"},
        )
        self.assertEqual(response.status, 400)


class ParseContractPatchJsonTests(unittest.TestCase):
    """Python revalidates Agentd's typed result against domain policy."""

    def _patch_payload(self, patch: dict, explanation: str = "已调整。") -> dict:
        return {"suggested_patch": patch, "explanation_zh": explanation}

    def test_rejects_raw_text_because_agentd_owns_format_repair(self) -> None:
        with self.assertRaises(AgentProviderError) as raised:
            _parse_contract_patch_json("model returned prose")
        self.assertEqual(raised.exception.code, "invalid_schema_object")

    def test_typed_result_happy_path(self) -> None:
        result = _parse_contract_patch_json(self._patch_payload({"resources.cpus_per_task": 4}))
        self.assertEqual(result["suggested_patch"], {"resources.cpus_per_task": 4})

    def test_rejects_additional_result_fields(self) -> None:
        payload = self._patch_payload({"resources.cpus_per_task": 4})
        payload["unexpected"] = True
        with self.assertRaises(AgentProviderError) as raised:
            _parse_contract_patch_json(payload)
        self.assertEqual(raised.exception.code, "invalid_schema_fields")

    def test_rejects_proto_pollution_field(self) -> None:
        with self.assertRaises(AgentProviderError) as raised:
            _parse_contract_patch_json(self._patch_payload({"__proto__.polluted": True}))
        self.assertEqual(raised.exception.code, "invalid_schema_patch_field")

    def test_rejects_constructor_field(self) -> None:
        with self.assertRaises(AgentProviderError) as raised:
            _parse_contract_patch_json(self._patch_payload({"constructor.x": 1}))
        self.assertEqual(raised.exception.code, "invalid_schema_patch_field")

    def test_rejects_prototype_field(self) -> None:
        with self.assertRaises(AgentProviderError) as raised:
            _parse_contract_patch_json(self._patch_payload({"prototype.y": 1}))
        self.assertEqual(raised.exception.code, "invalid_schema_patch_field")

    def test_rejects_non_whitelisted_identity_field(self) -> None:
        with self.assertRaises(AgentProviderError) as raised:
            _parse_contract_patch_json(self._patch_payload({"owner_identity": "bob"}))
        self.assertEqual(raised.exception.code, "invalid_schema_patch_field")

    def test_rejects_non_whitelisted_misc_field(self) -> None:
        with self.assertRaises(AgentProviderError) as raised:
            _parse_contract_patch_json(self._patch_payload({"cid": "abc"}))
        self.assertEqual(raised.exception.code, "invalid_schema_patch_field")

    def test_accepts_whitelisted_field(self) -> None:
        result = _parse_contract_patch_json(
            self._patch_payload({"project.workdir": "/public/home/alice/work"})
        )
        self.assertEqual(result["suggested_patch"], {"project.workdir": "/public/home/alice/work"})

    def test_allowed_fields_match_advice_patchable_fields(self) -> None:
        # Drift detector: the agent whitelist must mirror advice._PATCHABLE_FIELDS
        # so the LLM can only suggest fields the remediation layer can apply.
        self.assertEqual(_CONTRACT_PATCH_ALLOWED_FIELDS, _PATCHABLE_FIELDS)


def _build_explain_service(
    store: RunStore,
    evidence_store: EvidenceStore,
    *,
    client: _FakeAgentdClient | None = None,
):
    from pilot107.core.agent import AgentExplainService

    actual_client = client or _FakeAgentdClient(
        result={
            "suggested_patch": {
                "entry.command": "python3 train.py",
                "resources.cpus_per_task": 2,
                "resources.memory": "4G",
            },
            "explanation_zh": "已根据描述调整命令和资源。",
        }
    )
    provider = OpenAICompatibleLLMProvider(client=actual_client)
    return AgentExplainService(
        store=store,
        llm_provider=provider,
        evidence_binder=EvidenceBinder(store=store, evidence_root=evidence_store.root),
    )


if __name__ == "__main__":
    unittest.main()
