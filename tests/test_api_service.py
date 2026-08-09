import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from pilot107.adapters.slurm import SimulatorPathChecker
from pilot107.api.service import build_api_service, config_from_env
from pilot107.core.platform_snapshot import (
    ObservationSourceType,
    PartitionSnapshot,
    PlatformSnapshot,
    PlatformSnapshotScope,
)
from pilot107.core.user_entitlement import (
    EntitlementDataQuality,
    UserAssociation,
    UserEntitlementSnapshot,
)


class ApiServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_config_from_env_uses_read_only_defaults(self) -> None:
        config = config_from_env({}, project_root=self.root)

        self.assertEqual(config.db_path, self.root / "data" / "phase0" / "pilot107.db")
        self.assertEqual(config.evidence_root, self.root / "data" / "phase0" / "evidence")
        self.assertIsNone(config.control_postgres_dsn)
        self.assertIsNone(config.postgres_dsn)
        self.assertEqual(config.backend, "none")
        self.assertEqual(
            config.worker_metrics_root,
            self.root / "data" / "phase0" / "worker-metrics",
        )
        self.assertEqual(config.allowed_roots, ())
        self.assertFalse(config.auth_required)

    def test_config_from_env_accepts_backend_and_auth_overrides(self) -> None:
        config = config_from_env(
            {
                "PILOT107_DB_PATH": str(self.root / "api.db"),
                "PILOT107_EVIDENCE_ROOT": str(self.root / "evidence"),
                "PILOT107_API_BACKEND": "rest-native",
                "PILOT107_ALLOWED_ROOTS": "/public/home/alice,/public/home/bob",
                "PILOT107_COMMAND_TIMEOUT_SECONDS": "4.5",
                "PILOT107_SLURMRESTD_URL": "http://slurmrestd.example:6820",
                "PILOT107_SLURM_API_VERSION": "v0.0.42",
                "PILOT107_SLURM_TOKEN": "token-test",
                "PILOT107_REST_AUTH_STYLE": "slurm_headers",
                "PILOT107_SLURM_USER_NAME": "alice",
                "PILOT107_AUTH_REQUIRED": "true",
                "PILOT107_TRUSTED_USER_HEADER": "X-Remote-User",
                "PILOT107_PROXY_HMAC_SECRET": "x" * 32,
                "PILOT107_PROXY_SIGNATURE_MAX_AGE_SECONDS": "45",
                "PILOT107_CONTRACT_PROFILE": "real107-sim",
                "PILOT107_CAPABILITY_PROFILE_PATH": str(self.root / "probe"),
                "PILOT107_ALLOW_GPU_RECIPES": "false",
                "PILOT107_LLM_BASE_URL": "https://api.llm.example.edu.cn/v1",
                "PILOT107_LLM_API_KEY": "test-key",
                "PILOT107_LLM_MODEL": "ustc-deepseek/deepseek-v4-pro",
                "PILOT107_LLM_TIMEOUT_SECONDS": "3.5",
                "PILOT107_LLM_MAX_TOKENS": "512",
                "PILOT107_LLM_STRUCTURED_OUTPUT_MODE": "json_schema",
                "PILOT107_LLM_MAX_ATTEMPTS": "3",
                "PILOT107_TEMPLATE_REVIEWERS": "reviewer,reviewer2",
                "PILOT107_TEMPLATE_ADMINS": "admin",
                "PILOT107_TEMPLATE_COURSE_INSTRUCTORS": "course-107=teacher",
                "PILOT107_TEMPLATE_COURSE_TAS": "course-107=ta",
                "PILOT107_TEMPLATE_COURSE_MEMBERS": "course-107=alice,course-107=bob",
                "PILOT107_TEMPLATE_VERIFIED_CONTAINER_DIGESTS": f"sha256:{'a' * 64}",
                "PILOT107_TEMPLATE_VERIFICATION_ENVIRONMENT": "docker",
            },
            project_root=self.root,
        )

        self.assertEqual(config.db_path, self.root / "api.db")
        self.assertEqual(config.evidence_root, self.root / "evidence")
        self.assertEqual(config.backend, "rest-native")
        self.assertEqual(config.allowed_roots, ("/public/home/alice", "/public/home/bob"))
        self.assertEqual(config.command_timeout_seconds, 4.5)
        self.assertEqual(config.slurmrestd_url, "http://slurmrestd.example:6820")
        self.assertEqual(config.slurm_api_version, "v0.0.42")
        self.assertEqual(config.slurm_token, "token-test")
        self.assertEqual(config.rest_auth_style, "slurm_headers")
        self.assertEqual(config.slurm_username, "alice")
        self.assertTrue(config.auth_required)
        self.assertEqual(config.trusted_user_header, "X-Remote-User")
        self.assertEqual(config.proxy_hmac_secret, b"x" * 32)
        self.assertEqual(config.proxy_signature_max_age_seconds, 45)
        self.assertEqual(config.contract_profile, "real107-sim")
        self.assertEqual(config.capability_profile_path, self.root / "probe")
        self.assertFalse(config.allow_gpu_recipes)
        self.assertEqual(config.llm_base_url, "https://api.llm.example.edu.cn/v1")
        self.assertEqual(config.llm_api_key, "test-key")
        self.assertEqual(config.llm_model, "ustc-deepseek/deepseek-v4-pro")
        self.assertEqual(config.llm_timeout_seconds, 3.5)
        self.assertEqual(config.llm_max_tokens, 512)
        self.assertEqual(config.llm_structured_output_mode, "json_schema")
        self.assertEqual(config.llm_max_attempts, 3)
        self.assertEqual(config.template_reviewers, frozenset({"reviewer", "reviewer2"}))
        self.assertEqual(config.template_admins, frozenset({"admin"}))
        self.assertEqual(
            config.template_course_instructors,
            {"course-107": frozenset({"teacher"})},
        )
        self.assertEqual(config.template_course_tas, {"course-107": frozenset({"ta"})})
        self.assertEqual(
            config.template_course_members,
            {"course-107": frozenset({"alice", "bob"})},
        )
        self.assertEqual(
            config.template_verified_container_digests,
            frozenset({f"sha256:{'a' * 64}"}),
        )
        self.assertEqual(config.template_verification_environment, "docker")

    def test_postgres_domain_dsn_also_selects_the_control_plane_database(self) -> None:
        dsn = "postgresql://pilot107.example/pilot107"
        config = config_from_env({"PILOT107_POSTGRES_DSN": dsn}, project_root=self.root)

        self.assertEqual(config.postgres_dsn, dsn)
        self.assertEqual(config.control_postgres_dsn, dsn)

    def test_template_verification_environment_is_server_controlled(self) -> None:
        api = build_api_service(
            config_from_env(
                {"PILOT107_TEMPLATE_VERIFICATION_ENVIRONMENT": "real107_cpu"},
                project_root=self.root,
            )
        )

        self.assertIsNotNone(api.template_verification_service)
        self.assertEqual(api.template_verification_service.environment, "real107_cpu")
        with self.assertRaisesRegex(ValueError, "invalid template verification environment"):
            config_from_env(
                {"PILOT107_TEMPLATE_VERIFICATION_ENVIRONMENT": "browser"},
                project_root=self.root,
            )

    def test_template_role_config_rejects_unsafe_server_side_membership(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid template course member"):
            config_from_env(
                {"PILOT107_TEMPLATE_COURSE_MEMBERS": "course-107=../alice"},
                project_root=self.root,
            )

    def test_build_api_service_exposes_default_capabilities(self) -> None:
        api = build_api_service(config_from_env({}, project_root=self.root))

        response = api.handle_get("/api/v1/platform/capabilities")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["profile_id"], "simulator-real107-behavior")
        self.assertEqual(response.payload["default_partition"], "Students")

    def test_build_api_service_none_backend_is_read_only(self) -> None:
        api = build_api_service(config_from_env({}, project_root=self.root))

        response = api.handle_post(
            "/api/v1/runs/prepare",
            body=_json(_submit_payload()),
        )

        self.assertEqual(response.status, 503)
        self.assertEqual(response.payload["error"]["code"], "run_service_unavailable")

    def test_build_api_service_in_memory_backend_submits_run(self) -> None:
        api = build_api_service(
            config_from_env(
                {
                    "PILOT107_API_BACKEND": "in-memory",
                    "PILOT107_AUTH_REQUIRED": "1",
                    "PILOT107_ALLOWED_ROOTS": "/public/home/{user}",
                },
                project_root=self.root,
            )
        )
        payload = _submit_payload()
        payload.pop("owner")
        prepared = api.handle_post(
            "/api/v1/runs/prepare",
            body=_json(payload),
            headers={"X-Pilot107-User": "alice"},
        )

        submitted = api.handle_post(
            f"/api/v1/runs/{prepared.payload['run_id']}/submit",
            body=b"{}",
            headers={"X-Pilot107-User": "alice"},
        )

        self.assertEqual(prepared.status, 201)
        self.assertEqual(submitted.status, 200)
        self.assertEqual(submitted.payload["owner"], "alice")
        self.assertEqual(submitted.payload["submit_strategy"], "in_memory")
        self.assertIsNotNone(submitted.payload["job_id"])

    def test_build_api_service_direct_prepare_blocks_qos_numeric_limits(self) -> None:
        api = build_api_service(
            config_from_env(
                {
                    "PILOT107_API_BACKEND": "in-memory",
                    "PILOT107_AUTH_REQUIRED": "1",
                },
                project_root=self.root,
            )
        )
        payload = _submit_payload()
        payload.pop("owner")
        payload["resource_plan"].update(
            {
                "partition": "Students",
                "qos": "qos_stu_default",
                "cpus_per_task": 8,
                "gpus_total": 2,
                "memory_value": 32,
                "memory_unit": "G",
                "time_limit": "05:00:00",
            }
        )

        response = api.handle_post(
            "/api/v1/runs/prepare",
            body=_json(payload),
            headers={"X-Pilot107-User": "alice"},
        )

        self.assertEqual(response.status, 422)
        codes = {finding["code"] for finding in response.payload["preflight"]}
        self.assertIn("RESOURCE.QOS_CPU_LIMIT_EXCEEDED", codes)
        self.assertIn("RESOURCE.QOS_GPU_LIMIT_EXCEEDED", codes)
        self.assertIn("RESOURCE.QOS_MEMORY_LIMIT_EXCEEDED", codes)
        self.assertIn("RESOURCE.QOS_WALLTIME_LIMIT_EXCEEDED", codes)

    def test_build_api_service_demo_backend_submits_cross_process_job_id(self) -> None:
        api = build_api_service(
            config_from_env(
                {
                    "PILOT107_API_BACKEND": "demo",
                    "PILOT107_AUTH_REQUIRED": "1",
                    "PILOT107_ALLOWED_ROOTS": "/public/home/{user}",
                },
                project_root=self.root,
            )
        )
        payload = _submit_payload()
        payload.pop("owner")

        prepared = api.handle_post(
            "/api/v1/runs/prepare",
            body=_json(payload),
            headers={"X-Pilot107-User": "alice"},
        )
        submitted = api.handle_post(
            f"/api/v1/runs/{prepared.payload['run_id']}/submit",
            body=b"{}",
            headers={"X-Pilot107-User": "alice"},
        )

        self.assertEqual(submitted.status, 200)
        self.assertEqual(submitted.payload["submit_strategy"], "demo")
        self.assertTrue(submitted.payload["job_id"].startswith("demo-"))

    def test_command_gateway_preflight_is_scoped_to_run_owner(self) -> None:
        api = build_api_service(
            config_from_env(
                {
                    "PILOT107_API_BACKEND": "command-gateway",
                    "PILOT107_COMMAND_GATEWAY_URL": "http://gateway.invalid:8090",
                },
                project_root=self.root,
            )
        )

        assert api.run_service is not None
        factory = api.run_service.preflight_path_checker_factory
        self.assertIsNone(api.run_service.preflight_path_checker)
        self.assertIsNotNone(factory)
        assert factory is not None
        checker = factory("alice")
        self.assertIsInstance(checker, SimulatorPathChecker)
        self.assertEqual(checker.user, "alice")

    def test_command_gateway_backend_auto_collects_login_node_snapshot(self) -> None:
        scontrol_nodes = (
            "NodeName=anode16 Arch=x86_64 CoresPerSocket=8\n"
            "   CPUAlloc=2 CPUEfctv=8 CPUTot=8 CPULoad=0.64\n"
            "   Gres=(null)\n"
            "   RealMemory=15360 AllocMem=0 FreeMem=2816\n"
            "   State=MIXED ThreadsPerCore=1\n"
            "   Partitions=CPU-RC\n"
        )
        scontrol_part = "PartitionName=CPU-RC State=UP TotalCPUs=8 TotalNodes=1\n"
        squeue_out = "42|RUNNING|CPU-RC|CPU-RC|trainjob\n"

        class FakeGatewayExecutor:
            def __init__(self, *args: object, **kwargs: object) -> None: ...

            def run(
                self,
                argv: list[str],
                *,
                cwd: str | None = None,
                user: str | None = None,
                stdin: str | None = None,
                timeout_seconds: float = 10.0,
            ) -> object:
                from pilot107.adapters.slurm import CommandResult

                if argv[:3] == ["scontrol", "show", "nodes"]:
                    return CommandResult(0, scontrol_nodes, "")
                if argv[:3] == ["scontrol", "show", "part"]:
                    return CommandResult(0, scontrol_part, "")
                if argv[:1] == ["squeue"]:
                    return CommandResult(0, squeue_out, "")
                return CommandResult(0, "", "")

        with patch("pilot107.api.service.HttpCommandGatewayExecutor", FakeGatewayExecutor):
            api = build_api_service(
                config_from_env(
                    {
                        "PILOT107_API_BACKEND": "command-gateway",
                        "PILOT107_AUTH_REQUIRED": "1",
                        "PILOT107_ALLOWED_ROOTS": "/public/home/alice",
                        "PILOT107_COMMAND_GATEWAY_URL": "http://gateway.invalid:8090",
                    },
                    project_root=self.root,
                )
            )

        response = api.handle_get(
            "/api/v1/platform/snapshots/latest?owner=alice&scope=login_node",
            headers={"X-Pilot107-User": "alice"},
        )

        self.assertEqual(response.status, 200)
        snapshot = response.payload["snapshot"]
        self.assertEqual(snapshot["scope"], "login_node")
        self.assertEqual(len(snapshot["nodes"]), 1)
        self.assertEqual(snapshot["nodes"][0]["node_name"], "anode16")
        self.assertEqual(snapshot["nodes"][0]["cpus_total"], 8)
        self.assertEqual(snapshot["nodes"][0]["cpus_allocated"], 2)
        self.assertEqual(len(snapshot["partitions"]), 1)
        self.assertEqual(snapshot["partitions"][0]["name"], "CPU-RC")
        self.assertEqual(len(snapshot["squeue_jobs"]), 1)
        self.assertEqual(snapshot["squeue_jobs"][0]["state_raw"], "RUNNING")

    def test_contract_preflight_includes_owner_platform_snapshot_findings(self) -> None:
        api = build_api_service(
            config_from_env(
                {"PILOT107_AUTH_REQUIRED": "1"},
                project_root=self.root,
            )
        )
        now = datetime.now(UTC)
        api.platform_snapshot_store.create(
            owner="alice",
            snapshot=PlatformSnapshot(
                snapshot_id="snapshot_api_preflight",
                scope=PlatformSnapshotScope.LOGIN_NODE,
                captured_at=now.isoformat(),
                collector_version="test.v1",
                partitions=(PartitionSnapshot(name="debug", state_raw="DOWN"),),
            ),
            source_type=ObservationSourceType.SIMULATOR,
            source_name="docker-sim",
            expires_at=(now + timedelta(minutes=5)).isoformat(),
        )
        created = api.handle_post(
            "/api/v1/contracts",
            body=_json(_contract_payload()),
            headers={"X-Pilot107-User": "alice"},
        )

        response = api.handle_post(
            f"/api/v1/contracts/{created.payload['contract_id']}/preflight",
            body=b"{}",
            headers={"X-Pilot107-User": "alice"},
        )

        self.assertEqual(response.status, 200)
        finding = next(
            item
            for item in response.payload["findings"]
            if item["code"] == "PLATFORM.PARTITION_NOT_UP"
        )
        self.assertEqual(finding["severity"], "WARN")
        self.assertEqual(
            finding["source_authority"],
            "platform_snapshot:snapshot_api_preflight",
        )

    def test_direct_prepare_blocks_qos_missing_from_fresh_entitlement(self) -> None:
        api = build_api_service(
            config_from_env(
                {
                    "PILOT107_API_BACKEND": "in-memory",
                    "PILOT107_AUTH_REQUIRED": "1",
                },
                project_root=self.root,
            )
        )
        self._store_entitlement(api, qos=("normal",))
        payload = _submit_payload()
        payload.pop("owner")
        payload["resource_plan"].update({"partition": "Students", "qos": "qos_stu_default"})

        response = api.handle_post(
            "/api/v1/runs/prepare",
            body=_json(payload),
            headers={"X-Pilot107-User": "alice"},
        )

        self.assertEqual(response.status, 422)
        codes = {item["code"] for item in response.payload["preflight"]}
        self.assertIn("ENTITLEMENT.QOS_NOT_ALLOWED", codes)

    def test_direct_prepare_returns_and_persists_entitlement_findings(self) -> None:
        api = build_api_service(
            config_from_env(
                {
                    "PILOT107_API_BACKEND": "in-memory",
                    "PILOT107_AUTH_REQUIRED": "1",
                },
                project_root=self.root,
            )
        )
        self._store_entitlement(api, qos=("qos_stu_default",))
        payload = _submit_payload()
        payload.pop("owner")
        payload["resource_plan"].update({"partition": "Students", "qos": "qos_stu_default"})

        response = api.handle_post(
            "/api/v1/runs/prepare",
            body=_json(payload),
            headers={"X-Pilot107-User": "alice"},
        )

        self.assertEqual(response.status, 201)
        response_codes = {item["code"] for item in response.payload["preflight"]}
        self.assertIn("ENTITLEMENT.QOS_CONFIRMED", response_codes)
        event = next(
            item
            for item in api.store.list_events(response.payload["run_id"])
            if item.event_type == "run.preflight"
        )
        event_codes = {item["code"] for item in event.payload["findings"]}
        self.assertIn("ENTITLEMENT.QOS_CONFIRMED", event_codes)

    def _store_entitlement(self, api, *, qos: tuple[str, ...]) -> None:
        now = datetime.now(UTC)
        api.user_entitlement_store.create(
            owner="alice",
            snapshot=UserEntitlementSnapshot(
                snapshot_id="entitlement_api_prepare",
                captured_at=now.isoformat(),
                collector_version="test.v1",
                data_quality=EntitlementDataQuality.AUTHORITATIVE,
                default_account="students",
                associations=(
                    UserAssociation(
                        account="students",
                        partition=None,
                        qos=qos,
                        default_qos=qos[0],
                    ),
                ),
            ),
            source_type=ObservationSourceType.SIMULATOR,
            source_name="docker-sim",
            expires_at=(now + timedelta(minutes=5)).isoformat(),
        )


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


def _contract_payload() -> dict:
    return {
        "recipe_version_id": "recipe_python_cpu@1.0.0",
        "project": {"workdir": "/public/home/alice"},
        "entry": {"command": "echo contract-ok", "expected_outputs": []},
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
