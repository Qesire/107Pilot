import json
import tempfile
import unittest
from pathlib import Path

from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.http_app import Pilot107HttpApi
from pilot107.core.run_store import RunStore
from pilot107.worker.evidence import EvidenceStore


class RemediationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = RunStore(root / "pilot107.db")
        self.evidence = EvidenceStore(root / "evidence")
        self.api = Pilot107HttpApi(
            store=self.store,
            evidence_query=EvidenceQueryService(
                store=self.store,
                evidence_store=self.evidence,
            ),
            auth_required=True,
        )
        self.store.create_run(
            run_id="run_remediation_api",
            owner="alice",
            workdir="/public/home/alice",
            script="exit 1",
        )
        self.headers = {"X-Pilot107-User": "alice"}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_create_get_and_list_are_owner_scoped_and_idempotent(self) -> None:
        body = _json(
            {
                "request_key": "api-request-1",
                "automation_policy": "manual_approval",
                "budget": {"max_attempts": 2, "max_submissions": 1},
            }
        )
        created = self.api.handle_post(
            "/api/v1/runs/run_remediation_api/remediation-sessions",
            body=body,
            headers=self.headers,
        )
        replayed = self.api.handle_post(
            "/api/v1/runs/run_remediation_api/remediation-sessions",
            body=body,
            headers=self.headers,
        )
        session_id = created.payload["session_id"]
        detail = self.api.handle_get(
            f"/api/v1/remediation-sessions/{session_id}",
            headers=self.headers,
        )
        listed = self.api.handle_get(
            "/api/v1/remediation-sessions?state=waiting_evidence",
            headers=self.headers,
        )

        self.assertEqual(created.status, 201)
        self.assertEqual(replayed.status, 200)
        self.assertEqual(replayed.payload["session_id"], session_id)
        self.assertEqual(detail.status, 200)
        self.assertEqual(detail.payload["owner"], "alice")
        self.assertEqual([item["session_id"] for item in listed.payload["items"]], [session_id])

    def test_authenticated_identity_cannot_be_overridden_by_body(self) -> None:
        response = self.api.handle_post(
            "/api/v1/runs/run_remediation_api/remediation-sessions",
            body=_json({"request_key": "bob-request", "owner": "alice"}),
            headers={"X-Pilot107-User": "bob"},
        )

        self.assertEqual(response.status, 403)
        self.assertEqual(response.payload["error"]["code"], "AUTH.FORBIDDEN")

    def test_invalid_budget_is_rejected_without_creating_a_session(self) -> None:
        response = self.api.handle_post(
            "/api/v1/runs/run_remediation_api/remediation-sessions",
            body=_json({"request_key": "bad-budget", "budget": {"max_attempts": 0}}),
            headers=self.headers,
        )

        self.assertEqual(response.status, 400)
        listed = self.api.handle_get(
            "/api/v1/remediation-sessions",
            headers=self.headers,
        )
        self.assertEqual(listed.payload["items"], [])

    def test_owner_can_cancel_session_and_replay_is_idempotent(self) -> None:
        created = self.api.handle_post(
            "/api/v1/runs/run_remediation_api/remediation-sessions",
            body=_json({"request_key": "api-cancel"}),
            headers=self.headers,
        )
        session_id = created.payload["session_id"]
        body = _json({"expected_version": created.payload["version"]})

        cancelled = self.api.handle_post(
            f"/api/v1/remediation-sessions/{session_id}/cancel",
            body=body,
            headers=self.headers,
        )
        replayed = self.api.handle_post(
            f"/api/v1/remediation-sessions/{session_id}/cancel",
            body=body,
            headers=self.headers,
        )

        self.assertEqual(cancelled.status, 200)
        self.assertEqual(cancelled.payload["state"], "cancelled")
        self.assertEqual(replayed.status, 200)
        self.assertEqual(replayed.payload, cancelled.payload)


def _json(value: object) -> bytes:
    return json.dumps(value).encode()


if __name__ == "__main__":
    unittest.main()
