import unittest
from pathlib import Path
from unittest.mock import patch

from pilot107.adapters.slurm import (
    HttpResponse,
    RestAuthStyle,
    RestNativeSlurmBackend,
    SlurmAuthError,
    SlurmSubmissionRejected,
    SlurmTransportError,
    SubmitIntent,
    UrllibHttpTransport,
)
from pilot107.core.resources import ResourcePlan
from pilot107.core.states import RunState


class FakeTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> HttpResponse:
        self.calls.append((method, path, payload))
        return self.responses.pop(0)


class FakeUrlopenResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return b'{"ok": true}'


def _valid_intent() -> SubmitIntent:
    return SubmitIntent(
        user="alice",
        workdir=Path("/public/home/alice/run-1"),
        script="#!/bin/bash\nhostname\n",
        resource_plan=ResourcePlan(
            partition="debug",
            qos="normal",
            nodes=1,
            ntasks=1,
            cpus_per_task=1,
            time_limit="00:05:00",
        ),
    )


class RestNativeSlurmBackendTests(unittest.TestCase):
    def test_submit_uses_slurm_submit_endpoint(self) -> None:
        transport = FakeTransport([HttpResponse(200, {"job_id": 1234, "errors": []})])
        backend = RestNativeSlurmBackend(transport=transport, token="tok")

        receipt = backend.submit(_valid_intent())

        method, path, payload = transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/slurm/v0.0.41/job/submit")
        self.assertEqual(payload["job"]["current_working_directory"], "/public/home/alice/run-1")
        self.assertEqual(receipt.job_id, "1234")
        self.assertEqual(receipt.run_state, RunState.SUBMITTED)

    def test_submit_includes_afterok_dependency_in_rest_job(self) -> None:
        transport = FakeTransport([HttpResponse(200, {"job_id": 1235, "errors": []})])
        backend = RestNativeSlurmBackend(transport=transport)
        intent = _valid_intent()
        intent = SubmitIntent(
            user=intent.user,
            workdir=intent.workdir,
            script=intent.script,
            resource_plan=intent.resource_plan,
            dependency_job_ids=("120", "121"),
        )

        backend.submit(intent)

        payload = transport.calls[0][2]
        self.assertEqual(payload["job"]["dependency"], "afterok:120:121")

    def test_submit_uses_explicit_per_run_job_name(self) -> None:
        transport = FakeTransport([HttpResponse(200, {"job_id": 1236, "errors": []})])
        backend = RestNativeSlurmBackend(transport=transport)
        intent = _valid_intent()
        intent = SubmitIntent(
            user=intent.user,
            workdir=intent.workdir,
            script=intent.script,
            resource_plan=intent.resource_plan,
            idempotency_key="run-one:submit",
            job_name="pilot107-run-0123456789abcdef",
        )

        backend.submit(intent)

        payload = transport.calls[0][2]
        self.assertEqual(payload["job"]["name"], "pilot107-run-0123456789abcdef")

    def test_submit_rejects_semantic_errors(self) -> None:
        transport = FakeTransport([HttpResponse(200, {"errors": [{"description": "bad qos"}]})])
        backend = RestNativeSlurmBackend(transport=transport)

        with self.assertRaises(SlurmSubmissionRejected):
            backend.submit(_valid_intent())

    def test_submit_requires_job_id(self) -> None:
        transport = FakeTransport([HttpResponse(200, {"errors": []})])
        backend = RestNativeSlurmBackend(transport=transport)

        with self.assertRaises(SlurmTransportError):
            backend.submit(_valid_intent())

    def test_get_job_normalizes_state(self) -> None:
        transport = FakeTransport(
            [
                HttpResponse(
                    200,
                    {"jobs": [{"job_id": 1234, "user_name": "alice", "job_state": "RUNNING"}]},
                )
            ]
        )
        backend = RestNativeSlurmBackend(transport=transport)

        snapshot = backend.get_job(user="alice", job_id="1234")

        self.assertEqual(snapshot.run_state, RunState.RUNNING)
        self.assertEqual(snapshot.owner, "alice")

    def test_get_job_rejects_foreign_owner(self) -> None:
        transport = FakeTransport(
            [
                HttpResponse(
                    200,
                    {"jobs": [{"job_id": 1234, "user_name": "bob", "job_state": "RUNNING"}]},
                )
            ]
        )
        backend = RestNativeSlurmBackend(transport=transport)

        with self.assertRaises(SlurmAuthError):
            backend.get_job(user="alice", job_id="1234")


class UrllibHttpTransportTests(unittest.TestCase):
    def test_bearer_auth_sets_authorization_header(self) -> None:
        captured = []

        def fake_urlopen(request, timeout):
            captured.append(request)
            return FakeUrlopenResponse()

        transport = UrllibHttpTransport(
            base_url="http://slurmrestd", auth_style=RestAuthStyle.BEARER
        )
        with patch("urllib.request.urlopen", fake_urlopen):
            transport.request("GET", "/slurm/v0.0.41/ping", token="tok")

        self.assertEqual(captured[0].headers["Authorization"], "Bearer tok")

    def test_slurm_headers_auth_sets_slurm_headers(self) -> None:
        captured = []

        def fake_urlopen(request, timeout):
            captured.append(request)
            return FakeUrlopenResponse()

        transport = UrllibHttpTransport(
            base_url="http://slurmrestd",
            auth_style=RestAuthStyle.SLURM_HEADERS,
            slurm_username="alice",
        )
        with patch("urllib.request.urlopen", fake_urlopen):
            transport.request("GET", "/slurm/v0.0.41/ping", token="tok")

        self.assertEqual(captured[0].headers["X-slurm-user-name"], "alice")
        self.assertEqual(captured[0].headers["X-slurm-user-token"], "tok")

    def test_slurm_headers_auth_requires_username(self) -> None:
        transport = UrllibHttpTransport(
            base_url="http://slurmrestd",
            auth_style=RestAuthStyle.SLURM_HEADERS,
        )

        with self.assertRaises(SlurmTransportError):
            transport.request("GET", "/slurm/v0.0.41/ping", token="tok")


if __name__ == "__main__":
    unittest.main()
