import json
import tempfile
import unittest
from pathlib import Path

from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.http_app import Pilot107HttpApi
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.core.run_store import RunStore
from pilot107.services.agent_session_service import AgentSessionService
from pilot107.worker.evidence import EvidenceStore


class AgentSessionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        database = root / "pilot107.db"
        self.agent_store = SQLiteAgentSessionStore(database)
        self.control = SQLiteControlRepository(database)
        self.service = AgentSessionService(
            store=self.agent_store,
            control_repository=self.control,
        )
        run_store = RunStore(database)
        self.api = Pilot107HttpApi(
            store=run_store,
            evidence_query=EvidenceQueryService(
                store=run_store,
                evidence_store=EvidenceStore(root / "evidence"),
            ),
            agent_session_service=self.service,
            auth_required=True,
        )
        self.alice = {"X-Pilot107-User": "alice"}
        self.bob = {"X-Pilot107-User": "bob"}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _create_session(self, request_key: str = "session-1"):
        return self.api.handle_post(
            "/api/v1/agent-sessions",
            body=_json(
                {
                    "request_key": request_key,
                    "model_profile_id": "faux-default",
                    "source": {"run_id": "run-1"},
                }
            ),
            headers=self.alice,
        )

    def _create_turn(self, session: dict, request_key: str = "turn-1"):
        return self.api.handle_post(
            f"/api/v1/agent-sessions/{session['session_id']}/turns",
            body=_json(
                {
                    "request_key": request_key,
                    "message": "why is run-1 pending?",
                    "expected_state_version": session["state_version"],
                }
            ),
            headers=self.alice,
        )

    def test_create_replay_and_list_are_owner_scoped(self) -> None:
        created = self._create_session()
        replayed = self._create_session()
        listed = self.api.handle_get("/api/v1/agent-sessions", headers=self.alice)
        bob_list = self.api.handle_get("/api/v1/agent-sessions", headers=self.bob)
        bob_get = self.api.handle_get(
            f"/api/v1/agent-sessions/{created.payload['session_id']}",
            headers=self.bob,
        )

        self.assertEqual(created.status, 201)
        self.assertEqual(replayed.status, 200)
        self.assertEqual(replayed.payload["session_id"], created.payload["session_id"])
        self.assertEqual(created.payload["owner"], "alice")
        self.assertEqual(created.payload["profile_id"], "hpc-readonly-v1")
        self.assertEqual([item["owner"] for item in listed.payload["items"]], ["alice"])
        self.assertEqual(bob_list.payload["items"], [])
        self.assertEqual(bob_get.status, 404)

    def test_create_repair_profile_preserves_exact_authoritative_bindings(self) -> None:
        response = self.api.handle_post(
            "/api/v1/agent-sessions",
            body=_json(
                {
                    "request_key": "repair-session",
                    "model_profile_id": "faux-default",
                    "profile_id": "run_diagnosis_repair",
                    "source": {
                        "project_id": "project-repair",
                        "workspace_id": "workspace-repair",
                        "run_id": "run-failed",
                        "remediation_session_id": "remsession-repair",
                    },
                }
            ),
            headers=self.alice,
        )

        self.assertEqual(response.status, 201)
        self.assertEqual(response.payload["profile_id"], "run_diagnosis_repair")
        self.assertEqual(
            response.payload["source"],
            {
                "project_id": "project-repair",
                "workspace_id": "workspace-repair",
                "run_id": "run-failed",
                "remediation_session_id": "remsession-repair",
            },
        )

    def test_owner_override_unknown_fields_and_oversized_message_are_rejected(self) -> None:
        override = self.api.handle_post(
            "/api/v1/agent-sessions",
            body=_json(
                {
                    "request_key": "override",
                    "model_profile_id": "faux-default",
                    "source": {},
                    "owner": "bob",
                }
            ),
            headers=self.alice,
        )
        session = self._create_session().payload
        oversized = self.api.handle_post(
            f"/api/v1/agent-sessions/{session['session_id']}/turns",
            body=_json(
                {
                    "request_key": "oversized",
                    "message": "x" * 64_001,
                    "expected_state_version": session["state_version"],
                }
            ),
            headers=self.alice,
        )

        self.assertEqual(override.status, 400)
        self.assertEqual(override.payload["error"]["code"], "AGENT.SESSION.INVALID_REQUEST")
        self.assertEqual(oversized.status, 400)
        self.assertEqual(oversized.payload["error"]["code"], "AGENT.TURN.INVALID_REQUEST")

    def test_turn_replay_stale_version_and_public_shape(self) -> None:
        session = self._create_session().payload
        created = self._create_turn(session)
        replayed = self._create_turn(session)
        stale = self.api.handle_post(
            f"/api/v1/agent-sessions/{session['session_id']}/turns",
            body=_json(
                {
                    "request_key": "turn-stale",
                    "message": "another question",
                    "expected_state_version": session["state_version"],
                }
            ),
            headers=self.alice,
        )

        self.assertEqual(created.status, 202)
        self.assertEqual(replayed.status, 200)
        self.assertEqual(created.payload["turn_id"], replayed.payload["turn_id"])
        self.assertEqual(stale.status, 409)
        self.assertEqual(stale.payload["error"]["code"], "AGENT.SESSION.CONFLICT")
        for forbidden in (
            "lease_owner",
            "lease_expires_at",
            "fencing_token",
            "final_checkpoint",
            "capability_token",
        ):
            self.assertNotIn(forbidden, created.payload)

    def test_list_cursor_is_opaque_and_bound_to_owner_and_state_filter(self) -> None:
        self._create_session("page-a")
        self._create_session("page-b")
        first = self.api.handle_get(
            "/api/v1/agent-sessions?limit=1",
            headers=self.alice,
        )
        cursor = first.payload["page"]["next_cursor"]
        second = self.api.handle_get(
            f"/api/v1/agent-sessions?limit=1&cursor={cursor}",
            headers=self.alice,
        )
        mismatched = self.api.handle_get(
            f"/api/v1/agent-sessions?limit=1&state=running&cursor={cursor}",
            headers=self.alice,
        )
        bob_cursor = self.api.handle_get(
            f"/api/v1/agent-sessions?limit=1&cursor={cursor}",
            headers=self.bob,
        )
        malformed = self.api.handle_get(
            "/api/v1/agent-sessions?cursor=not-a-cursor",
            headers=self.alice,
        )

        self.assertNotEqual(
            first.payload["items"][0]["session_id"],
            second.payload["items"][0]["session_id"],
        )
        self.assertEqual(mismatched.status, 400)
        self.assertEqual(bob_cursor.status, 400)
        self.assertEqual(malformed.status, 400)

    def test_events_resume_from_last_event_id_without_raw_checkpoint(self) -> None:
        session = self._create_session().payload
        turn = self._create_turn(session).payload
        claim = self.agent_store.claim_turn(
            turn["turn_id"],
            worker_id="worker-1",
            lease_seconds=30,
        )
        assert claim is not None
        self.agent_store.append_event(
            turn["turn_id"],
            claim=claim,
            sequence=1,
            event_type="turn_started",
            payload={"task_kind": "interactive"},
        )
        self.agent_store.append_event(
            turn["turn_id"],
            claim=claim,
            sequence=2,
            event_type="checkpoint",
            payload={
                "checkpoint": {
                    "digest": "a" * 64,
                    "messages": [{"role": "assistant", "content": "private chain"}],
                }
            },
        )
        self.agent_store.append_event(
            turn["turn_id"],
            claim=claim,
            sequence=3,
            event_type="message_delta",
            payload={"delta": "public answer"},
        )

        first = self.api.handle_get(
            f"/api/v1/agent-sessions/{session['session_id']}/events?limit=1",
            headers=self.alice,
        )
        resumed = self.api.handle_get(
            f"/api/v1/agent-sessions/{session['session_id']}/events",
            headers={**self.alice, "Last-Event-ID": str(first.payload["page"]["last_event_id"])},
        )
        bob = self.api.handle_get(
            f"/api/v1/agent-sessions/{session['session_id']}/events",
            headers=self.bob,
        )

        self.assertEqual([item["sequence"] for item in first.payload["items"]], [1])
        self.assertEqual([item["sequence"] for item in resumed.payload["items"]], [2, 3])
        self.assertNotIn("private chain", repr(resumed.payload))
        self.assertEqual(
            resumed.payload["items"][0]["payload"],
            {"checkpoint_digest": "a" * 64},
        )
        self.assertEqual(bob.status, 404)

    def test_cancel_is_owner_scoped_and_idempotent(self) -> None:
        session = self._create_session().payload
        turn = self._create_turn(session).payload
        body = _json({"expected_state_version": turn["state_version"]})
        path = f"/api/v1/agent-sessions/{session['session_id']}/turns/{turn['turn_id']}/cancel"

        bob = self.api.handle_post(path, body=body, headers=self.bob)
        cancelled = self.api.handle_post(path, body=body, headers=self.alice)
        replayed = self.api.handle_post(path, body=body, headers=self.alice)

        self.assertEqual(bob.status, 404)
        self.assertEqual(cancelled.status, 200)
        self.assertTrue(cancelled.payload["cancel_requested"])
        self.assertEqual(replayed.status, 200)
        self.assertEqual(replayed.payload, cancelled.payload)


def _json(value: object) -> bytes:
    return json.dumps(value).encode()


if __name__ == "__main__":
    unittest.main()
