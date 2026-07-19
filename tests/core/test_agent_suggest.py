"""Tests for the contract suggest LLM method and HTTP route."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.http_app import Pilot107HttpApi
from pilot107.api.http_types import ApiResponse
from pilot107.core.agent import (
    AgentProviderError,
    OpenAICompatibleLLMProvider,
    suggest_contract_patch_without_llm,
)
from pilot107.core.evidence_binding import EvidenceBinder
from pilot107.core.run_store import RunStore
from pilot107.worker.evidence import EvidenceStore


class _FakeHttpResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self._body if limit < 0 else self._body[:limit]


def _chat_response(content: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")


_VALID_CONTRACT = {
    "entry": {"command": "python3 main.py"},
    "resources": {"cpus_per_task": 1, "memory": "1G"},
}


class SuggestContractPatchTests(unittest.TestCase):
    def test_returns_patch_and_explanation_from_llm(self) -> None:
        provider = OpenAICompatibleLLMProvider(
            base_url="http://llm.internal/v1",
            api_key="test-key",
            model="local-model",
            max_attempts=2,
        )
        body = _chat_response(
            json.dumps(
                {
                    "suggested_patch": {
                        "entry.command": "python3 train.py",
                        "resources.cpus_per_task": 2,
                        "resources.memory": "4G",
                    },
                    "explanation_zh": "把命令改成 python3 train.py，资源调整为 2 CPU 4G 内存。",
                },
                ensure_ascii=False,
            )
        )

        with patch(
            "pilot107.core.agent.urllib.request.urlopen",
            return_value=_FakeHttpResponse(body),
        ) as urlopen:
            result = provider.suggest_contract_patch(
                current_contract=_VALID_CONTRACT,
                recipe_version_id="recipe_python_cpu@1.0.0",
                user_intent="我要跑一个 python 训练脚本，需要 2 个 CPU 和 4G 内存",
            )

        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(
            result["suggested_patch"],
            {
                "entry.command": "python3 train.py",
                "resources.cpus_per_task": 2,
                "resources.memory": "4G",
            },
        )
        self.assertIn("python3 train.py", result["explanation_zh"])

    def test_retries_when_first_response_is_invalid_json(self) -> None:
        provider = OpenAICompatibleLLMProvider(
            base_url="http://llm.internal/v1",
            api_key=None,
            model="local-model",
            max_attempts=2,
        )
        invalid = _chat_response("not-json")
        valid = _chat_response(
            json.dumps(
                {
                    "suggested_patch": {"entry.command": "python3 train.py"},
                    "explanation_zh": "已调整命令。",
                }
            )
        )

        with patch(
            "pilot107.core.agent.urllib.request.urlopen",
            side_effect=[_FakeHttpResponse(invalid), _FakeHttpResponse(valid)],
        ) as urlopen:
            result = provider.suggest_contract_patch(
                current_contract=_VALID_CONTRACT,
                recipe_version_id="recipe_python_cpu@1.0.0",
                user_intent="改一下命令",
            )

        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(result["suggested_patch"], {"entry.command": "python3 train.py"})

    def test_rejects_payload_with_extra_fields(self) -> None:
        provider = OpenAICompatibleLLMProvider(
            base_url="http://llm.internal/v1",
            api_key=None,
            model="local-model",
            max_attempts=1,
        )
        body = _chat_response(
            json.dumps(
                {
                    "suggested_patch": {"entry.command": "python3 train.py"},
                    "explanation_zh": "ok",
                    "extra_field": "should not be here",
                }
            )
        )

        with (
            patch(
                "pilot107.core.agent.urllib.request.urlopen",
                return_value=_FakeHttpResponse(body),
            ),
            self.assertRaises(AgentProviderError),
        ):
            provider.suggest_contract_patch(
                current_contract=_VALID_CONTRACT,
                recipe_version_id="recipe_python_cpu@1.0.0",
                user_intent="改一下",
            )

    def test_transport_error_raises_agent_provider_error(self) -> None:
        provider = OpenAICompatibleLLMProvider(
            base_url="http://llm.internal/v1",
            api_key=None,
            model="local-model",
            max_attempts=1,
        )

        with (
            patch(
                "pilot107.core.agent.urllib.request.urlopen",
                side_effect=TimeoutError("read timed out"),
            ),
            self.assertRaises(AgentProviderError) as raised,
        ):
            provider.suggest_contract_patch(
                current_contract=_VALID_CONTRACT,
                recipe_version_id="recipe_python_cpu@1.0.0",
                user_intent="改一下",
            )

        self.assertEqual(raised.exception.code, "transport_error")

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

    def _api_with_llm(self) -> Pilot107HttpApi:
        api = Pilot107HttpApi(
            store=self.store,
            evidence_query=self.evidence_query,
            agent_explain_service=_build_explain_service(self.store, self.evidence_store),
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
        body = _chat_response(
            json.dumps(
                {
                    "suggested_patch": {
                        "entry.command": "python3 train.py",
                        "resources.cpus_per_task": 2,
                        "resources.memory": "4G",
                    },
                    "explanation_zh": "已根据描述调整命令和资源。",
                }
            )
        )
        with patch(
            "pilot107.core.agent.urllib.request.urlopen",
            return_value=_FakeHttpResponse(body),
        ):
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

    def test_route_provider_none_returns_empty_patch(self) -> None:
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
        self.assertEqual(response.payload["suggested_patch"], {})
        self.assertFalse(response.payload["needs_user_confirmation"])

    def test_route_llm_not_configured_falls_back(self) -> None:
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
        self.assertEqual(response.payload["suggested_patch"], {})
        self.assertFalse(response.payload["needs_user_confirmation"])
        self.assertIn("LLM", response.payload["explanation_zh"])

    def test_route_missing_body_returns_400(self) -> None:
        api = self._api_with_llm()
        response = api.handle_post(
            "/api/v1/contracts/agent/suggest",
            body=b"not-json",
            headers={"X-Pilot107-User": "alice"},
        )
        self.assertEqual(response.status, 400)


def _build_explain_service(store: RunStore, evidence_store: EvidenceStore):
    from pilot107.core.agent import AgentExplainService

    provider = OpenAICompatibleLLMProvider(
        base_url="http://llm.internal/v1",
        api_key="test-key",
        model="local-model",
        max_attempts=2,
    )
    return AgentExplainService(
        store=store,
        llm_provider=provider,
        evidence_binder=EvidenceBinder(store=store, evidence_root=evidence_store.root),
    )


if __name__ == "__main__":
    unittest.main()
