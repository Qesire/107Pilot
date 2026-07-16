"""Contract tests for ``RestNativeSlurmBackend`` against a fake slurmrestd.

These tests cover the REST convergence matrix from
``docs/phase-0/docker_mainline_plan.md`` §5 and the submit smoke list from
``docs/phase-1/submission_strategy.md`` §3 at the adapter contract level — no
real Slurm required.

Two fake-server approaches are used, chosen for simplicity and to match the
project's existing test style:

* ``ScriptedTransport`` — an in-process ``HttpTransport`` stub injected directly
  into ``RestNativeSlurmBackend``. Used for the full submit/get/cancel matrix
  where only transport-level control is needed.
* ``FakeSlurmRestServer`` — a real ``http.server.ThreadingHTTPServer`` used only
  for the ``RestAuthStyle.SLURM_HEADERS`` path, where we must assert the real
  HTTP headers ``UrllibHttpTransport`` emits over the wire.
"""

from __future__ import annotations

import json
import threading
import unittest
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from pilot107.adapters.slurm import (
    HttpResponse,
    RestAuthStyle,
    RestNativeSlurmBackend,
    SlurmAuthError,
    SlurmBackendError,
    SlurmSubmissionRejected,
    SlurmTransportError,
    SubmitIntent,
    UrllibHttpTransport,
)
from pilot107.core.resources import ResourcePlan
from pilot107.core.states import RunState

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _plan() -> ResourcePlan:
    return ResourcePlan(
        partition="debug",
        qos="normal",
        nodes=1,
        ntasks=1,
        cpus_per_task=1,
        time_limit="00:05:00",
    )


def _intent(
    *,
    user: str = "alice",
    workdir: str = "/public/home/alice/run-1",
    script: str = "#!/bin/bash\nhostname\n",
    idempotency_key: str | None = None,
) -> SubmitIntent:
    return SubmitIntent(
        user=user,
        workdir=Path(workdir),
        script=script,
        resource_plan=_plan(),
        idempotency_key=idempotency_key,
    )


# --------------------------------------------------------------------------- #
# In-process scripted transport (matrix coverage)
# --------------------------------------------------------------------------- #


@dataclass
class _CapturedCall:
    method: str
    path: str
    token: str | None
    payload: dict[str, Any] | None


class ScriptedTransport:
    """In-process ``HttpTransport`` stub with scripted responses and capture."""

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


class RaisingTransport:
    """Transport that always raises a specified exception (connection failure)."""

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


# --------------------------------------------------------------------------- #
# Real-socket fake slurmrestd (SLURM_HEADERS header assertions only)
# --------------------------------------------------------------------------- #


@dataclass
class _CapturedHttpRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class _ScriptedHttpReply:
    status: int
    payload: dict[str, object]


@dataclass
class _FakeServerState:
    replies: list[_ScriptedHttpReply] = field(default_factory=list)
    captured: list[_CapturedHttpRequest] = field(default_factory=list)


class _FakeSlurmHandler(BaseHTTPRequestHandler):
    state: _FakeServerState

    def _handle(self, method: str, body: bytes = b"") -> None:
        self.state.captured.append(
            _CapturedHttpRequest(
                method=method,
                path=self.path,
                headers={k: v for k, v in self.headers.items()},
                body=body,
            )
        )
        if not self.state.replies:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"errors":[{"description":"no scripted reply"}]}')
            return
        reply = self.state.replies.pop(0)
        reply_body = json.dumps(reply.payload).encode("utf-8")
        self.send_response(reply.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply_body)))
        self.end_headers()
        self.wfile.write(reply_body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        self._handle("POST", body)

    def do_DELETE(self) -> None:  # noqa: N802 - http.server API
        self._handle("DELETE")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - signature
        return


class FakeSlurmRestServer:
    """Real-socket fake slurmrestd for header-level assertions."""

    def __init__(self) -> None:
        self.state = _FakeServerState()
        handler = type(
            "BoundFakeSlurmHandler",
            (_FakeSlurmHandler,),
            {"state": self.state},
        )
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)

    @property
    def base_url(self) -> str:
        address = self._server.server_address
        host = str(address[0])
        port = int(address[1])
        return f"http://{host}:{port}"

    def queue(self, status: int, payload: dict[str, object]) -> None:
        self.state.replies.append(_ScriptedHttpReply(status=status, payload=payload))

    @property
    def captured(self) -> list[_CapturedHttpRequest]:
        return self.state.captured

    def __enter__(self) -> FakeSlurmRestServer:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


# --------------------------------------------------------------------------- #
# Matrix: submit scenarios
# --------------------------------------------------------------------------- #


class RestSubmitMatrixTests(unittest.TestCase):
    def test_matrix1_valid_shared_workdir_submit_success(self) -> None:
        """Matrix #1 / smoke #7: REST + valid shared workdir → submit success."""
        transport = ScriptedTransport([HttpResponse(200, {"job_id": 4242, "errors": []})])
        backend = RestNativeSlurmBackend(transport=transport, token="tok")

        receipt = backend.submit(_intent(workdir="/public/home/alice/run-1"))

        self.assertEqual(receipt.job_id, "4242")
        self.assertEqual(receipt.run_state, RunState.SUBMITTED)
        self.assertEqual(receipt.strategy, "rest_native")
        call = transport.calls[0]
        self.assertEqual(call.method, "POST")
        self.assertEqual(call.path, "/slurm/v0.0.41/job/submit")
        self.assertEqual(call.token, "tok")
        assert call.payload is not None
        self.assertEqual(
            call.payload["job"]["current_working_directory"], "/public/home/alice/run-1"
        )

    def test_matrix2_nonexistent_workdir_structured_failure(self) -> None:
        """Matrix #2 / smoke #8: REST + nonexistent workdir → SlurmSubmissionRejected."""
        transport = ScriptedTransport(
            [
                HttpResponse(
                    200,
                    {"errors": [{"description": "path does not exist", "error_number": 2001}]},
                )
            ]
        )
        backend = RestNativeSlurmBackend(transport=transport)

        with self.assertRaises(SlurmSubmissionRejected) as ctx:
            backend.submit(_intent(workdir="/public/home/alice/missing"))

        self.assertIn("REST submit rejected", str(ctx.exception))

    def test_matrix3_no_permission_workdir_structured_failure(self) -> None:
        """Matrix #3: REST + no-permission workdir → structured failure."""
        transport = ScriptedTransport(
            [
                HttpResponse(
                    403,
                    {"errors": [{"description": "permission denied", "error_number": 2}]},
                )
            ]
        )
        backend = RestNativeSlurmBackend(transport=transport)

        with self.assertRaises(SlurmSubmissionRejected):
            backend.submit(_intent(workdir="/public/home/bob"))

    def test_smoke9_unwritable_output_structured_failure(self) -> None:
        """Smoke #9: unwritable output dir → REST returns structured error."""
        transport = ScriptedTransport(
            [
                HttpResponse(
                    200,
                    {"errors": [{"description": "output directory not writable"}]},
                )
            ]
        )
        backend = RestNativeSlurmBackend(transport=transport)

        with self.assertRaises(SlurmSubmissionRejected):
            backend.submit(_intent(workdir="/public/home/alice/run-1"))

    def test_matrix4_local_tmp_output_path_forwarded_not_enforced(self) -> None:
        """Matrix #4: local /tmp workdir is forwarded; adapter does not enforce.

        WorkDirPreflight (/tmp rejection) is a service-layer concern (Lane 4);
        the REST adapter itself only validates the resource plan and forwards the
        workdir to slurmrestd. This test pins that contract: a /tmp workdir is
        passed through unchanged, and submit succeeds when slurmrestd accepts it.
        """
        transport = ScriptedTransport([HttpResponse(200, {"job_id": 7, "errors": []})])
        backend = RestNativeSlurmBackend(transport=transport)

        receipt = backend.submit(_intent(workdir="/tmp/pilot107-local"))

        self.assertEqual(receipt.job_id, "7")
        call = transport.calls[0]
        assert call.payload is not None
        self.assertEqual(call.payload["job"]["current_working_directory"], "/tmp/pilot107-local")

    def test_matrix5_timeout_propagates_as_transport_error(self) -> None:
        """Matrix #5: submit timeout raises SlurmTransportError cleanly.

        Idempotency replay (marker/time-window reconciliation) is a service-layer
        concern; the adapter surfaces the timeout as ``SlurmTransportError`` so
        the service layer can reconcile. See ``test_idempotency_key_not_deduped``
        for the contract-level gap note.
        """
        transport = RaisingTransport(SlurmTransportError("timeout contacting slurmrestd"))
        backend = RestNativeSlurmBackend(transport=transport)

        with self.assertRaises(SlurmTransportError):
            backend.submit(_intent())

    def test_idempotency_key_not_deduped_at_adapter(self) -> None:
        """Contract gap: RestNativeSlurmBackend does not dedupe idempotency_key.

        Two submits with the same ``idempotency_key`` each issue a fresh POST.
        Reconciliation (no-double-submit) must be handled by the service layer
        via marker/time-window queries (Lane 3/4). This test documents the gap.
        """
        transport = ScriptedTransport(
            [
                HttpResponse(200, {"job_id": 11, "errors": []}),
                HttpResponse(200, {"job_id": 12, "errors": []}),
            ]
        )
        backend = RestNativeSlurmBackend(transport=transport)
        intent = _intent(idempotency_key="run-x:submit")

        first = backend.submit(intent)
        second = backend.submit(intent)

        self.assertEqual(first.job_id, "11")
        self.assertEqual(second.job_id, "12")
        self.assertEqual(len(transport.calls), 2)

    def test_matrix6_transport_unavailable_raises_transport_error(self) -> None:
        """Matrix #6: transport unavailable → SlurmTransportError (clean raise)."""
        transport = RaisingTransport(SlurmTransportError("connection refused"))
        backend = RestNativeSlurmBackend(transport=transport)

        with self.assertRaises(SlurmTransportError) as ctx:
            backend.submit(_intent())

        # The adapter must not swallow this into a generic SlurmBackendError subtype.
        self.assertNotIsInstance(ctx.exception, SlurmSubmissionRejected)

    def test_submit_missing_job_id_is_transport_error(self) -> None:
        """A successful HTTP 200 without a job_id is a transport-level fault."""
        transport = ScriptedTransport([HttpResponse(200, {"errors": []})])
        backend = RestNativeSlurmBackend(transport=transport)

        with self.assertRaises(SlurmTransportError):
            backend.submit(_intent())


# --------------------------------------------------------------------------- #
# Matrix: get_job read scenarios
# --------------------------------------------------------------------------- #


class RestGetJobTests(unittest.TestCase):
    def test_get_job_success_normalizes_state(self) -> None:
        transport = ScriptedTransport(
            [
                HttpResponse(
                    200,
                    {"jobs": [{"job_id": 4242, "user_name": "alice", "job_state": "RUNNING"}]},
                )
            ]
        )
        backend = RestNativeSlurmBackend(transport=transport)

        snapshot = backend.get_job(user="alice", job_id="4242")

        self.assertEqual(snapshot.run_state, RunState.RUNNING)
        self.assertEqual(snapshot.owner, "alice")
        self.assertEqual(snapshot.job_id, "4242")
        call = transport.calls[0]
        self.assertEqual(call.method, "GET")
        self.assertEqual(call.path, "/slurm/v0.0.41/job/4242")

    def test_get_job_cross_user_rejected_as_auth_error(self) -> None:
        transport = ScriptedTransport(
            [
                HttpResponse(
                    200,
                    {"jobs": [{"job_id": 4242, "user_name": "bob", "job_state": "RUNNING"}]},
                )
            ]
        )
        backend = RestNativeSlurmBackend(transport=transport)

        with self.assertRaises(SlurmAuthError):
            backend.get_job(user="alice", job_id="4242")

    def test_get_job_unknown_job_is_transport_error(self) -> None:
        transport = ScriptedTransport(
            [HttpResponse(404, {"errors": [{"description": "job not found"}]})]
        )
        backend = RestNativeSlurmBackend(transport=transport)

        with self.assertRaises(SlurmTransportError):
            backend.get_job(user="alice", job_id="9999")

    def test_get_job_semantic_error_payload_is_transport_error(self) -> None:
        transport = ScriptedTransport(
            [HttpResponse(200, {"errors": [{"description": "slurmdb unavailable"}]})]
        )
        backend = RestNativeSlurmBackend(transport=transport)

        with self.assertRaises(SlurmTransportError):
            backend.get_job(user="alice", job_id="4242")

    def test_get_job_rejects_unsafe_job_id(self) -> None:
        backend = RestNativeSlurmBackend(transport=ScriptedTransport([]))

        with self.assertRaises(SlurmSubmissionRejected):
            backend.get_job(user="alice", job_id="../etc/passwd")


# --------------------------------------------------------------------------- #
# Matrix: cancel scenarios
# --------------------------------------------------------------------------- #


class RestCancelTests(unittest.TestCase):
    def test_cancel_success_returns_cancelled_snapshot(self) -> None:
        transport = ScriptedTransport([HttpResponse(200, {"errors": []})])
        backend = RestNativeSlurmBackend(transport=transport)

        snapshot = backend.cancel(user="alice", job_id="4242")

        self.assertEqual(snapshot.run_state, RunState.CANCELLED)
        self.assertEqual(snapshot.owner, "alice")
        self.assertEqual(snapshot.raw_state_flags, ["CANCELLED"])
        call = transport.calls[0]
        self.assertEqual(call.method, "DELETE")
        self.assertEqual(call.path, "/slurm/v0.0.41/job/4242")

    def test_cancel_already_terminal_job_returns_cancelled_snapshot(self) -> None:
        """Cancel of an already-terminal job: adapter issues DELETE and reports CANCELLED.

        Slurm semantics for cancelling a completed job return a non-fatal error;
        the adapter currently treats any error payload as ``SlurmTransportError``.
        This test pins the contract: a clean 200 yields CANCELLED regardless of
        prior job state (the adapter does not pre-read state before cancelling).
        """
        transport = ScriptedTransport([HttpResponse(200, {"errors": []})])
        backend = RestNativeSlurmBackend(transport=transport)

        snapshot = backend.cancel(user="alice", job_id="4242")

        self.assertEqual(snapshot.run_state, RunState.CANCELLED)

    def test_cancel_failure_is_transport_error(self) -> None:
        transport = ScriptedTransport(
            [HttpResponse(500, {"errors": [{"description": "slurmctld down"}]})]
        )
        backend = RestNativeSlurmBackend(transport=transport)

        with self.assertRaises(SlurmTransportError):
            backend.cancel(user="alice", job_id="4242")


# --------------------------------------------------------------------------- #
# Semantic error classification
# --------------------------------------------------------------------------- #


class RestSemanticClassificationTests(unittest.TestCase):
    def test_http200_with_errors_payload_classified_as_rejected(self) -> None:
        """Slurm returns HTTP 200 but embeds ``errors`` → submit rejected.

        ``check_slurm_rest_semantics`` classifies a non-empty ``errors`` list as
        ``RestSemanticLevel.ERROR`` even on HTTP 200; the adapter maps that to
        ``SlurmSubmissionRejected`` for submit.
        """
        transport = ScriptedTransport(
            [HttpResponse(200, {"errors": [{"description": "invalid qos"}]})]
        )
        backend = RestNativeSlurmBackend(transport=transport)

        with self.assertRaises(SlurmSubmissionRejected):
            backend.submit(_intent())

    def test_http4xx_with_errors_payload_classified_as_rejected(self) -> None:
        transport = ScriptedTransport(
            [HttpResponse(400, {"errors": [{"description": "bad request"}]})]
        )
        backend = RestNativeSlurmBackend(transport=transport)

        with self.assertRaises(SlurmSubmissionRejected):
            backend.submit(_intent())

    def test_http5xx_classified_as_rejected_for_submit(self) -> None:
        transport = ScriptedTransport(
            [HttpResponse(500, {"errors": [{"description": "internal"}]})]
        )
        backend = RestNativeSlurmBackend(transport=transport)

        with self.assertRaises(SlurmSubmissionRejected):
            backend.submit(_intent())

    def test_warnings_payload_does_not_block_submit(self) -> None:
        """A warnings-only payload (no errors) is a successful submit."""
        transport = ScriptedTransport(
            [HttpResponse(200, {"job_id": 55, "warnings": ["deprecated field"]})]
        )
        backend = RestNativeSlurmBackend(transport=transport)

        receipt = backend.submit(_intent())

        self.assertEqual(receipt.job_id, "55")


# --------------------------------------------------------------------------- #
# SLURM_HEADERS auth (real socket fake slurmrestd)
# --------------------------------------------------------------------------- #


class SlurmHeadersAuthTests(unittest.TestCase):
    def test_slurm_headers_sends_both_headers_over_wire(self) -> None:
        """SLURM_HEADERS path: real HTTP asserts X-SLURM-USER-NAME + X-SLURM-USER-TOKEN.

        Per @librarian research, Slurm 23.11 JWT auth requires these two headers
        (not ``Authorization: Bearer``). The adapter must send both.
        """
        with FakeSlurmRestServer() as server:
            server.queue(200, {"job_id": 909, "errors": []})
            transport = UrllibHttpTransport(
                base_url=server.base_url,
                auth_style=RestAuthStyle.SLURM_HEADERS,
                slurm_username="alice",
            )
            backend = RestNativeSlurmBackend(transport=transport, token="jwt-token-value")

            receipt = backend.submit(_intent())

        self.assertEqual(receipt.job_id, "909")
        self.assertEqual(len(server.captured), 1)
        request = server.captured[0]
        headers = {k.lower(): v for k, v in request.headers.items()}
        self.assertEqual(headers.get("x-slurm-user-name"), "alice")
        self.assertEqual(headers.get("x-slurm-user-token"), "jwt-token-value")
        # Bearer header must NOT be sent under SLURM_HEADERS.
        self.assertNotIn("authorization", headers)

    def test_slurm_headers_post_body_contains_script_and_job(self) -> None:
        """Verify the real POST body shape for the SLURM_HEADERS path."""
        with FakeSlurmRestServer() as server:
            server.queue(200, {"job_id": 910, "errors": []})
            transport = UrllibHttpTransport(
                base_url=server.base_url,
                auth_style=RestAuthStyle.SLURM_HEADERS,
                slurm_username="alice",
            )
            backend = RestNativeSlurmBackend(transport=transport, token="jwt")

            backend.submit(_intent(script="#!/bin/bash\necho hi\n"))

        request = server.captured[0]
        body = json.loads(request.body.decode("utf-8"))
        self.assertEqual(body["script"], "#!/bin/bash\necho hi\n")
        self.assertIn("job", body)
        self.assertEqual(body["job"]["current_working_directory"], "/public/home/alice/run-1")

    def test_slurm_headers_missing_username_raises_transport_error(self) -> None:
        """SLURM_HEADERS without slurm_username → SlurmTransportError."""
        with FakeSlurmRestServer() as server:
            transport = UrllibHttpTransport(
                base_url=server.base_url,
                auth_style=RestAuthStyle.SLURM_HEADERS,
                slurm_username=None,
            )
            backend = RestNativeSlurmBackend(transport=transport, token="jwt")

            with self.assertRaises(SlurmTransportError):
                backend.submit(_intent())

        # No request should have reached the server.
        self.assertEqual(len(server.captured), 0)

    def test_bearer_auth_sends_authorization_header_over_wire(self) -> None:
        """BEARER path: real HTTP asserts Authorization: Bearer <token>."""
        with FakeSlurmRestServer() as server:
            server.queue(200, {"job_id": 911, "errors": []})
            transport = UrllibHttpTransport(
                base_url=server.base_url,
                auth_style=RestAuthStyle.BEARER,
            )
            backend = RestNativeSlurmBackend(transport=transport, token="bearer-tok")

            receipt = backend.submit(_intent())

        self.assertEqual(receipt.job_id, "911")
        headers = {k.lower(): v for k, v in server.captured[0].headers.items()}
        self.assertEqual(headers.get("authorization"), "Bearer bearer-tok")
        self.assertNotIn("x-slurm-user-name", headers)
        self.assertNotIn("x-slurm-user-token", headers)

    def test_slurm_headers_get_job_sends_headers(self) -> None:
        """get_job under SLURM_HEADERS also sends both auth headers."""
        with FakeSlurmRestServer() as server:
            server.queue(
                200,
                {"jobs": [{"job_id": 912, "user_name": "alice", "job_state": "RUNNING"}]},
            )
            transport = UrllibHttpTransport(
                base_url=server.base_url,
                auth_style=RestAuthStyle.SLURM_HEADERS,
                slurm_username="alice",
            )
            backend = RestNativeSlurmBackend(transport=transport, token="jwt")

            snapshot = backend.get_job(user="alice", job_id="912")

        self.assertEqual(snapshot.run_state, RunState.RUNNING)
        headers = {k.lower(): v for k, v in server.captured[0].headers.items()}
        self.assertEqual(headers.get("x-slurm-user-name"), "alice")
        self.assertEqual(headers.get("x-slurm-user-token"), "jwt")


# --------------------------------------------------------------------------- #
# Adapter error hierarchy sanity
# --------------------------------------------------------------------------- #


class RestErrorHierarchyTests(unittest.TestCase):
    def test_transport_and_submission_errors_are_backend_errors(self) -> None:
        self.assertTrue(issubclass(SlurmTransportError, SlurmBackendError))
        self.assertTrue(issubclass(SlurmSubmissionRejected, SlurmBackendError))
        self.assertTrue(issubclass(SlurmAuthError, SlurmBackendError))


if __name__ == "__main__":
    unittest.main()
