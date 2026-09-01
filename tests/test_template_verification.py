import json
import tempfile
import unittest
from contextlib import suppress
from pathlib import Path
from typing import Any

from pilot107.adapters.slurm import JobSnapshot, SubmissionStrategy, SubmitReceipt
from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.http_app import Pilot107HttpApi
from pilot107.core.contracts import ContractService, ContractStore, RecipeCatalog
from pilot107.core.evidence_binding import EvidenceBinder
from pilot107.core.run_store import RunStore
from pilot107.core.states import RunState
from pilot107.core.template_market import (
    TemplateMarketError,
    TemplateMarketStore,
    TemplateReleaseRecord,
    TemplateVisibility,
)
from pilot107.core.template_policy import (
    TemplatePublicationGate,
    TemplateReviewerPrincipal,
    TemplateReviewerRole,
)
from pilot107.core.template_verification import TemplateVerificationService
from pilot107.worker.capsule import RawCapsuleService
from pilot107.worker.evidence import EvidenceStore


class TemplateVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self.db_path = root / "pilot107.db"
        self.evidence_store = EvidenceStore(root / "evidence")
        self.capsule_root = root / "capsules"
        self.run_store = RunStore(self.db_path)
        self.contract_service = ContractService(
            catalog=RecipeCatalog(),
            store=ContractStore(self.db_path),
        )
        self.template_store = TemplateMarketStore(
            self.db_path,
            publication_gate=TemplatePublicationGate(self.contract_service),
            contract_service=self.contract_service,
        )
        self.verification_service = TemplateVerificationService(
            template_store=self.template_store,
            run_store=self.run_store,
            environment="docker",
            capsule_root=self.capsule_root,
        )
        self.api = Pilot107HttpApi(
            store=self.run_store,
            evidence_query=EvidenceQueryService(
                store=self.run_store,
                evidence_store=self.evidence_store,
            ),
            contract_service=self.contract_service,
            template_market_store=self.template_store,
            template_verification_service=self.verification_service,
            auth_required=True,
        )

    def tearDown(self) -> None:
        for path in sorted(
            Path(self._temporary.name).rglob("*"),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            if path.is_symlink():
                continue
            with suppress(FileNotFoundError):
                path.chmod(0o700 if path.is_dir() else 0o600)
        self._temporary.cleanup()

    def test_verification_is_derived_from_adoption_run_and_final_evidence(self) -> None:
        release = self._release()
        adoption = self.template_store.adopt_release(
            release.release_id,
            adopter="bob",
            request_key="bob-adoption",
        )
        run_id = self._terminal_run(contract_id=str(adoption.target_contract_id))

        verification = self.verification_service.verify_from_run(
            release_id=release.release_id,
            run_id=run_id,
            actor="bob",
            request_key="verify-run",
        )
        repeated = self.verification_service.verify_from_run(
            release_id=release.release_id,
            run_id=run_id,
            actor="bob",
            request_key="verify-run",
        )

        self.assertEqual(verification.status, "passed")
        self.assertEqual(verification.environment, "docker")
        self.assertEqual(len(str(verification.evidence_sha256)), 64)
        self.assertEqual(verification.verification_id, repeated.verification_id)
        self.assertEqual(verification.detail["adoption_id"], adoption.adoption_id)
        self.assertEqual(len(verification.detail["capsule_manifest_sha256"]), 64)
        self.assertEqual(
            verification.evidence_ref,
            f"evidence://runs/{run_id}/manifest/manifest.json",
        )

    def test_verification_rejects_wrong_lineage_nonterminal_and_gpu_mismatch(self) -> None:
        release = self._release()
        adoption = self.template_store.adopt_release(
            release.release_id,
            adopter="bob",
            request_key="bob-negative-adoption",
        )
        wrong = self.run_store.create_run(
            run_id="run_wrong_lineage",
            owner="bob",
            workdir="/public/home/bob",
            script="echo ok",
            contract_id="contract_unrelated",
        )
        with self.assertRaises(TemplateMarketError) as lineage:
            self.verification_service.verify_from_run(
                release_id=release.release_id,
                run_id=wrong.run_id,
                actor="bob",
                request_key="wrong-lineage",
            )
        self.assertEqual(lineage.exception.code, "TEMPLATE.VERIFICATION_LINEAGE_INVALID")

        pending = self.run_store.create_run(
            run_id="run_pending",
            owner="bob",
            workdir="/public/home/bob",
            script="echo ok",
            contract_id=adoption.target_contract_id,
        )
        with self.assertRaises(TemplateMarketError) as not_ready:
            self.verification_service.verify_from_run(
                release_id=release.release_id,
                run_id=pending.run_id,
                actor="bob",
                request_key="pending-run",
            )
        self.assertEqual(not_ready.exception.code, "TEMPLATE.VERIFICATION_RUN_NOT_READY")

        no_capsule_run = self._terminal_run(
            contract_id=str(adoption.target_contract_id),
            run_id="run_no_capsule",
            capsule_ready=False,
        )
        with self.assertRaises(TemplateMarketError) as no_capsule:
            self.verification_service.verify_from_run(
                release_id=release.release_id,
                run_id=no_capsule_run,
                actor="bob",
                request_key="capsule-missing",
            )
        self.assertEqual(
            no_capsule.exception.code,
            "TEMPLATE.VERIFICATION_CAPSULE_INCOMPLETE",
        )

        tampered_run = self._terminal_run(
            contract_id=str(adoption.target_contract_id),
            run_id="run_tampered_capsule",
        )
        tampered_path = (
            self.capsule_root / "runs" / tampered_run / "raw" / "slurm" / "accounting.json"
        )
        tampered_path.chmod(0o600)
        tampered_path.write_text("tampered", encoding="utf-8")
        with self.assertRaises(TemplateMarketError) as tampered:
            self.verification_service.verify_from_run(
                release_id=release.release_id,
                run_id=tampered_run,
                actor="bob",
                request_key="capsule-tampered",
            )
        self.assertEqual(
            tampered.exception.code,
            "TEMPLATE.VERIFICATION_CAPSULE_INCOMPLETE",
        )

        run_id = self._terminal_run(
            contract_id=str(adoption.target_contract_id),
            run_id="run_cpu_only",
        )
        gpu_service = TemplateVerificationService(
            template_store=self.template_store,
            run_store=self.run_store,
            environment="real107_gpu",
            capsule_root=self.capsule_root,
        )
        with self.assertRaises(TemplateMarketError) as mismatch:
            gpu_service.verify_from_run(
                release_id=release.release_id,
                run_id=run_id,
                actor="bob",
                request_key="gpu-mismatch",
            )
        self.assertEqual(
            mismatch.exception.code,
            "TEMPLATE.VERIFICATION_ENVIRONMENT_MISMATCH",
        )

    def test_verification_requires_declared_expected_output_evidence(self) -> None:
        release = self._release(expected_output="results/required.txt")
        adoption = self.template_store.adopt_release(
            release.release_id,
            adopter="bob",
            request_key="bob-expected-output-adoption",
        )
        run_id = self._terminal_run(
            contract_id=str(adoption.target_contract_id),
            run_id="run_missing_expected_output",
        )

        with self.assertRaises(TemplateMarketError) as raised:
            self.verification_service.verify_from_run(
                release_id=release.release_id,
                run_id=run_id,
                actor="bob",
                request_key="missing-expected-output",
            )
        self.assertEqual(raised.exception.code, "TEMPLATE.VERIFICATION_EVIDENCE_INCOMPLETE")

    def test_verification_rejects_raw_capsule_symlink_outside_capsule_root(self) -> None:
        release = self._release()
        adoption = self.template_store.adopt_release(
            release.release_id,
            adopter="bob",
            request_key="bob-symlink-adoption",
        )
        run_id = self._terminal_run(
            contract_id=str(adoption.target_contract_id),
            run_id="run_symlink_capsule",
        )
        raw = self.capsule_root / "runs" / run_id / "raw"
        escaped = Path(self._temporary.name) / "escaped-capsule"
        raw.rename(escaped)
        raw.symlink_to(escaped, target_is_directory=True)

        with self.assertRaises(TemplateMarketError) as raised:
            self.verification_service.verify_from_run(
                release_id=release.release_id,
                run_id=run_id,
                actor="bob",
                request_key="symlink-capsule",
            )

        self.assertEqual(
            raised.exception.code,
            "TEMPLATE.VERIFICATION_CAPSULE_INCOMPLETE",
        )

    def test_api_rejects_client_asserted_verification_facts(self) -> None:
        release = self._release()
        adoption = self.template_store.adopt_release(
            release.release_id,
            adopter="bob",
            request_key="bob-api-adoption",
        )
        run_id = self._terminal_run(contract_id=str(adoption.target_contract_id))
        path = f"/api/v1/templates/{release.template_id}/releases/{release.release_version}/verify"

        forged = self.api.handle_post(
            path,
            body=_json(
                {
                    "run_id": run_id,
                    "request_key": "api-forged",
                    "environment": "real107_gpu",
                    "status": "passed",
                }
            ),
            headers={"X-Pilot107-User": "bob"},
        )
        verified = self.api.handle_post(
            path,
            body=_json({"run_id": run_id, "request_key": "api-verified"}),
            headers={"X-Pilot107-User": "bob"},
        )
        listed = self.api.handle_get(
            f"/api/v1/templates/{release.template_id}/releases/"
            f"{release.release_version}/verifications",
            headers={"X-Pilot107-User": "bob"},
        )

        self.assertEqual(forged.status, 400)
        self.assertEqual(verified.status, 201)
        self.assertEqual(verified.payload["environment"], "docker")
        self.assertEqual(verified.payload["status"], "passed")
        self.assertEqual(
            listed.payload["items"][0]["verification_id"],
            verified.payload["verification_id"],
        )

    def _release(self, *, expected_output: str | None = None) -> TemplateReleaseRecord:
        payload: dict[str, Any] = {
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
        }
        if expected_output is not None:
            payload["outputs"] = {
                "expected": [expected_output],
                "success_conditions": ["slurm_exit_code_zero"],
            }
        draft = self.template_store.create_draft(
            owner="alice",
            title="Verification template",
            description="A controlled verification workload",
            visibility=TemplateVisibility.PUBLIC,
            payload=payload,
            compatibility={"partitions": ["debug"], "gpu": False},
            publication={
                "license": "MIT",
                "attribution": "Original work by alice",
                "dataset_access": "No external dataset",
                "risk_statement": "No known elevated risk",
            },
        )
        review = self.template_store.submit_review(
            draft.draft_id,
            owner="alice",
            expected_version=1,
        )
        self.template_store.decide_review(
            review.review_id,
            principal=TemplateReviewerPrincipal(
                actor="reviewer",
                roles=frozenset({TemplateReviewerRole.REVIEWER}),
            ),
            expected_version=1,
            approve=True,
        )
        return self.template_store.publish(
            review.review_id,
            owner="alice",
            release_version="1.0.0",
        )

    def _terminal_run(
        self,
        *,
        contract_id: str,
        run_id: str = "run_verification",
        capsule_ready: bool = True,
    ) -> str:
        run = self.run_store.create_run(
            run_id=run_id,
            owner="bob",
            workdir="/public/home/bob",
            script="echo ok",
            resource_plan={"gpus_total": 0},
            contract_id=contract_id,
            workspace_digest="b" * 64,
            source_revision="workspace-snapshot:sha256:" + "b" * 64,
            platform_snapshot_ref="snapshot:template-verification",
        )
        self.run_store.apply_submit_receipt(
            run.run_id,
            SubmitReceipt(
                job_id="10701",
                run_state=RunState.SUBMITTED,
                strategy=SubmissionStrategy.COMMAND,
            ),
        )
        self.run_store.apply_snapshot(
            run.run_id,
            JobSnapshot(
                job_id="10701",
                owner="bob",
                run_state=RunState.SUCCEEDED,
                raw_state_flags=["COMPLETED"],
                exit_code="0:0",
            ),
        )
        for task in self.run_store.list_due_collection_tasks():
            if task.run_id == run.run_id:
                self.run_store.mark_collection_task_succeeded(task.task_id, payload={})
        finalized_at = "2026-07-16T00:00:00+00:00"
        accounting = self.evidence_store.write_json(
            run_id=run.run_id,
            logical_path="slurm/accounting.json",
            payload={"state": "COMPLETED", "exit_code": "0:0"},
        )
        summary = self.evidence_store.write_json(
            run_id=run.run_id,
            logical_path="derived/result_summary.v1.json",
            payload={"result_status": "COMPLETE"},
        )
        manifest = self.evidence_store.write_json(
            run_id=run.run_id,
            logical_path="manifest/manifest.json",
            payload={
                "schema": "pilot107.evidence_manifest.v1",
                "run_id": run.run_id,
                "owner": run.owner,
                "job_id": "10701",
                "workspace_revision": None,
                "workspace_digest": "b" * 64,
                "legacy_boundary": True,
                "source_revision": "workspace-snapshot:sha256:" + "b" * 64,
                "platform_snapshot_ref": "snapshot:template-verification",
                "artifacts": [
                    {
                        "logical_path": artifact.logical_path,
                        "sha256": artifact.sha256,
                        "size_bytes": artifact.size_bytes,
                        "content_type": artifact.content_type,
                        "evidence_ref": (
                            f"evidence://runs/{run.run_id}/{artifact.logical_path}"
                        ),
                    }
                    for artifact in (accounting, summary)
                ],
                "warnings": [],
            },
        )
        objects = [
            {
                "object_id": f"evidence_{run.run_id}_{index}",
                "category": artifact.logical_path.split("/", 1)[0],
                "logical_path": artifact.logical_path,
                "store_path": str(artifact.path),
                "source_uri": f"evidence://runs/{run.run_id}/{artifact.logical_path}",
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
                "mime_type": artifact.content_type,
                "collection_status": "collected",
                "mutable_during_run": False,
                "finalized_at": finalized_at,
                "workspace_revision": None,
                "workspace_digest": "b" * 64,
                "source_revision": "workspace-snapshot:sha256:" + "b" * 64,
                "platform_snapshot_ref": "snapshot:template-verification",
            }
            for index, artifact in enumerate((manifest, accounting, summary))
        ]
        self.run_store.upsert_evidence_objects(run.run_id, objects)
        refs = tuple(
            f"evidence://runs/{run.run_id}/{artifact.logical_path}"
            for artifact in (manifest, accounting, summary)
        )
        EvidenceBinder(
            store=self.run_store,
            evidence_root=self.evidence_store.root,
        ).seal_terminal_evidence(
            run.run_id,
            refs,
            {
                "workspace_revision": None,
                "workspace_digest": "b" * 64,
                "legacy_boundary": True,
                "source_revision": "workspace-snapshot:sha256:" + "b" * 64,
                "platform_snapshot_ref": "snapshot:template-verification",
            },
        )
        if capsule_ready:
            RawCapsuleService(
                store=self.run_store,
                evidence_store=self.evidence_store,
                capsule_root=self.capsule_root,
            ).build_raw_capsule(run.run_id)
        return run.run_id


def _json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


if __name__ == "__main__":
    unittest.main()
