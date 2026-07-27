import json
import tempfile
import unittest
from pathlib import Path

from pilot107.adapters.slurm import (
    InMemorySlurmBackend,
    JobSnapshot,
    SubmissionStrategy,
    SubmitReceipt,
)
from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.http_app import Pilot107HttpApi
from pilot107.core.agent import AgentCitation, AgentExplainService, LLMExplanation
from pilot107.core.contracts import ContractService, ContractStore, RecipeCatalog
from pilot107.core.evidence_binding import EvidenceBinder
from pilot107.core.proxy_auth import signed_proxy_headers
from pilot107.core.resources import ResourcePlan
from pilot107.core.run_service import RunService, RunSubmitRequest
from pilot107.core.run_store import RunStore
from pilot107.core.states import RunState
from pilot107.worker.capsule import RawCapsuleService
from pilot107.worker.evidence import EvidenceStore


class HttpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.db_path = root / "pilot107.db"
        self.run_store = RunStore(self.db_path)
        self.evidence_store = EvidenceStore(root / "evidence")
        self.backend = InMemorySlurmBackend()
        self.run_service = RunService(store=self.run_store, backend=self.backend)
        self.recipe_catalog = RecipeCatalog()
        self.contract_service = ContractService(
            catalog=self.recipe_catalog,
            store=ContractStore(self.db_path),
        )
        self.api = Pilot107HttpApi(
            store=self.run_store,
            evidence_query=EvidenceQueryService(
                store=self.run_store,
                evidence_store=self.evidence_store,
            ),
            run_service=self.run_service,
            recipe_catalog=self.recipe_catalog,
            contract_service=self.contract_service,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_proxy_signature_guards_forwarded_identity_and_replay(self) -> None:
        secret = b"0123456789abcdef0123456789abcdef"
        api = Pilot107HttpApi(
            store=self.run_store,
            evidence_query=EvidenceQueryService(
                store=self.run_store,
                evidence_store=self.evidence_store,
            ),
            run_service=self.run_service,
            recipe_catalog=self.recipe_catalog,
            contract_service=self.contract_service,
            auth_required=True,
            proxy_hmac_secret=secret,
        )
        target = "/api/v1/recipes"

        self.assertEqual(
            api.handle_get(target, headers={"X-Pilot107-User": "alice"}).payload["error"]["code"],
            "AUTH.PROXY_SIGNATURE_INVALID",
        )
        headers = signed_proxy_headers(
            secret=secret,
            method="GET",
            target=target,
            user="alice",
        )
        self.assertEqual(api.handle_get(target, headers=headers).status, 200)
        self.assertEqual(api.handle_get(target, headers=headers).status, 403)
        self.assertEqual(api.handle_get("/api/v1/health/live").status, 200)

    def _failed_run_with_stderr(self):
        run = self.run_store.create_run(
            run_id="run_diag_http",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\npython train.py\n",
        )
        self.run_store.apply_submit_receipt(
            run.run_id,
            SubmitReceipt(
                job_id="789",
                run_state=run.state,
                strategy=SubmissionStrategy.COMMAND,
                raw_response={"stdout": "789\n"},
            ),
        )
        failed = self.run_store.apply_snapshot(
            run.run_id,
            JobSnapshot(
                job_id="789",
                owner="alice",
                run_state=RunState.FAILED,
                raw_state_flags=["FAILED"],
                exit_code="1:0",
            ),
        )
        artifact = self.evidence_store.write_text(
            run_id=run.run_id,
            logical_path="logs/stderr.tail.txt",
            content="ModuleNotFoundError: No module named 'torch'\n",
            content_type="text/plain",
        )
        self.run_store.upsert_evidence_objects(
            run.run_id,
            [
                {
                    "object_id": "ev_http_stderr",
                    "category": "logs",
                    "logical_path": artifact.logical_path,
                    "store_path": str(artifact.path),
                    "source_uri": f"evidence://runs/{run.run_id}/{artifact.logical_path}",
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "mime_type": artifact.content_type,
                    "collection_status": "collected",
                    "mutable_during_run": False,
                }
            ],
        )
        return failed

    def test_healthz(self) -> None:
        response = self.api.handle_get("/healthz")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload, {"status": "ok"})

    def test_health_routes_are_unauthenticated_and_report_dependencies(self) -> None:
        api = Pilot107HttpApi(
            store=self.run_store,
            evidence_query=EvidenceQueryService(
                store=self.run_store,
                evidence_store=self.evidence_store,
            ),
            run_service=self.run_service,
            auth_required=True,
        )

        live = api.handle_get("/api/v1/health/live")
        ready = api.handle_get("/api/v1/health/ready")

        self.assertEqual(live.status, 200)
        self.assertEqual(live.payload["status"], "alive")
        self.assertEqual(ready.status, 200)
        self.assertEqual(ready.payload["status"], "ready")
        checks = {item["name"]: item for item in ready.payload["checks"]}
        self.assertEqual(checks["database"]["status"], "ok")
        self.assertEqual(checks["evidence_store"]["status"], "ok")
        self.assertEqual(checks["platform_snapshot_store"]["status"], "disabled")
        self.assertEqual(checks["user_entitlement_store"]["status"], "disabled")
        self.assertEqual(checks["run_submission"]["status"], "configured")
        self.assertEqual(checks["local_llm"]["status"], "disabled")

    def test_get_run_evidence_with_api_v1_prefix(self) -> None:
        run = self.run_store.create_run(
            run_id="run_http",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\nhostname\n",
        )
        self.run_store.apply_submit_receipt(
            run.run_id,
            SubmitReceipt(
                job_id="123",
                run_state=run.state,
                strategy=SubmissionStrategy.COMMAND,
                raw_response={"stdout": "123\n"},
            ),
        )
        self.evidence_store.write_json(
            run_id=run.run_id,
            logical_path="manifest/manifest.json",
            payload={"run_id": run.run_id},
        )

        response = self.api.handle_get(f"/api/v1/runs/{run.run_id}/evidence")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["run_id"], run.run_id)
        self.assertEqual(response.payload["tree"]["children"][0]["name"], "manifest")

        traces = self.api.control_repository.list_traces(run_id=run.run_id)
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0].request_id, response.headers["X-Request-ID"])
        self.assertEqual(traces[0].job_id, "123")
        self.assertEqual(traces[0].route, "/api/v1/runs/{run_id}/{action}")

    def test_get_evidence_object_preview_with_api_v1_prefix(self) -> None:
        run = self.run_store.create_run(
            run_id="run_http_preview",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\ntrue\n",
        )
        artifact = self.evidence_store.write_text(
            run_id=run.run_id,
            logical_path="outputs/result.txt",
            content="preview from evidence\n",
            content_type="text/plain",
        )
        self.run_store.upsert_evidence_objects(
            run.run_id,
            [
                {
                    "object_id": "ev_http_preview",
                    "category": "outputs",
                    "logical_path": artifact.logical_path,
                    "store_path": str(artifact.path),
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "mime_type": artifact.content_type,
                    "collection_status": "collected",
                    "mutable_during_run": False,
                }
            ],
        )

        response = self.api.handle_get(
            f"/api/v1/runs/{run.run_id}/evidence/objects/ev_http_preview"
        )
        missing = self.api.handle_get(f"/api/v1/runs/{run.run_id}/evidence/objects/ev_missing")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["preview"]["content"], "preview from evidence\n")
        self.assertEqual(response.payload["preview"]["integrity"], "verified")
        self.assertNotIn("store_path", response.payload)
        self.assertEqual(missing.status, 404)
        self.assertEqual(missing.payload["error"]["code"], "evidence_object_not_found")

    def test_build_then_get_capsule_without_server_path_disclosure(self) -> None:
        run = self.run_store.create_run(
            run_id="run_http_capsule",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\ntrue\n",
        )
        self.run_store.apply_submit_receipt(
            run.run_id,
            SubmitReceipt(
                job_id="capsule-job",
                run_state=RunState.SUBMITTED,
                strategy=SubmissionStrategy.COMMAND,
            ),
        )
        self.run_store.apply_snapshot(
            run.run_id,
            JobSnapshot(
                job_id="capsule-job",
                owner="alice",
                run_state=RunState.SUCCEEDED,
                raw_state_flags=["COMPLETED"],
                exit_code="0:0",
            ),
        )
        for task in self.run_store.list_collection_tasks(run.run_id):
            self.run_store.mark_collection_task_succeeded(task["task_id"])
        artifact = self.evidence_store.write_text(
            run_id=run.run_id,
            logical_path="submission/user_script.original.sh",
            content=run.script,
            content_type="text/x-shellscript",
        )
        self.evidence_store.write_json(
            run_id=run.run_id,
            logical_path="manifest/manifest.json",
            payload={
                "schema": "pilot107.evidence_manifest.v1",
                "artifacts": [
                    {
                        "logical_path": artifact.logical_path,
                        "sha256": artifact.sha256,
                        "size_bytes": artifact.size_bytes,
                    }
                ],
            },
        )
        capsule_service = RawCapsuleService(
            store=self.run_store,
            evidence_store=self.evidence_store,
            capsule_root=Path(self._tmp.name) / "capsules",
        )
        api = Pilot107HttpApi(
            store=self.run_store,
            evidence_query=EvidenceQueryService(
                store=self.run_store,
                evidence_store=self.evidence_store,
            ),
            capsule_service=capsule_service,
        )

        built = api.handle_post(f"/api/v1/runs/{run.run_id}/capsule", body=b"{}")
        fetched = api.handle_get(f"/api/v1/runs/{run.run_id}/capsule")

        self.assertEqual(built.status, 200)
        self.assertNotIn("capsule_dir", built.payload["capsule"])
        self.assertEqual(fetched.status, 200)
        self.assertTrue(fetched.payload["capsule"]["valid"])
        self.assertEqual(fetched.payload["capsule"]["manifest"]["run_id"], run.run_id)
        self.assertNotIn("capsule_dir", fetched.payload["capsule"])

    def test_post_diagnose_and_get_diagnoses_with_api_v1_prefix(self) -> None:
        run = self._failed_run_with_stderr()

        diagnosed = self.api.handle_post(f"/api/v1/runs/{run.run_id}/diagnose", body=b"{}")
        fetched = self.api.handle_get(f"/api/v1/runs/{run.run_id}/diagnoses")

        self.assertEqual(diagnosed.status, 200)
        self.assertEqual(fetched.status, 200)
        self.assertEqual(fetched.payload["diagnosis_state"], "succeeded")
        self.assertIn(
            "RUNTIME.PYTHON_PACKAGE_MISSING",
            {item["rule_id"] for item in fetched.payload["items"]},
        )
        package_missing = next(
            item
            for item in fetched.payload["items"]
            if item["rule_id"] == "RUNTIME.PYTHON_PACKAGE_MISSING"
        )
        self.assertEqual(package_missing["category"], "optional_dependency")
        self.assertEqual(package_missing["stage"], "runtime")
        self.assertIn("fix", package_missing["fix_guide"])

    def test_known_errors_api_lists_and_fetches_rules(self) -> None:
        listed = self.api.handle_get("/api/v1/diagnosis/known-errors")
        detail = self.api.handle_get("/api/v1/diagnosis/known-errors/SLURM.INVALID_QOS")
        missing = self.api.handle_get("/api/v1/diagnosis/known-errors/UNKNOWN.ERROR")

        self.assertEqual(listed.status, 200)
        self.assertIn(
            "SLURM.INVALID_QOS",
            {item["error_id"] for item in listed.payload["items"]},
        )
        self.assertEqual(detail.status, 200)
        self.assertEqual(detail.payload["error_id"], "SLURM.INVALID_QOS")
        self.assertEqual(detail.payload["fix_template"]["patch"], {"resources.qos": None})
        self.assertIn("invalid qos", detail.payload["symptoms"])
        self.assertEqual(missing.status, 404)
        self.assertEqual(missing.payload["error"]["code"], "known_error_not_found")

    def test_auth_rejects_cross_user_diagnoses(self) -> None:
        run = self._failed_run_with_stderr()
        api = Pilot107HttpApi(
            store=self.run_store,
            evidence_query=EvidenceQueryService(
                store=self.run_store,
                evidence_store=self.evidence_store,
            ),
            run_service=self.run_service,
            auth_required=True,
        )

        response = api.handle_get(
            f"/api/v1/runs/{run.run_id}/diagnoses",
            headers={"X-Pilot107-User": "bob"},
        )

        self.assertEqual(response.status, 403)
        self.assertEqual(response.payload["error"]["code"], "AUTH.FORBIDDEN")

    def test_post_agent_explain_uses_none_provider(self) -> None:
        run = self._failed_run_with_stderr()
        self.api.handle_post(f"/api/v1/runs/{run.run_id}/diagnose", body=b"{}")

        response = self.api.handle_post(
            f"/api/v1/runs/{run.run_id}/agent/explain",
            body=b'{"provider":"none"}',
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["provider"], "none")
        self.assertEqual(response.payload["status"], "explained")
        self.assertTrue(response.payload["facts"])
        self.assertTrue(response.payload["facts"][0]["evidence_refs"])
        self.assertTrue(
            any(
                "RUNTIME.PYTHON_PACKAGE_MISSING" in fact["statement"]
                for fact in response.payload["facts"]
            )
        )

    def test_post_agent_explain_rejects_unsupported_provider(self) -> None:
        run = self._failed_run_with_stderr()

        response = self.api.handle_post(
            f"/api/v1/runs/{run.run_id}/agent/explain",
            body=b'{"provider":"campus"}',
        )

        self.assertEqual(response.status, 400)
        self.assertEqual(response.payload["error"]["code"], "agent_provider_unsupported")

    def test_post_agent_explain_uses_campus_provider_when_configured(self) -> None:
        run = self._failed_run_with_stderr()
        self.api.handle_post(f"/api/v1/runs/{run.run_id}/diagnose", body=b"{}")
        api = Pilot107HttpApi(
            store=self.run_store,
            evidence_query=EvidenceQueryService(
                store=self.run_store,
                evidence_store=self.evidence_store,
            ),
            run_service=self.run_service,
            agent_explain_service=AgentExplainService(
                store=self.run_store,
                llm_provider=FakeCampusProvider(),
                evidence_binder=EvidenceBinder(
                    store=self.run_store,
                    evidence_root=self.evidence_store.root,
                ),
            ),
        )

        response = api.handle_post(
            f"/api/v1/runs/{run.run_id}/agent/explain",
            body=b'{"provider":"campus"}',
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["provider"], "local")
        self.assertEqual(response.payload["model"], "ustc-deepseek/deepseek-v4-pro")
        self.assertEqual(response.payload["narrative"], "检测到 Python 包缺失。")
        self.assertEqual(response.payload["recommendations"], ["安装缺失包"])

    def test_auth_rejects_cross_user_agent_explain(self) -> None:
        run = self._failed_run_with_stderr()
        api = Pilot107HttpApi(
            store=self.run_store,
            evidence_query=EvidenceQueryService(
                store=self.run_store,
                evidence_store=self.evidence_store,
            ),
            run_service=self.run_service,
            auth_required=True,
        )

        response = api.handle_post(
            f"/api/v1/runs/{run.run_id}/agent/explain",
            body=b"{}",
            headers={"X-Pilot107-User": "bob"},
        )

        self.assertEqual(response.status, 403)
        self.assertEqual(response.payload["error"]["code"], "AUTH.FORBIDDEN")

    def test_get_run_summary_with_api_v1_prefix(self) -> None:
        run = self.run_store.create_run(
            run_id="run_summary",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\nhostname\n",
            job_name="summary-smoke",
        )
        self.run_store.apply_submit_receipt(
            run.run_id,
            SubmitReceipt(
                job_id="456",
                run_state=run.state,
                strategy=SubmissionStrategy.COMMAND,
                raw_response={"stdout": "456\n"},
            ),
        )

        response = self.api.handle_get(f"/api/v1/runs/{run.run_id}")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["run_id"], run.run_id)
        self.assertEqual(response.payload["state"], "SUBMITTED")
        self.assertEqual(response.payload["job_id"], "456")
        self.assertEqual(response.payload["job_name"], "summary-smoke")
        self.assertEqual(response.payload["workdir"], "/public/home/alice")
        self.assertEqual(response.payload["collection_state"], "pending")
        self.assertIn("created_at", response.payload)

    def test_get_run_summary_returns_404_for_missing_run(self) -> None:
        response = self.api.handle_get("/api/v1/runs/run_missing")

        self.assertEqual(response.status, 404)
        self.assertEqual(response.payload["error"]["code"], "run_not_found")

    def test_get_recipes_with_api_v1_prefix(self) -> None:
        response = self.api.handle_get("/api/v1/recipes")

        self.assertEqual(response.status, 200)
        by_id = {item["recipe_id"]: item for item in response.payload["items"]}
        self.assertIn("recipe_python_cpu", by_id)
        self.assertEqual(by_id["recipe_python_cpu"]["latest_version"], "1.0.0")

    def test_get_recipe_version_with_api_v1_prefix(self) -> None:
        response = self.api.handle_get("/api/v1/recipes/recipe_python_cpu/versions/1.0.0")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["recipe_version_id"], "recipe_python_cpu@1.0.0")
        self.assertIn("compatibility", response.payload)

    def test_get_platform_capabilities(self) -> None:
        response = self.api.handle_get("/api/v1/platform/capabilities")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["default_partition"], "Students")
        self.assertTrue(response.payload["rest"]["partial_payload_with_errors"])
        partition_names = {item["name"] for item in response.payload["partitions"]}
        self.assertIn("Students", partition_names)

    def test_post_contract_validate_with_api_v1_prefix(self) -> None:
        response = self.api.handle_post(
            "/api/v1/contracts/validate",
            body=_json(_contract_payload()),
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["status"], "OK")
        self.assertEqual(response.payload["effective_request"]["workdir"], "/public/home/alice")

    def test_get_contract_v2_schema(self) -> None:
        response = self.api.handle_get("/api/v1/contracts/schema")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["$id"], "pilot107.contract/v2")
        self.assertFalse(response.payload["additionalProperties"])

    def test_post_contract_create_and_get_with_api_v1_prefix(self) -> None:
        payload = _contract_payload()
        payload["owner"] = "alice"

        created = self.api.handle_post("/api/v1/contracts", body=_json(payload))
        fetched = self.api.handle_get(f"/api/v1/contracts/{created.payload['contract_id']}")

        self.assertEqual(created.status, 201)
        self.assertEqual(fetched.status, 200)
        self.assertEqual(fetched.payload["owner"], "alice")
        self.assertEqual(fetched.payload["recipe_version_id"], "recipe_python_cpu@1.0.0")

    def test_prepare_run_from_contract_id(self) -> None:
        payload = _contract_payload()
        payload["owner"] = "alice"
        created = self.api.handle_post("/api/v1/contracts", body=_json(payload))

        response = self.api.handle_post(
            "/api/v1/runs/prepare",
            body=_json({"contract_id": created.payload["contract_id"]}),
        )

        self.assertEqual(response.status, 201)
        self.assertEqual(response.payload["owner"], "alice")
        self.assertEqual(response.payload["contract_id"], created.payload["contract_id"])
        self.assertEqual(response.payload["state"], "VALIDATED")
        self.assertIn("echo contract-ok", response.payload["preview"]["submitted_script"])

    def test_prepare_derived_run_and_query_lineage(self) -> None:
        root = self.run_service.prepare(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script="echo root",
                resource_plan=ResourcePlan(
                    partition="debug",
                    qos="normal",
                    nodes=1,
                    ntasks=1,
                    cpus_per_task=1,
                    time_limit="00:05:00",
                ),
            )
        )
        payload = _submit_payload()
        payload.update(
            {
                "parent_run_id": root.run_id,
                "lineage_reason": "manual_retry",
            }
        )

        prepared = self.api.handle_post("/api/v1/runs/prepare", body=_json(payload))
        lineage = self.api.handle_get(f"/api/v1/runs/{prepared.payload['run_id']}/lineage")

        self.assertEqual(prepared.status, 201)
        self.assertEqual(prepared.payload["attempt"], 1)
        self.assertEqual(prepared.payload["parent_run_id"], root.run_id)
        self.assertEqual(lineage.status, 200)
        self.assertEqual(
            [item["run_id"] for item in lineage.payload["lineage"]],
            [root.run_id, prepared.payload["run_id"]],
        )

    def test_auth_rejects_cross_user_contract_read(self) -> None:
        payload = _contract_payload()
        payload["owner"] = "alice"
        created = self.api.handle_post("/api/v1/contracts", body=_json(payload))
        api = Pilot107HttpApi(
            store=self.run_store,
            evidence_query=EvidenceQueryService(
                store=self.run_store,
                evidence_store=self.evidence_store,
            ),
            run_service=self.run_service,
            recipe_catalog=self.recipe_catalog,
            contract_service=self.contract_service,
            auth_required=True,
        )

        response = api.handle_get(
            f"/api/v1/contracts/{created.payload['contract_id']}",
            headers={"X-Pilot107-User": "bob"},
        )

        self.assertEqual(response.status, 403)
        self.assertEqual(response.payload["error"]["code"], "AUTH.FORBIDDEN")

    def test_auth_required_returns_401_without_identity(self) -> None:
        api = Pilot107HttpApi(
            store=self.run_store,
            evidence_query=EvidenceQueryService(
                store=self.run_store,
                evidence_store=self.evidence_store,
            ),
            run_service=self.run_service,
            auth_required=True,
        )

        response = api.handle_get("/api/v1/runs/run_missing")

        self.assertEqual(response.status, 401)
        self.assertEqual(response.payload["error"]["code"], "AUTH.MISSING")

    def test_auth_prepare_uses_trusted_header_identity(self) -> None:
        api = Pilot107HttpApi(
            store=self.run_store,
            evidence_query=EvidenceQueryService(
                store=self.run_store,
                evidence_store=self.evidence_store,
            ),
            run_service=self.run_service,
            auth_required=True,
        )
        payload = _submit_payload()
        payload.pop("owner")

        response = api.handle_post(
            "/api/v1/runs/prepare",
            body=_json(payload),
            headers={"X-Pilot107-User": "alice"},
        )

        self.assertEqual(response.status, 201)
        self.assertEqual(response.payload["owner"], "alice")

    def test_auth_prepare_rejects_body_owner_mismatch(self) -> None:
        api = Pilot107HttpApi(
            store=self.run_store,
            evidence_query=EvidenceQueryService(
                store=self.run_store,
                evidence_store=self.evidence_store,
            ),
            run_service=self.run_service,
            auth_required=True,
        )
        payload = _submit_payload()
        payload["owner"] = "bob"

        response = api.handle_post(
            "/api/v1/runs/prepare",
            body=_json(payload),
            headers={"X-Pilot107-User": "alice"},
        )

        self.assertEqual(response.status, 403)
        self.assertEqual(response.payload["error"]["code"], "AUTH.FORBIDDEN")

    def test_auth_contract_create_rejects_body_owner_mismatch(self) -> None:
        api = Pilot107HttpApi(
            store=self.run_store,
            evidence_query=EvidenceQueryService(
                store=self.run_store,
                evidence_store=self.evidence_store,
            ),
            run_service=self.run_service,
            recipe_catalog=self.recipe_catalog,
            contract_service=self.contract_service,
            auth_required=True,
        )
        payload = _contract_payload()
        payload["owner"] = "bob"

        response = api.handle_post(
            "/api/v1/contracts",
            body=_json(payload),
            headers={"X-Pilot107-User": "alice"},
        )

        self.assertEqual(response.status, 403)
        self.assertEqual(response.payload["error"]["code"], "AUTH.FORBIDDEN")

    def test_auth_rejects_cross_user_run_read(self) -> None:
        run = self.run_store.create_run(
            run_id="run_alice",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\nhostname\n",
        )
        api = Pilot107HttpApi(
            store=self.run_store,
            evidence_query=EvidenceQueryService(
                store=self.run_store,
                evidence_store=self.evidence_store,
            ),
            run_service=self.run_service,
            auth_required=True,
        )

        response = api.handle_get(
            f"/api/v1/runs/{run.run_id}",
            headers={"X-Pilot107-User": "bob"},
        )

        self.assertEqual(response.status, 403)
        self.assertEqual(response.payload["error"]["code"], "AUTH.FORBIDDEN")

    def test_auth_rejects_cross_user_evidence_read(self) -> None:
        run = self.run_store.create_run(
            run_id="run_alice_evidence",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\nhostname\n",
        )
        self.evidence_store.write_json(
            run_id=run.run_id,
            logical_path="manifest/manifest.json",
            payload={"run_id": run.run_id},
        )
        api = Pilot107HttpApi(
            store=self.run_store,
            evidence_query=EvidenceQueryService(
                store=self.run_store,
                evidence_store=self.evidence_store,
            ),
            run_service=self.run_service,
            auth_required=True,
        )

        response = api.handle_get(
            f"/api/v1/runs/{run.run_id}/evidence",
            headers={"X-Pilot107-User": "bob"},
        )

        self.assertEqual(response.status, 403)
        self.assertEqual(response.payload["error"]["code"], "AUTH.FORBIDDEN")

    def test_auth_rejects_cross_user_evidence_object_preview(self) -> None:
        run = self.run_store.create_run(
            run_id="run_alice_preview",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\ntrue\n",
        )
        api = Pilot107HttpApi(
            store=self.run_store,
            evidence_query=EvidenceQueryService(
                store=self.run_store,
                evidence_store=self.evidence_store,
            ),
            run_service=self.run_service,
            auth_required=True,
        )

        response = api.handle_get(
            f"/api/v1/runs/{run.run_id}/evidence/objects/ev_missing",
            headers={"X-Pilot107-User": "bob"},
        )

        self.assertEqual(response.status, 403)
        self.assertEqual(response.payload["error"]["code"], "AUTH.FORBIDDEN")

    def test_auth_rejects_unsafe_identity_header(self) -> None:
        api = Pilot107HttpApi(
            store=self.run_store,
            evidence_query=EvidenceQueryService(
                store=self.run_store,
                evidence_store=self.evidence_store,
            ),
            run_service=self.run_service,
            auth_required=True,
        )

        response = api.handle_get(
            "/api/v1/runs/run_missing",
            headers={"X-Pilot107-User": "../alice"},
        )

        self.assertEqual(response.status, 403)
        self.assertEqual(response.payload["error"]["code"], "AUTH.FORBIDDEN")

    def test_post_prepare_run_with_api_v1_prefix(self) -> None:
        response = self.api.handle_post("/api/v1/runs/prepare", body=_submit_body())

        self.assertEqual(response.status, 201)
        self.assertEqual(response.payload["owner"], "alice")
        self.assertEqual(response.payload["state"], "VALIDATED")
        self.assertIn("run_id", response.payload)
        self.assertIn("submitted_script_sha256", response.payload["script_artifacts"])
        self.assertIn("execution_wrapper", response.payload["preview"])
        self.assertEqual(response.payload["preflight"], [])
        stored = self.run_store.get_run(response.payload["run_id"])
        self.assertEqual(stored.resource_plan["partition"], "debug")

    def test_post_prepare_blocks_invalid_resource_plan(self) -> None:
        payload = _submit_payload()
        payload["resource_plan"]["time_limit"] = None

        response = self.api.handle_post("/api/v1/runs/prepare", body=_json(payload))

        self.assertEqual(response.status, 422)
        self.assertEqual(response.payload["error"]["code"], "preflight_blocked")
        self.assertEqual(response.payload["preflight"][0]["code"], "RESOURCE.TIME_LIMIT_REQUIRED")

    def test_post_submit_prepared_run_with_api_v1_prefix(self) -> None:
        prepared = self.api.handle_post("/api/v1/runs/prepare", body=_submit_body())
        run_id = prepared.payload["run_id"]

        response = self.api.handle_post(f"/api/v1/runs/{run_id}/submit", body=b"{}")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["run_id"], run_id)
        self.assertEqual(response.payload["state"], "SUBMITTED")
        self.assertEqual(response.payload["submit_state"], "submitted")
        self.assertIsNotNone(response.payload["job_id"])

    def test_post_submit_returns_structured_422_for_owner_root_violation(self) -> None:
        self.api.run_service = RunService(
            store=self.run_store,
            backend=self.backend,
            workdir_preflight_enabled=True,
            preflight_allowed_roots=("/public/home/{user}",),
            preflight_shared_roots=("/public",),
            preflight_local_roots=("/tmp",),
        )
        payload = _submit_payload()
        payload["workdir"] = "/public/home/bob"
        prepared = self.api.handle_post("/api/v1/runs/prepare", body=_json(payload))

        response = self.api.handle_post(
            f"/api/v1/runs/{prepared.payload['run_id']}/submit",
            body=b"{}",
        )

        self.assertEqual(response.status, 422)
        self.assertEqual(response.payload["error"]["code"], "workdir_preflight_blocked")
        self.assertIn(
            "WORKDIR_NOT_ALLOWED",
            {finding["code"] for finding in response.payload["preflight"]},
        )

    def test_post_submit_prepared_run_returns_404_for_missing_run(self) -> None:
        response = self.api.handle_post("/api/v1/runs/run_missing/submit", body=b"{}")

        self.assertEqual(response.status, 404)
        self.assertEqual(response.payload["error"]["code"], "run_not_found")

    def test_post_submit_returns_conflict_when_another_worker_claimed_run(self) -> None:
        prepared = self.api.handle_post("/api/v1/runs/prepare", body=_submit_body())
        run_id = prepared.payload["run_id"]
        self.assertTrue(self.run_store.claim_submission(run_id))

        response = self.api.handle_post(f"/api/v1/runs/{run_id}/submit", body=b"{}")

        self.assertEqual(response.status, 409)
        self.assertEqual(response.payload["error"]["code"], "submission_in_progress")

    def test_post_cancel_run_with_api_v1_prefix(self) -> None:
        run = self.run_service.submit(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\nsleep 30\n",
                resource_plan=ResourcePlan(
                    partition="debug",
                    qos="normal",
                    nodes=1,
                    ntasks=1,
                    cpus_per_task=1,
                    time_limit="00:05:00",
                ),
            )
        )

        response = self.api.handle_post(f"/api/v1/runs/{run.run_id}/cancel", body=b"{}")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["run_id"], run.run_id)
        self.assertEqual(response.payload["state"], "CANCELLED")
        self.assertEqual(self.run_store.get_run(run.run_id).state, RunState.CANCELLED)

    def test_post_cancel_run_returns_404_for_missing_run(self) -> None:
        response = self.api.handle_post("/api/v1/runs/run_missing/cancel", body=b"{}")

        self.assertEqual(response.status, 404)
        self.assertEqual(response.payload["error"]["code"], "run_not_found")

    def test_auth_rejects_cross_user_cancel(self) -> None:
        run = self.run_service.submit(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\nsleep 30\n",
                resource_plan=ResourcePlan(
                    partition="debug",
                    qos="normal",
                    nodes=1,
                    ntasks=1,
                    cpus_per_task=1,
                    time_limit="00:05:00",
                ),
            )
        )
        api = Pilot107HttpApi(
            store=self.run_store,
            evidence_query=EvidenceQueryService(
                store=self.run_store,
                evidence_store=self.evidence_store,
            ),
            run_service=self.run_service,
            auth_required=True,
        )

        response = api.handle_post(
            f"/api/v1/runs/{run.run_id}/cancel",
            body=b"{}",
            headers={"X-Pilot107-User": "bob"},
        )

        self.assertEqual(response.status, 403)
        self.assertEqual(response.payload["error"]["code"], "AUTH.FORBIDDEN")

    def test_post_cancel_without_run_service_returns_503(self) -> None:
        api = Pilot107HttpApi(
            store=self.run_store,
            evidence_query=EvidenceQueryService(
                store=self.run_store,
                evidence_store=self.evidence_store,
            ),
        )

        response = api.handle_post("/api/v1/runs/run_any/cancel", body=b"{}")

        self.assertEqual(response.status, 503)
        self.assertEqual(response.payload["error"]["code"], "run_service_unavailable")

    def test_get_run_evidence_legacy_local_path_still_works(self) -> None:
        run = self.run_store.create_run(
            run_id="run_http_legacy",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\nhostname\n",
        )

        response = self.api.handle_get(f"/runs/{run.run_id}/evidence")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["run_id"], run.run_id)

    def test_get_run_evidence_returns_404_for_missing_run(self) -> None:
        response = self.api.handle_get("/api/v1/runs/run_missing/evidence")

        self.assertEqual(response.status, 404)
        self.assertEqual(response.payload["error"]["code"], "run_not_found")

    def test_unknown_route_returns_404(self) -> None:
        response = self.api.handle_get("/unknown")

        self.assertEqual(response.status, 404)
        self.assertEqual(response.payload["error"]["code"], "not_found")

    def test_remediation_service_receives_stores_when_injected(self) -> None:
        # P1-1 (round 5 audit): the API process's RemediationService must
        # receive contract_store + evidence_store so manual /advance performs
        # strict expected-output verification (same as the Worker path).
        contract_store = ContractStore(self.db_path)
        api = Pilot107HttpApi(
            store=self.run_store,
            evidence_query=EvidenceQueryService(
                store=self.run_store,
                evidence_store=self.evidence_store,
            ),
            run_service=self.run_service,
            recipe_catalog=self.recipe_catalog,
            contract_service=self.contract_service,
            contract_store=contract_store,
            evidence_store=self.evidence_store,
        )
        self.assertIsNotNone(api.remediation_service.contract_store)
        self.assertIsNotNone(api.remediation_service.evidence_store)

    def test_remediation_service_stores_default_to_none_without_injection(self) -> None:
        # Backward compat: when stores are NOT injected (legacy callers), the
        # RemediationService falls back to legacy VERIFIED_SUCCESS. This is the
        # behavior the round-5 audit flagged for the API path — now fixed by
        # build_api_service threading the stores through. This test documents
        # the fallback so a future regression is visible.
        api = Pilot107HttpApi(
            store=self.run_store,
            evidence_query=EvidenceQueryService(
                store=self.run_store,
                evidence_store=self.evidence_store,
            ),
            run_service=self.run_service,
            recipe_catalog=self.recipe_catalog,
            contract_service=self.contract_service,
        )
        self.assertIsNone(api.remediation_service.contract_store)
        self.assertIsNone(api.remediation_service.evidence_store)


def _submit_body() -> bytes:
    return _json(_submit_payload())


def _submit_payload() -> dict:
    return {
        "owner": "alice",
        "workdir": "/public/home/alice",
        "script": "#!/bin/bash\nhostname\n",
        "resource_plan": {
            "partition": "debug",
            "qos": "normal",
            "nodes": 1,
            "ntasks": 1,
            "cpus_per_task": 1,
            "time_limit": "00:05:00",
        },
    }


class FakeCampusProvider:
    provider_name = "local"
    model = "ustc-deepseek/deepseek-v4-pro"

    def explain(self, explanation):
        citations = tuple(
            AgentCitation(
                fact_id=fact.fact_id,
                evidence_object_ids=fact.evidence_object_ids,
            )
            for fact in explanation.facts
        )
        return LLMExplanation(
            summary=explanation.summary,
            narrative="检测到 Python 包缺失。",
            recommendations=("安装缺失包",),
            model=self.model,
            citations=citations,
        )


def _contract_payload() -> dict:
    return {
        "recipe_version_id": "recipe_python_cpu@1.0.0",
        "project": {
            "workdir": "/public/home/alice",
        },
        "entry": {
            "command": "echo contract-ok",
            "expected_outputs": ["result.txt"],
        },
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
