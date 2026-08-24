import json
import tempfile
import unittest
from pathlib import Path

from pilot107.adapters.slurm import InMemorySlurmBackend
from pilot107.agent.market_sessions import (
    MarketApplicationService,
    SQLiteMarketSessionStore,
)
from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.http_app import Pilot107HttpApi
from pilot107.core.contracts import ContractService, ContractStore, RecipeCatalog
from pilot107.core.run_publications import (
    RunPublicationError,
    RunPublicationStore,
    RunPublicationVisibility,
)
from pilot107.core.run_service import RunService
from pilot107.core.run_store import RunStore
from pilot107.worker.evidence import EvidenceStore


class SuccessfulRunPublicationTests(unittest.TestCase):
    """The source Run is executed through the normal in-memory Slurm backend.

    The in-memory backend is a test fixture for the same SlurmBackend contract;
    publishing and adoption do not receive simulator-specific flags or branches.
    """

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self.db_path = root / "pilot107.db"
        self.run_store = RunStore(self.db_path)
        self.contract_service = ContractService(
            catalog=RecipeCatalog(),
            store=ContractStore(self.db_path),
        )
        self.backend = InMemorySlurmBackend()
        self.run_service = RunService(store=self.run_store, backend=self.backend)
        self.publications = RunPublicationStore(
            self.db_path,
            run_store=self.run_store,
            contract_service=self.contract_service,
        )
        market_application_service = MarketApplicationService(
            store=SQLiteMarketSessionStore(self.db_path),
            contract_service=self.contract_service,
            run_publications=self.publications,
            template_market=None,
            project_service=None,
        )
        self.api = Pilot107HttpApi(
            store=self.run_store,
            evidence_query=EvidenceQueryService(
                store=self.run_store,
                evidence_store=EvidenceStore(root / "evidence"),
            ),
            run_service=self.run_service,
            contract_service=self.contract_service,
            run_publication_store=self.publications,
            market_application_service=market_application_service,
            auth_required=True,
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_simulated_successful_run_can_be_confirmed_published_and_adopted(self) -> None:
        run = self._successful_contract_run(owner="alice")
        eligible = self.api.handle_get(
            f"/api/v1/runs/{run.run_id}",
            headers=self._headers("alice"),
        )

        published = self.api.handle_post(
            f"/api/v1/runs/{run.run_id}/publish",
            body=_json(
                {
                    "request_key": "alice-shares-preprocess",
                    "title": "预处理与训练",
                    "description": "演示成功运行；路径和数据访问由采用者自行确认。",
                    "visibility": "campus",
                    "tags": ["ml", "demo"],
                    "reproduction_note": "请在采用后修改项目工作目录。",
                    "confirm_share": True,
                    "share_manifest": {
                        "description": True,
                        "resource_summary": False,
                        "result_summary": True,
                        "contract_for_adaptation": True,
                        "script": False,
                        "evidence_previews": False,
                        "small_assets": [],
                    },
                }
            ),
            headers=self._headers("alice"),
        )
        market = self.api.handle_get("/api/v1/market", headers=self._headers("bob"))
        unified_market = self.api.handle_get(
            "/api/v1/market/items?kind=run_publication",
            headers=self._headers("bob"),
        )
        detailed = self.api.handle_get(
            f"/api/v1/market/{published.payload['publication_id']}",
            headers=self._headers("bob"),
        )
        unified_detail = self.api.handle_get(
            f"/api/v1/market/items/{published.payload['publication_id']}",
            headers=self._headers("bob"),
        )
        legacy_adopt = self.api.handle_post(
            f"/api/v1/market/items/{published.payload['publication_id']}/adopt",
            body=_json({"request_key": "bob-adopts-preprocess"}),
            headers=self._headers("bob"),
        )
        started_application = self.api.handle_post(
            "/api/v1/market/applications",
            body=_json(
                {
                    "source_kind": "run_publication",
                    "source_item_id": published.payload["publication_id"],
                    "user_intent": "adapt the shared Contract into Bob's workspace",
                    "request_key": "bob-reference-application",
                }
            ),
            headers=self._headers("bob"),
        )
        adopted = self.api.handle_post(
            f"/api/v1/market/applications/{started_application.payload['session_id']}/confirmation",
            body=_json(
                {
                    "expected_version": started_application.payload["version"],
                    "confirmation_digest": started_application.payload[
                        "confirmation_digest"
                    ],
                    "request_key": "bob-adopts-preprocess",
                }
            ),
            headers=self._headers("bob"),
        )
        published_run = self.api.handle_get(
            f"/api/v1/runs/{run.run_id}",
            headers=self._headers("alice"),
        )

        self.assertEqual(eligible.payload["publication"]["status"], "eligible")
        self.assertEqual(published.status, 201)
        self.assertEqual(market.status, 200)
        self.assertEqual(market.payload["items"][0]["kind"], "successful_run")
        self.assertEqual(market.payload["items"][0]["source_run_id"], run.run_id)
        self.assertEqual(
            unified_market.payload["items"][0]["kind"],
            "run_publication",
        )
        self.assertEqual(
            unified_market.payload["items"][0]["item_id"],
            published.payload["publication_id"],
        )
        self.assertEqual(
            unified_detail.payload["source"]["run_id"],
            run.run_id,
        )
        self.assertNotIn("script", detailed.payload)
        self.assertNotIn("workdir", detailed.payload)
        self.assertNotIn("source_contract_id", detailed.payload)
        self.assertNotIn("workdir", unified_detail.payload)
        self.assertNotIn("contract_payload", unified_detail.payload)
        self.assertTrue(
            unified_detail.payload["share_manifest"]["contract_for_adaptation"]
        )
        self.assertEqual(
            unified_detail.payload["share_manifest_digest"],
            published.payload["share_manifest_digest"],
        )
        self.assertEqual(
            unified_detail.payload["shared"]["result_summary"]["state"],
            "SUCCEEDED",
        )
        self.assertEqual(published_run.payload["publication"]["status"], "published")
        self.assertEqual(
            published_run.payload["publication"]["publication_id"],
            published.payload["publication_id"],
        )
        self.assertEqual(legacy_adopt.status, 409)
        self.assertEqual(
            legacy_adopt.payload["error"]["code"],
            "MARKET.AGENT_APPLICATION_REQUIRED",
        )
        self.assertEqual(adopted.status, 200)
        adopted_contract = self.contract_service.get(adopted.payload["target_contract_id"])
        self.assertEqual(adopted_contract.owner, "bob")
        self.assertEqual(adopted_contract.derivation_reason, "run_publication_adaptation")
        self.assertEqual(adopted_contract.parent_contract_id, run.contract_id)
        self.assertEqual(
            adopted_contract.payload["project"]["workdir"],
            "/public/home/bob/market-demo",
        )
        self.assertEqual(
            adopted_contract.field_sources[0]["source_publication_id"],
            published.payload["publication_id"],
        )
        self.assertEqual(
            [event.event_type for event in self.run_store.list_events(run.run_id)][-2:],
            ["market.run_published", "market.run_adopted"],
        )

    def test_publish_requires_owner_confirmation_and_a_zero_exit_success(self) -> None:
        successful = self._successful_contract_run(owner="alice")
        with self.assertRaisesRegex(RunPublicationError, "owner confirmation"):
            self.publications.publish(
                source_run_id=successful.run_id,
                owner="alice",
                title="not confirmed",
                description="",
                visibility=RunPublicationVisibility.CAMPUS,
                scope_key=None,
                request_key="without-confirmation",
                confirmed=False,
            )

    def test_rejects_unsuccessful_run_and_hides_withdrawn_item(self) -> None:
        pending_contract = self.contract_service.create(
            owner="alice",
            payload=_contract_payload(owner="alice"),
        )
        pending_run = self.run_service.submit(
            self.contract_service.to_submit_request(pending_contract)
        )
        pending_payload = self.api.handle_get(
            f"/api/v1/runs/{pending_run.run_id}",
            headers=self._headers("alice"),
        )
        self.assertEqual(
            pending_payload.payload["publication"],
            {
                "status": "ineligible",
                "reason": "run_not_succeeded",
                "publication_id": None,
            },
        )
        with self.assertRaisesRegex(RunPublicationError, "only a succeeded Run"):
            self.publications.publish(
                source_run_id=pending_run.run_id,
                owner="alice",
                title="too early",
                description="",
                visibility=RunPublicationVisibility.PUBLIC,
                scope_key=None,
                request_key="pending-run",
                confirmed=True,
            )

        successful = self._successful_contract_run(owner="alice")
        publication = self.publications.publish(
            source_run_id=successful.run_id,
            owner="alice",
            title="撤回演示",
            description="",
            visibility=RunPublicationVisibility.PUBLIC,
            scope_key=None,
            request_key="withdraw-demo",
            confirmed=True,
        )
        withdrawn = self.api.handle_post(
            f"/api/v1/market/{publication.publication_id}/withdraw",
            body=_json({"reason": "the input data was removed"}),
            headers=self._headers("alice"),
        )
        hidden = self.api.handle_get(
            f"/api/v1/market/{publication.publication_id}",
            headers=self._headers("bob"),
        )

        self.assertEqual(withdrawn.status, 200)
        self.assertEqual(hidden.status, 404)

    def _successful_contract_run(self, *, owner: str):
        contract = self.contract_service.create(owner=owner, payload=_contract_payload(owner=owner))
        submitted = self.run_service.submit(self.contract_service.to_submit_request(contract))
        self.backend.advance_job(
            job_id=submitted.job_id or "",
            raw_state="COMPLETED",
            exit_code="0:0",
        )
        return self.run_service.reconcile_once(submitted.run_id)

    @staticmethod
    def _headers(username: str) -> dict[str, str]:
        return {"X-Pilot107-User": username}


def _contract_payload(*, owner: str) -> dict:
    return {
        "recipe_version_id": "recipe_python_cpu@1.0.0",
        "project": {
            "name": "simulated-market-run",
            "workdir": f"/public/home/{owner}/market-demo",
        },
        "entry": {"command": "python -c 'print(\"market success\")'"},
        "resources": {
            "partition": "debug",
            "qos": "normal",
            "nodes": 1,
            "ntasks": 1,
            "cpus_per_task": 1,
            "time_limit": "00:05:00",
        },
    }


def _json(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
