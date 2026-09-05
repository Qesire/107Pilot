import json
import tempfile
import unittest
from pathlib import Path

from pilot107.agent.market_sessions import (
    MarketApplicationService,
    SQLiteMarketSessionStore,
)
from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.http_app import Pilot107HttpApi
from pilot107.core.contracts import ContractService, ContractStore, RecipeCatalog
from pilot107.core.run_publications import RunPublicationStore
from pilot107.core.run_store import RunStore
from pilot107.core.template_market import TemplateMarketStore
from pilot107.core.template_policy import (
    TemplatePublicationGate,
    TemplateRoleDirectory,
)
from pilot107.worker.evidence import EvidenceStore


class TemplateApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        db_path = root / "pilot107.db"
        run_store = RunStore(db_path)
        contract_service = ContractService(
            catalog=RecipeCatalog(),
            store=ContractStore(db_path),
        )
        template_store = TemplateMarketStore(
            db_path,
            publication_gate=TemplatePublicationGate(contract_service),
            contract_service=contract_service,
        )
        market_application_service = MarketApplicationService(
            store=SQLiteMarketSessionStore(db_path),
            contract_service=contract_service,
            run_publications=RunPublicationStore(
                db_path,
                run_store=run_store,
                contract_service=contract_service,
            ),
            template_market=template_store,
            project_service=None,
        )
        self.api = Pilot107HttpApi(
            store=run_store,
            evidence_query=EvidenceQueryService(
                store=run_store,
                evidence_store=EvidenceStore(root / "evidence"),
            ),
            contract_service=contract_service,
            template_market_store=template_store,
            market_application_service=market_application_service,
            template_role_directory=TemplateRoleDirectory(
                reviewers=frozenset({"reviewer"}),
                admins=frozenset({"admin"}),
                course_instructors={"course-107": frozenset({"teacher"})},
                course_tas={"course-107": frozenset({"ta"})},
                course_members={"course-107": frozenset({"bob"})},
            ),
            auth_required=True,
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_public_release_workflow_is_owner_scoped_reviewed_and_idempotent(self) -> None:
        created = self._create_draft(visibility="public")
        draft_id = created.payload["draft_id"]

        cross_owner = self.api.handle_get(
            f"/api/v1/template-drafts/{draft_id}",
            headers=self._headers("bob"),
        )
        validated = self.api.handle_post(
            f"/api/v1/template-drafts/{draft_id}/validate",
            body=b"{}",
            headers=self._headers("alice"),
        )
        updated = self.api.handle_patch(
            f"/api/v1/template-drafts/{draft_id}",
            body=_json({"expected_version": 1, "description": "updated"}),
            headers=self._headers("alice"),
        )
        stale = self.api.handle_patch(
            f"/api/v1/template-drafts/{draft_id}",
            body=_json({"expected_version": 1, "description": "stale"}),
            headers=self._headers("alice"),
        )
        submitted = self.api.handle_post(
            f"/api/v1/template-drafts/{draft_id}/reviews",
            body=_json({"expected_version": 2}),
            headers=self._headers("alice"),
        )
        review_id = submitted.payload["review_id"]
        reviewer_queue = self.api.handle_get(
            "/api/v1/template-reviews",
            headers=self._headers("reviewer"),
        )
        ordinary_queue = self.api.handle_get(
            "/api/v1/template-reviews",
            headers=self._headers("bob"),
        )
        decided = self.api.handle_post(
            f"/api/v1/template-reviews/{review_id}/decision",
            body=_json({"expected_version": 1, "approve": True, "note": "safe"}),
            headers=self._headers("reviewer"),
        )
        publish_body = {
            "review_id": review_id,
            "release_version": "1.0.0",
            "request_key": "publish-public-v1",
        }
        published = self.api.handle_post(
            f"/api/v1/template-drafts/{draft_id}/publish",
            body=_json(publish_body),
            headers=self._headers("alice"),
        )
        repeated = self.api.handle_post(
            f"/api/v1/template-drafts/{draft_id}/publish",
            body=_json(publish_body),
            headers=self._headers("alice"),
        )
        template_id = published.payload["template_id"]
        fetched = self.api.handle_get(
            f"/api/v1/templates/{template_id}/releases/1.0.0",
            headers=self._headers("bob"),
        )
        unified_market = self.api.handle_get(
            "/api/v1/market/items?kind=curated_template",
            headers=self._headers("bob"),
        )
        unified_detail = self.api.handle_get(
            f"/api/v1/market/items/{published.payload['release_id']}",
            headers=self._headers("bob"),
        )
        legacy_adopt = self.api.handle_post(
            f"/api/v1/market/items/{published.payload['release_id']}/adopt",
            body=_json({"request_key": "bob-adopts-public"}),
            headers=self._headers("bob"),
        )
        legacy_template_adopt = self.api.handle_post(
            f"/api/v1/templates/{template_id}/releases/1.0.0/adopt",
            body=_json({"request_key": "bob-adopts-public"}),
            headers=self._headers("bob"),
        )
        started_application = self.api.handle_post(
            "/api/v1/market/applications",
            body=_json(
                {
                    "source_kind": "curated_template",
                    "source_item_id": published.payload["release_id"],
                    "user_intent": "instantiate the reviewed template privately",
                    "request_key": "bob-curated-application",
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
                    "request_key": "bob-adopts-public",
                }
            ),
            headers=self._headers("bob"),
        )
        adopted_again = self.api.handle_post(
            f"/api/v1/market/applications/{started_application.payload['session_id']}/confirmation",
            body=_json(
                {
                    "expected_version": adopted.payload["version"],
                    "confirmation_digest": started_application.payload[
                        "confirmation_digest"
                    ],
                    "request_key": "bob-adopts-public",
                }
            ),
            headers=self._headers("bob"),
        )
        withdrawn = self.api.handle_post(
            f"/api/v1/templates/{template_id}/releases/1.0.0/withdraw",
            body=_json({"reason": "superseded by a safer release"}),
            headers=self._headers("alice"),
        )
        withdrawn_detail = self.api.handle_get(
            f"/api/v1/templates/{template_id}/releases/1.0.0",
            headers=self._headers("bob"),
        )
        blocked_adoption = self.api.handle_post(
            f"/api/v1/templates/{template_id}/releases/1.0.0/adopt",
            body=_json({"request_key": "carol-after-withdrawal"}),
            headers=self._headers("carol"),
        )
        retried_after_withdrawal = self.api.handle_post(
            f"/api/v1/templates/{template_id}/releases/1.0.0/adopt",
            body=_json({"request_key": "bob-adopts-public"}),
            headers=self._headers("bob"),
        )

        self.assertEqual(created.status, 201)
        self.assertEqual(cross_owner.status, 404)
        self.assertEqual(validated.payload["status"], "OK")
        self.assertEqual(updated.payload["version"], 2)
        self.assertEqual(stale.status, 409)
        self.assertEqual(submitted.status, 201)
        self.assertEqual(reviewer_queue.payload["items"][0]["review_id"], review_id)
        self.assertEqual(ordinary_queue.payload["items"], [])
        self.assertEqual(decided.payload["reviewer_role"], "reviewer")
        self.assertEqual(published.status, 201)
        self.assertEqual(repeated.payload["release_id"], published.payload["release_id"])
        self.assertEqual(fetched.status, 200)
        self.assertEqual(unified_market.status, 200)
        self.assertEqual(
            unified_market.payload["items"][0]["kind"],
            "curated_template",
        )
        self.assertEqual(
            unified_market.payload["items"][0]["item_id"],
            published.payload["release_id"],
        )
        self.assertEqual(
            unified_detail.payload["template"]["template_id"],
            template_id,
        )
        self.assertIn("metrics", unified_detail.payload)
        self.assertEqual(legacy_adopt.status, 409)
        self.assertEqual(
            legacy_adopt.payload["error"]["code"],
            "MARKET.AGENT_APPLICATION_REQUIRED",
        )
        self.assertEqual(legacy_template_adopt.status, 409)
        self.assertEqual(adopted.status, 200)
        self.assertEqual(adopted_again.payload["adoption_id"], adopted.payload["adoption_id"])
        self.assertTrue(adopted.payload["target_contract_id"].startswith("contract_adopted_"))
        adopted_contract = self.api.handle_get(
            f"/api/v1/contracts/{adopted.payload['target_contract_id']}",
            headers=self._headers("bob"),
        )
        self.assertEqual(adopted_contract.status, 200)
        self.assertEqual(adopted_contract.payload["derivation_reason"], "template_application")
        self.assertEqual(withdrawn.status, 200)
        self.assertEqual(withdrawn_detail.payload["withdrawal_actor"], "alice")
        self.assertEqual(
            withdrawn_detail.payload["withdrawal_reason"],
            "superseded by a safer release",
        )
        self.assertEqual(blocked_adoption.status, 409)
        self.assertEqual(retried_after_withdrawal.status, 409)

    def test_client_cannot_self_assert_reviewer_role_or_course_scope(self) -> None:
        public = self._create_draft(visibility="public")
        public_review = self._submit_review(public.payload["draft_id"])
        forged_role = self.api.handle_post(
            f"/api/v1/template-reviews/{public_review}/decision",
            body=_json({"expected_version": 1, "approve": True}),
            headers={
                **self._headers("mallory"),
                "X-Pilot107-Template-Role": "admin",
            },
        )

        course = self._create_draft(visibility="course", scope_key="course-107")
        course_review = self._submit_review(course.payload["draft_id"])
        forged_scope = self.api.handle_post(
            f"/api/v1/template-reviews/{course_review}/decision",
            body=_json({"expected_version": 1, "approve": True}),
            headers={
                **self._headers("mallory"),
                "X-Pilot107-Course-Scopes": "course-107",
            },
        )
        reviewer_queue = self.api.handle_get(
            "/api/v1/template-reviews",
            headers=self._headers("reviewer"),
        )
        teacher_queue = self.api.handle_get(
            "/api/v1/template-reviews",
            headers=self._headers("teacher"),
        )
        legitimate = self.api.handle_post(
            f"/api/v1/template-reviews/{course_review}/decision",
            body=_json({"expected_version": 1, "approve": True}),
            headers=self._headers("teacher"),
        )

        self.assertEqual(forged_role.status, 403)
        self.assertEqual(forged_scope.status, 403)
        self.assertEqual(
            {item["review_id"] for item in reviewer_queue.payload["items"]},
            {public_review},
        )
        self.assertEqual(
            {item["review_id"] for item in teacher_queue.payload["items"]},
            {course_review},
        )
        self.assertEqual(legitimate.status, 200)
        self.assertEqual(legitimate.payload["reviewer_scope_key"], "course-107")

    def test_course_release_visibility_uses_server_side_membership(self) -> None:
        created = self._create_draft(visibility="course", scope_key="course-107")
        draft_id = created.payload["draft_id"]
        review_id = self._submit_review(draft_id)
        self.api.handle_post(
            f"/api/v1/template-reviews/{review_id}/decision",
            body=_json({"expected_version": 1, "approve": True}),
            headers=self._headers("ta"),
        )
        published = self.api.handle_post(
            f"/api/v1/template-drafts/{draft_id}/publish",
            body=_json(
                {
                    "review_id": review_id,
                    "release_version": "1.0.0",
                    "request_key": "publish-course-v1",
                }
            ),
            headers=self._headers("alice"),
        )
        target = (
            f"/api/v1/templates/{published.payload['template_id']}/releases/1.0.0"
        )

        denied = self.api.handle_get(target, headers=self._headers("carol"))
        visible = self.api.handle_get(target, headers=self._headers("bob"))
        publisher_visible = self.api.handle_get(target, headers=self._headers("alice"))
        admin_visible = self.api.handle_get(target, headers=self._headers("admin"))
        member_market = self.api.handle_get(
            "/api/v1/templates?visibility=course&partition=debug&gpu=false",
            headers=self._headers("bob"),
        )
        outsider_market = self.api.handle_get(
            "/api/v1/templates?visibility=course",
            headers=self._headers("carol"),
        )

        self.assertEqual(denied.status, 403)
        self.assertEqual(visible.status, 200)
        self.assertEqual(publisher_visible.status, 200)
        self.assertEqual(admin_visible.status, 200)
        self.assertEqual(
            member_market.payload["items"][0]["release_id"],
            published.payload["release_id"],
        )
        self.assertEqual(outsider_market.payload["items"], [])

    def test_template_requests_reject_unknown_role_fields(self) -> None:
        payload = self._draft_body(visibility="public")
        payload["roles"] = ["admin"]

        response = self.api.handle_post(
            "/api/v1/template-drafts",
            body=_json(payload),
            headers=self._headers("alice"),
        )

        self.assertEqual(response.status, 400)
        self.assertEqual(response.payload["error"]["code"], "TEMPLATE.INVALID_REQUEST")

    def test_draft_list_uses_owner_bound_keyset_cursor(self) -> None:
        created_ids = {
            self._create_draft(visibility="private").payload["draft_id"]
            for _ in range(3)
        }

        first = self.api.handle_get(
            "/api/v1/template-drafts?limit=1",
            headers=self._headers("alice"),
        )
        second = self.api.handle_get(
            "/api/v1/template-drafts?limit=2&cursor="
            + first.payload["page"]["next_cursor"],
            headers=self._headers("alice"),
        )
        cross_owner = self.api.handle_get(
            "/api/v1/template-drafts?limit=1&cursor="
            + first.payload["page"]["next_cursor"],
            headers=self._headers("bob"),
        )

        listed_ids = {
            first.payload["items"][0]["draft_id"],
            *(item["draft_id"] for item in second.payload["items"]),
        }
        self.assertTrue(first.payload["page"]["has_more"])
        self.assertEqual(listed_ids, created_ids)
        self.assertEqual(cross_owner.status, 400)

    def test_release_mutations_require_resolved_identity_not_body_actor(self) -> None:
        self.api.auth_required = False

        response = self.api.handle_post(
            "/api/v1/templates/template_missing/releases/1.0.0/adopt",
            body=_json({"request_key": "forged", "actor": "bob"}),
        )

        self.assertEqual(response.status, 401)
        self.assertEqual(response.payload["error"]["code"], "AUTH.MISSING")

    def test_published_draft_can_create_a_new_release_and_diff(self) -> None:
        created = self._create_draft(visibility="public")
        draft_id = created.payload["draft_id"]
        first_review = self._submit_review(draft_id)
        self.api.handle_post(
            f"/api/v1/template-reviews/{first_review}/decision",
            body=_json({"expected_version": 1, "approve": True}),
            headers=self._headers("reviewer"),
        )
        first = self.api.handle_post(
            f"/api/v1/template-drafts/{draft_id}/publish",
            body=_json(
                {
                    "review_id": first_review,
                    "release_version": "1.0.0",
                    "request_key": "publish-diff-v1",
                }
            ),
            headers=self._headers("alice"),
        )
        revised = self.api.handle_patch(
            f"/api/v1/template-drafts/{draft_id}",
            body=_json({"expected_version": 1, "title": "Python assignment v2"}),
            headers=self._headers("alice"),
        )
        second_review = self.api.handle_post(
            f"/api/v1/template-drafts/{draft_id}/reviews",
            body=_json({"expected_version": 2}),
            headers=self._headers("alice"),
        )
        self.api.handle_post(
            f"/api/v1/template-reviews/{second_review.payload['review_id']}/decision",
            body=_json({"expected_version": 1, "approve": True}),
            headers=self._headers("reviewer"),
        )
        second = self.api.handle_post(
            f"/api/v1/template-drafts/{draft_id}/publish",
            body=_json(
                {
                    "review_id": second_review.payload["review_id"],
                    "release_version": "1.1.0",
                    "request_key": "publish-diff-v2",
                }
            ),
            headers=self._headers("alice"),
        )
        diff = self.api.handle_get(
            f"/api/v1/templates/{first.payload['template_id']}/diff"
            "?from=1.0.0&to=1.1.0",
            headers=self._headers("bob"),
        )
        first_again = self.api.handle_get(
            f"/api/v1/templates/{first.payload['template_id']}/releases/1.0.0",
            headers=self._headers("bob"),
        )

        self.assertEqual(revised.status, 200)
        self.assertEqual(revised.payload["version"], 2)
        self.assertEqual(second.status, 201)
        self.assertEqual(diff.status, 200)
        self.assertEqual(
            diff.payload["changes"],
            [
                {
                    "path": "/title",
                    "before": "Python assignment",
                    "after": "Python assignment v2",
                }
            ],
        )
        self.assertEqual(first_again.payload["title"], "Python assignment")

    def _create_draft(self, *, visibility: str, scope_key: str | None = None):
        return self.api.handle_post(
            "/api/v1/template-drafts",
            body=_json(self._draft_body(visibility=visibility, scope_key=scope_key)),
            headers=self._headers("alice"),
        )

    def _submit_review(self, draft_id: str) -> str:
        response = self.api.handle_post(
            f"/api/v1/template-drafts/{draft_id}/reviews",
            body=_json({"expected_version": 1}),
            headers=self._headers("alice"),
        )
        self.assertEqual(response.status, 201)
        return str(response.payload["review_id"])

    def _draft_body(self, *, visibility: str, scope_key: str | None = None) -> dict:
        body = {
            "title": "Python assignment",
            "description": "Reviewed template",
            "visibility": visibility,
            "payload": {
                "recipe_version_id": "recipe_python_cpu@1.0.0",
                "project": {"workdir": "/public/home/alice"},
                "entry": {"command": "echo ok"},
                "resources": {
                    "partition": "debug",
                    "qos": "normal",
                    "nodes": 1,
                    "ntasks": 1,
                    "cpus_per_task": 1,
                    "time_limit": "00:05:00",
                },
                "extensions": {
                    "advanced": {"raw_sbatch": "#SBATCH --exclusive"}
                },
            },
            "compatibility": {"partitions": ["debug"], "gpu": False},
            "publication": {
                "license": "MIT",
                "attribution": "Original work by alice",
                "dataset_access": "No external dataset",
                "risk_statement": "No known elevated risk",
            },
        }
        if scope_key is not None:
            body["scope_key"] = scope_key
        return body

    def _headers(self, username: str) -> dict[str, str]:
        return {"X-Pilot107-User": username}


def _json(payload: dict) -> bytes:
    return json.dumps(payload).encode()


if __name__ == "__main__":
    unittest.main()
