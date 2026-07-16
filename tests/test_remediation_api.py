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

    def test_list_cursor_is_filter_bound_and_get_supports_etag(self) -> None:
        for request_key in ("page-a", "page-b"):
            self.api.handle_post(
                "/api/v1/runs/run_remediation_api/remediation-sessions",
                body=_json({"request_key": request_key}),
                headers=self.headers,
            )
        first = self.api.handle_get(
            "/api/v1/remediation-sessions?limit=1",
            headers=self.headers,
        )
        cursor = first.payload["page"]["next_cursor"]
        second = self.api.handle_get(
            f"/api/v1/remediation-sessions?limit=1&cursor={cursor}",
            headers=self.headers,
        )
        mismatched = self.api.handle_get(
            f"/api/v1/remediation-sessions?limit=1&state=cancelled&cursor={cursor}",
            headers=self.headers,
        )
        detail_path = f"/api/v1/remediation-sessions/{first.payload['items'][0]['session_id']}"
        detail = self.api.handle_get(detail_path, headers=self.headers)
        not_modified = self.api.handle_get(
            detail_path,
            headers={**self.headers, "If-None-Match": detail.headers["ETag"]},
        )

        self.assertNotEqual(
            first.payload["items"][0]["session_id"],
            second.payload["items"][0]["session_id"],
        )
        self.assertEqual(mismatched.status, 400)
        self.assertEqual(not_modified.status, 304)

    def test_session_events_are_owner_scoped_and_incrementally_readable(self) -> None:
        created = self.api.handle_post(
            "/api/v1/runs/run_remediation_api/remediation-sessions",
            body=_json({"request_key": "api-events"}),
            headers=self.headers,
        )
        session_id = created.payload["session_id"]
        self.api.handle_post(
            f"/api/v1/remediation-sessions/{session_id}/cancel",
            body=_json({"expected_version": created.payload["version"]}),
            headers=self.headers,
        )

        first = self.api.handle_get(
            f"/api/v1/remediation-sessions/{session_id}/events?limit=1",
            headers=self.headers,
        )
        after = first.payload["page"]["next_after_event_id"]
        second = self.api.handle_get(
            f"/api/v1/remediation-sessions/{session_id}/events?after_event_id={after}",
            headers=self.headers,
        )
        forbidden = self.api.handle_get(
            f"/api/v1/remediation-sessions/{session_id}/events",
            headers={"X-Pilot107-User": "bob"},
        )

        self.assertEqual(first.payload["items"][0]["event_type"], "session.created")
        self.assertEqual(second.payload["items"][0]["event_type"], "session.state_changed")
        self.assertEqual(forbidden.status, 403)

    def test_owner_can_record_manual_takeover_with_required_reason(self) -> None:
        created = self.api.handle_post(
            "/api/v1/runs/run_remediation_api/remediation-sessions",
            body=_json({"request_key": "api-takeover"}),
            headers=self.headers,
        )
        session_id = created.payload["session_id"]
        missing_note = self.api.handle_post(
            f"/api/v1/remediation-sessions/{session_id}/takeover",
            body=_json({"expected_version": created.payload["version"]}),
            headers=self.headers,
        )
        blocked = self.api.handle_post(
            f"/api/v1/remediation-sessions/{session_id}/takeover",
            body=_json(
                {
                    "expected_version": created.payload["version"],
                    "note": "continue from a manually derived Contract",
                }
            ),
            headers=self.headers,
        )

        self.assertEqual(missing_note.status, 400)
        self.assertEqual(blocked.status, 200)
        self.assertEqual(blocked.payload["stop_reason"], "manual_takeover")


def _json(value: object) -> bytes:
    return json.dumps(value).encode()


if __name__ == "__main__":
    unittest.main()
