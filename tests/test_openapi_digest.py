"""Tests for the OpenAPI digest auto-refresh building blocks in ``platform.py``.

Covers:
* ``compute_openapi_digest`` is deterministic and key-order independent.
* ``refresh_openapi_digest`` returns the expected digest from a scripted
  ``HttpTransport`` and raises ``SlurmTransportError`` on HTTP error.
* ``dataclasses.replace`` produces an updated snapshot/capability with the new
  digest and unchanged other fields.
* The token is never present in digest output or in any raised exception.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Any

from pilot107.adapters.slurm import HttpResponse, SlurmTransportError
from pilot107.core.platform import (
    RestCapability,
    compute_openapi_digest,
    docker_sim_configuration_snapshot,
    refresh_configuration_snapshot_digest,
    refresh_openapi_digest,
    refresh_rest_capability_digest,
)

_TOKEN = "secret-jwt-must-not-leak-abcdef0123456789"


@dataclass
class _CapturedCall:
    method: str
    path: str
    token: str | None
    payload: dict[str, Any] | None


class _ScriptedTransport:
    """Minimal in-process ``HttpTransport`` stub for digest tests."""

    def __init__(self, responses: list[HttpResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[_CapturedCall] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> HttpResponse:
        self.calls.append(_CapturedCall(method=method, path=path, token=token, payload=payload))
        if not self._responses:
            raise SlurmTransportError("no scripted response remains")
        return self._responses.pop(0)


class _RaisingTransport:
    """Transport that always raises a supplied exception."""

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.calls: list[_CapturedCall] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> HttpResponse:
        self.calls.append(_CapturedCall(method=method, path=path, token=token, payload=payload))
        raise self._error


class ComputeOpenapiDigestTests(unittest.TestCase):
    def test_digest_is_deterministic_and_full_64_hex(self) -> None:
        payload = {"openapi": "3.0.3", "info": {"title": "slurm"}, "paths": {}}
        digest = compute_openapi_digest(payload)

        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, compute_openapi_digest(payload))

    def test_digest_is_key_order_independent(self) -> None:
        a = {"openapi": "3.0.3", "paths": {}, "info": {"title": "slurm"}}
        b = {"info": {"title": "slurm"}, "openapi": "3.0.3", "paths": {}}

        self.assertEqual(compute_openapi_digest(a), compute_openapi_digest(b))

    def test_digest_accepts_bytes_and_str(self) -> None:
        canonical = '{"a":1}'
        self.assertEqual(
            compute_openapi_digest(canonical),
            compute_openapi_digest(canonical.encode("utf-8")),
        )

    def test_different_payloads_produce_different_digests(self) -> None:
        self.assertNotEqual(
            compute_openapi_digest({"a": 1}),
            compute_openapi_digest({"a": 2}),
        )

    def test_token_never_appears_in_digest(self) -> None:
        payload = {"info": {"title": "slurm"}, "token_leaked": _TOKEN}

        self.assertNotIn(_TOKEN, compute_openapi_digest(payload))


class RefreshOpenapiDigestTests(unittest.TestCase):
    def test_returns_expected_digest_from_scripted_openapi(self) -> None:
        openapi_doc = {"openapi": "3.0.3", "paths": {"/slurm/v0.0.41/ping": {}}}
        transport = _ScriptedTransport([HttpResponse(200, openapi_doc)])

        digest = refresh_openapi_digest(transport, "v0.0.41", token=_TOKEN)

        self.assertEqual(digest, compute_openapi_digest(openapi_doc))
        # Only GET /openapi/v3 issued, token forwarded to the transport only.
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call.method, "GET")
        self.assertEqual(call.path, "/openapi/v3")
        self.assertEqual(call.token, _TOKEN)
        self.assertNotIn(_TOKEN, digest)

    def test_raises_slurm_transport_error_on_http_error(self) -> None:
        transport = _ScriptedTransport(
            [HttpResponse(401, {"errors": [{"description": "unauthorized"}]})]
        )

        with self.assertRaises(SlurmTransportError):
            refresh_openapi_digest(transport, "v0.0.41", token=_TOKEN)

    def test_raises_on_empty_or_non_object_body(self) -> None:
        transport = _ScriptedTransport([HttpResponse(200, {})])

        with self.assertRaises(SlurmTransportError):
            refresh_openapi_digest(transport, "v0.0.41", token=_TOKEN)

    def test_raises_slurm_transport_error_when_transport_raises(self) -> None:
        # A SlurmTransportError carrying the token would be a leak; use a
        # neutral message to confirm the function does not unwrap/re-wrap it
        # with the token included.
        transport = _RaisingTransport(SlurmTransportError("connection refused"))

        with self.assertRaises(SlurmTransportError) as ctx:
            refresh_openapi_digest(transport, "v0.0.41", token=_TOKEN)

        self.assertNotIn(_TOKEN, str(ctx.exception))

    def test_token_not_in_exception_message_on_http_error(self) -> None:
        transport = _ScriptedTransport(
            [HttpResponse(500, {"errors": [{"description": _TOKEN}]})]
        )

        with self.assertRaises(SlurmTransportError) as ctx:
            refresh_openapi_digest(transport, "v0.0.41", token=_TOKEN)

        self.assertNotIn(_TOKEN, str(ctx.exception))


class RefreshSnapshotAndCapabilityTests(unittest.TestCase):
    def test_refresh_configuration_snapshot_updates_digest_preserving_other_fields(
        self,
    ) -> None:
        original = docker_sim_configuration_snapshot(
            slurm_rest_url="http://127.0.0.1:6820",
            captured_at="2026-07-12T00:48:34+00:00",
        )
        openapi_doc = {"openapi": "3.0.3", "paths": {}}
        transport = _ScriptedTransport([HttpResponse(200, openapi_doc)])

        updated = refresh_configuration_snapshot_digest(original, transport, token=_TOKEN)

        self.assertEqual(updated.openapi_digest, compute_openapi_digest(openapi_doc))
        # Preserved fields.
        self.assertEqual(updated.cluster, original.cluster)
        self.assertEqual(updated.users, original.users)
        self.assertEqual(updated.endpoints, original.endpoints)
        self.assertEqual(updated.auth_strategy, original.auth_strategy)
        self.assertEqual(updated.captured_at, original.captured_at)
        self.assertEqual(updated.freshness_seconds, original.freshness_seconds)
        # Original frozen instance is untouched.
        self.assertNotEqual(original.openapi_digest, updated.openapi_digest)

    def test_refresh_rest_capability_updates_digest_preserving_flags(self) -> None:
        original = RestCapability(
            base_url="http://107.ustc.edu.cn:6820",
            api_version="v0.0.41",
            auth_strategy="single_user_jwt_bearer",
            supports_query=True,
            supports_submit=False,
            supports_cancel=False,
            supports_accounting=False,
            partial_payload_with_errors=True,
        )
        openapi_doc = {"openapi": "3.0.3", "paths": {"/ping": {}}}
        transport = _ScriptedTransport([HttpResponse(200, openapi_doc)])

        updated = refresh_rest_capability_digest(original, transport, token=_TOKEN)

        self.assertEqual(updated.openapi_digest, compute_openapi_digest(openapi_doc))
        self.assertEqual(updated.base_url, original.base_url)
        self.assertEqual(updated.api_version, original.api_version)
        self.assertEqual(updated.auth_strategy, original.auth_strategy)
        self.assertEqual(updated.supports_query, original.supports_query)
        self.assertEqual(updated.supports_submit, original.supports_submit)
        self.assertEqual(updated.supports_cancel, original.supports_cancel)
        self.assertEqual(updated.supports_accounting, original.supports_accounting)
        self.assertEqual(
            updated.partial_payload_with_errors, original.partial_payload_with_errors
        )
        self.assertIsNone(original.openapi_digest)

    def test_snapshot_to_payload_roundtrips_refreshed_digest(self) -> None:
        snapshot = docker_sim_configuration_snapshot()
        openapi_doc = {"openapi": "3.0.3"}
        transport = _ScriptedTransport([HttpResponse(200, openapi_doc)])

        updated = refresh_configuration_snapshot_digest(snapshot, transport, token=_TOKEN)

        payload = updated.to_payload()
        self.assertEqual(payload["openapi_digest"], updated.openapi_digest)
        self.assertNotIn(_TOKEN, str(payload))


if __name__ == "__main__":
    unittest.main()
