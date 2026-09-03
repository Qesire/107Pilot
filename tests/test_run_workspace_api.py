import tempfile
import unittest
from pathlib import Path

from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.http_app import Pilot107HttpApi
from pilot107.api.run_workspace_routes import RunWorkspaceRoutes
from pilot107.core.contracts import ContractStore
from pilot107.core.identity import UserIdentity
from pilot107.core.run_store import RunStore
from pilot107.services.run_workspace_service import RunWorkspaceService
from pilot107.worker.evidence import EvidenceStore


class RunWorkspaceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = RunStore(root / "pilot107.db")
        self.contract_store = ContractStore(root / "pilot107.db")
        self.evidence_store = EvidenceStore(root / "evidence")
        self.run = self.store.create_run(
            run_id="run_workspace_api",
            owner="alice",
            workdir="/public/home/alice/project",
            script="true",
            job_name="workspace-api",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_http_api_exposes_workspace_projection(self) -> None:
        api = Pilot107HttpApi(
            store=self.store,
            evidence_query=EvidenceQueryService(
                store=self.store,
                evidence_store=self.evidence_store,
            ),
            contract_store=self.contract_store,
        )

        response = api.handle_get(f"/api/v1/runs/{self.run.run_id}/workspace")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["run"]["run_id"], self.run.run_id)
        self.assertEqual(response.payload["states"]["execution"], "DRAFT")
        self.assertEqual(response.payload["next_action"]["kind"], "watch_queue")
        self.assertEqual(response.payload["evidence_summary"]["object_count"], 0)

    def test_route_rejects_cross_owner_access(self) -> None:
        routes = RunWorkspaceRoutes(
            RunWorkspaceService(store=self.store, contract_store=self.contract_store)
        )

        response = routes.handle_get(
            ["runs", self.run.run_id, "workspace"],
            params={},
            identity=UserIdentity(username="bob"),
        )

        self.assertIsNotNone(response)
        assert response is not None
        self.assertEqual(response.status, 403)
        self.assertEqual(response.payload["error"]["code"], "RUN_WORKSPACE.FORBIDDEN")

    def test_workspace_route_rejects_unknown_query_parameters(self) -> None:
        routes = RunWorkspaceRoutes(RunWorkspaceService(store=self.store))

        response = routes.handle_get(
            ["runs", self.run.run_id, "workspace"],
            params={"deep": ["1"]},
            identity=None,
        )

        self.assertIsNotNone(response)
        assert response is not None
        self.assertEqual(response.status, 400)
        self.assertEqual(response.payload["error"]["code"], "RUN_WORKSPACE.INVALID_QUERY")


if __name__ == "__main__":
    unittest.main()
