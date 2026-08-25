from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pilot107.adapters.slurm import InMemorySlurmBackend
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.agent.task_store import SQLiteAgentTaskStore
from pilot107.agent.tasks import AgentResourceEnvelope, AgentTaskRequest
from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.http_app import Pilot107HttpApi
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.core.run_service import RunService
from pilot107.core.run_store import RunStore
from pilot107.services.agent_session_service import AgentSessionService
from pilot107.services.agent_task_service import AgentTaskService
from pilot107.worker.evidence import EvidenceStore


class AgentTaskApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        database = root / "pilot107.db"
        control = SQLiteControlRepository(database)
        session_store = SQLiteAgentSessionStore(database)
        session_service = AgentSessionService(
            store=session_store,
            control_repository=control,
        )
        session, _ = session_service.create_session(
            owner="alice",
            request_key="session-1",
            model_profile_id="faux-default",
            source={"run_id": "run-1"},
        )
        other_session, _ = session_service.create_session(
            owner="alice",
            request_key="session-2",
            model_profile_id="faux-default",
            source={"run_id": "run-2"},
        )
        self.session_id = session.session_id
        self.task_store = SQLiteAgentTaskStore(database)
        run_store = RunStore(database)
        run_service = RunService(
            store=run_store,
            backend=InMemorySlurmBackend(),
            control_repository=control,
        )
        self.service = AgentTaskService(
            store=self.task_store,
            session_store=session_store,
            session_service=session_service,
            run_service=run_service,
            control_repository=control,
            workspace_resolver=lambda owner, workspace_id, digest: root,
            worker_id="api-test-worker",
        )
        task, _ = self.task_store.create_task(
            owner="alice",
            session_id=session.session_id,
            turn_id="turn-1",
            project_id="project-1",
            workspace_id="workspace-1",
            task_kind="slurm_validation",
            request_key="validation-1",
            request=_request(),
            envelope=_envelope(),
        )
        other, _ = self.task_store.create_task(
            owner="alice",
            session_id=other_session.session_id,
            turn_id="turn-2",
            project_id="project-1",
            workspace_id="workspace-1",
            task_kind="slurm_validation",
            request_key="validation-2",
            request=_request(),
            envelope=_envelope(),
        )
        self.task_id = task.task_id
        self.task_version = task.version
        self.other_task_id = other.task_id
        self.api = Pilot107HttpApi(
            store=run_store,
            evidence_query=EvidenceQueryService(
                store=run_store,
                evidence_store=EvidenceStore(root / "evidence"),
            ),
            agent_task_service=self.service,
            auth_required=True,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def headers(user: str) -> dict[str, str]:
        return {"X-Pilot107-User": user}

    def test_list_tasks_is_scoped_to_session_and_owner(self) -> None:
        response = self.api.handle_get(
            f"/api/v1/agent-sessions/{self.session_id}/tasks",
            headers=self.headers("alice"),
        )
        bob = self.api.handle_get(
            f"/api/v1/agent-sessions/{self.session_id}/tasks",
            headers=self.headers("bob"),
        )
        missing = self.api.handle_get(
            "/api/v1/agent-sessions/session-missing/tasks",
            headers=self.headers("alice"),
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(
            [item["task_id"] for item in response.payload["items"]],
            [self.task_id],
        )
        self.assertNotIn(self.other_task_id, repr(response.payload))
        self.assertEqual(bob.status, 404)
        self.assertEqual(missing.status, 404)

    def test_get_task_is_owner_scoped_and_missing_is_not_found(self) -> None:
        found = self.api.handle_get(
            f"/api/v1/agent-tasks/{self.task_id}",
            headers=self.headers("alice"),
        )
        bob = self.api.handle_get(
            f"/api/v1/agent-tasks/{self.task_id}",
            headers=self.headers("bob"),
        )
        missing = self.api.handle_get(
            "/api/v1/agent-tasks/task-missing",
            headers=self.headers("alice"),
        )

        self.assertEqual(found.status, 200)
        self.assertEqual(found.payload["task_id"], self.task_id)
        self.assertFalse(found.payload["cancel_requested"])
        self.assertEqual(bob.status, 404)
        self.assertEqual(missing.status, 404)

    def test_cancel_requires_expected_version_and_propagates(self) -> None:
        path = f"/api/v1/agent-tasks/{self.task_id}/cancel"
        stale = self.api.handle_post(
            path,
            body=_json({"expected_version": self.task_version + 1}),
            headers=self.headers("alice"),
        )
        bob = self.api.handle_post(
            path,
            body=_json({"expected_version": self.task_version}),
            headers=self.headers("bob"),
        )
        missing = self.api.handle_post(
            "/api/v1/agent-tasks/task-missing/cancel",
            body=_json({"expected_version": 0}),
            headers=self.headers("alice"),
        )
        cancelled = self.api.handle_post(
            path,
            body=_json({"expected_version": self.task_version}),
            headers=self.headers("alice"),
        )

        self.assertEqual(stale.status, 409)
        self.assertEqual(bob.status, 404)
        self.assertEqual(missing.status, 404)
        self.assertEqual(cancelled.status, 200)
        self.assertTrue(cancelled.payload["cancel_requested"])
        self.assertEqual(cancelled.payload["state"], "cancelled")


def _request() -> AgentTaskRequest:
    return AgentTaskRequest(
        partition="debug",
        qos="normal",
        cpus=1,
        memory_mib=1024,
        gpu_type=None,
        gpus=0,
        walltime_seconds=300,
        tasks=1,
        submissions=1,
        workspace_snapshot_digest="a" * 64,
        payload={"script": "true\n", "job_name": "validation"},
    )


def _envelope() -> AgentResourceEnvelope:
    return AgentResourceEnvelope(
        partition="debug",
        qos="normal",
        cpus=1,
        memory_mib=1024,
        gpu_type=None,
        gpus=0,
        walltime_seconds=300,
        max_tasks=1,
        max_submissions=1,
        workspace_snapshot_digest="a" * 64,
        expires_at="2027-08-19T01:00:00Z",
        approved_by="alice",
    )


def _json(value: object) -> bytes:
    return json.dumps(value).encode()
