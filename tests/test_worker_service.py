import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, NoReturn

from pilot107.adapters.slurm import (
    DemoSlurmBackend,
    SimulatorPathChecker,
    SubmissionStrategy,
    SubmitReceipt,
)
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.core.remediation import RemediationState
from pilot107.core.resources import ResourcePlan
from pilot107.core.run_service import RunService, RunSubmitRequest
from pilot107.core.run_store import RunStore
from pilot107.core.states import CollectionState, RunState
from pilot107.worker.runtime_worker import WorkerRunError, WorkerTickResult
from pilot107.worker.service import build_worker_service, config_from_env


class WorkerServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_config_from_env_uses_phase0_defaults(self) -> None:
        config = config_from_env({}, project_root=self.root)

        self.assertEqual(config.db_path, self.root / "data" / "phase0" / "pilot107.db")
        self.assertEqual(config.evidence_root, self.root / "data" / "phase0" / "evidence")
        self.assertIsNone(config.control_postgres_dsn)
        self.assertIsNone(config.postgres_dsn)
        self.assertEqual(config.backend, "docker-compose-command")
        self.assertEqual(config.allowed_roots, ())
        self.assertEqual(config.compose_file, self.root / "simulator" / "compose" / "compose.yml")
        self.assertEqual(config.health_path, self.root / "data" / "phase0" / "worker-health.json")
        self.assertEqual(
            config.metrics_root,
            self.root / "data" / "phase0" / "worker-metrics",
        )

    def test_config_from_env_accepts_service_overrides(self) -> None:
        config = config_from_env(
            {
                "PILOT107_DB_PATH": str(self.root / "db.sqlite"),
                "PILOT107_EVIDENCE_ROOT": str(self.root / "evidence-root"),
                "PILOT107_WORKER_BACKEND": "in-memory",
                "PILOT107_ALLOWED_ROOTS": "/public/home/alice,/public/home/bob",
                "PILOT107_WORKER_ID": "worker-test",
                "PILOT107_WORKER_BATCH_SIZE": "7",
                "PILOT107_WORKER_INTERVAL_SECONDS": "0.25",
                "PILOT107_WORKER_TASK_LEASE_SECONDS": "11",
                "PILOT107_COMMAND_TIMEOUT_SECONDS": "3.5",
                "PILOT107_SLURMRESTD_URL": "http://slurmrestd.example:6820",
                "PILOT107_SLURM_API_VERSION": "v0.0.42",
                "PILOT107_SLURM_TOKEN": "token-test",
                "PILOT107_REST_AUTH_STYLE": "slurm_headers",
                "PILOT107_SLURM_USER_NAME": "alice",
                "PILOT107_WORKER_HEALTH_PATH": "",
                "PILOT107_WORKER_METRICS_ROOT": "",
                "PILOT107_CONTROL_POSTGRES_DSN": "postgresql://control.example/pilot107",
                "PILOT107_AGENTD_URL": "http://pilot-agentd:8091",
                "PILOT107_AGENTD_TOKEN": "internal-agentd-token",
                "PILOT107_AGENTD_MODEL_PROFILE": "campus-default",
                "PILOT107_OBSERVABILITY_ENABLED": "true",
                "PILOT107_OBSERVABILITY_MAX_COMMANDS_PER_MINUTE": "17",
                "PILOT107_OBSERVABILITY_BATCH_SIZE": "23",
            },
            project_root=self.root,
        )

        self.assertEqual(config.db_path, self.root / "db.sqlite")
        self.assertEqual(config.evidence_root, self.root / "evidence-root")
        self.assertEqual(config.backend, "in-memory")
        self.assertEqual(config.allowed_roots, ("/public/home/alice", "/public/home/bob"))
        self.assertEqual(config.worker_id, "worker-test")
        self.assertEqual(config.batch_size, 7)
        self.assertEqual(config.interval_seconds, 0.25)
        self.assertEqual(config.task_lease_seconds, 11)
        self.assertEqual(config.command_timeout_seconds, 3.5)
        self.assertEqual(config.slurmrestd_url, "http://slurmrestd.example:6820")
        self.assertEqual(config.slurm_api_version, "v0.0.42")
        self.assertEqual(config.slurm_token, "token-test")
        self.assertEqual(config.rest_auth_style, "slurm_headers")
        self.assertEqual(config.slurm_username, "alice")
        self.assertIsNone(config.health_path)
        self.assertIsNone(config.metrics_root)
        self.assertEqual(
            config.control_postgres_dsn,
            "postgresql://control.example/pilot107",
        )
        self.assertFalse(config.enable_docker_volume_evidence_transport)
        self.assertEqual(config.agentd_url, "http://pilot-agentd:8091")
        self.assertEqual(config.agentd_token, "internal-agentd-token")
        self.assertEqual(config.agentd_model_profile, "campus-default")
        self.assertFalse(config.agent_a1_enabled)
        self.assertTrue(config.observability_enabled)
        self.assertEqual(config.observability_max_commands_per_minute, 17)
        self.assertEqual(config.observability_batch_size, 23)
        self.assertIsNone(config.agent_capability_hmac_secret_file)
        self.assertNotIn("internal-agentd-token", repr(config))

    def test_build_worker_service_requires_agent_capability_secret_when_enabled(self) -> None:
        with self.assertRaisesRegex(ValueError, "capability HMAC secret"):
            build_worker_service(
                config_from_env(
                    {"PILOT107_WORKER_BACKEND": "in-memory", "PILOT107_AGENT_A1_ENABLED": "true"},
                    project_root=self.root,
                )
            )

    def test_build_worker_service_rejects_short_agent_capability_secret_file(self) -> None:
        secret_file = self.root / "agent-capability.key"
        secret_file.write_bytes(b"too-short")

        with self.assertRaisesRegex(ValueError, "at least 32 bytes"):
            build_worker_service(
                config_from_env(
                    {
                        "PILOT107_WORKER_BACKEND": "in-memory",
                        "PILOT107_AGENT_A1_ENABLED": "true",
                        "PILOT107_AGENT_CAPABILITY_HMAC_SECRET_FILE": str(secret_file),
                    },
                    project_root=self.root,
                )
            )

    def test_build_worker_service_wires_a1_turn_worker_from_secret_file(self) -> None:
        secret_file = self.root / "agent-capability.key"
        secret_file.write_bytes(b"f" * 32)

        service = build_worker_service(
            config_from_env(
                {
                    "PILOT107_WORKER_BACKEND": "in-memory",
                    "PILOT107_AGENTD_URL": "http://pilot-agentd:8091",
                    "PILOT107_AGENTD_TOKEN": "internal-agentd-token",
                    "PILOT107_AGENTD_MODEL_PROFILE": "campus-default",
                    "PILOT107_AGENT_CAPABILITY_HMAC_SECRET_FILE": str(secret_file),
                },
                project_root=self.root,
            )
        )

        self.assertTrue(service.config.agent_a1_enabled)
        self.assertIsNotNone(service.stack.worker.agent_turn_worker)
        self.assertIsNotNone(service.stack.agent_session_service)
        self.assertNotIn("f" * 32, repr(service.config))

    def test_worker_config_loader_never_reads_legacy_llm_settings(self) -> None:
        config = config_from_env(
            _RejectLegacyLlmReads(
                {
                    "PILOT107_AGENTD_URL": "http://pilot-agentd:8091",
                    "PILOT107_AGENTD_TOKEN": "internal-agentd-token",
                    "PILOT107_AGENTD_MODEL_PROFILE": "campus-default",
                }
            ),
            project_root=self.root,
        )

        self.assertEqual(config.agentd_model_profile, "campus-default")

    def test_build_worker_service_enables_agentd_only_with_complete_config(self) -> None:
        configured = build_worker_service(
            config_from_env(
                {
                    "PILOT107_WORKER_BACKEND": "in-memory",
                    "PILOT107_AGENTD_URL": "http://pilot-agentd:8091",
                    "PILOT107_AGENTD_TOKEN": "internal-agentd-token",
                    "PILOT107_AGENTD_MODEL_PROFILE": "campus-default",
                },
                project_root=self.root,
            )
        )
        partial = build_worker_service(
            config_from_env(
                {
                    "PILOT107_WORKER_BACKEND": "in-memory",
                    "PILOT107_AGENTD_URL": "http://pilot-agentd:8091",
                },
                project_root=self.root / "partial",
            )
        )

        provider = configured.stack.remediation_service.advice_service.explain_service.llm_provider
        self.assertIsNotNone(provider)
        assert provider is not None
        self.assertEqual(provider.client.config.model_profile_id, "campus-default")
        self.assertIsNone(
            partial.stack.remediation_service.advice_service.explain_service.llm_provider
        )

    def test_postgres_domain_dsn_also_selects_the_control_plane_database(self) -> None:
        dsn = "postgresql://pilot107.example/pilot107"
        config = config_from_env({"PILOT107_POSTGRES_DSN": dsn}, project_root=self.root)

        self.assertEqual(config.postgres_dsn, dsn)
        self.assertEqual(config.control_postgres_dsn, dsn)

    def test_config_from_env_accepts_explicit_volume_evidence_transport_flag(self) -> None:
        config = config_from_env(
            {
                "PILOT107_ENABLE_DOCKER_VOLUME_EVIDENCE_TRANSPORT": "true",
            },
            project_root=self.root,
        )

        self.assertTrue(config.enable_docker_volume_evidence_transport)

    def test_command_gateway_worker_preflight_is_scoped_to_run_owner(self) -> None:
        worker = build_worker_service(
            config_from_env(
                {
                    "PILOT107_WORKER_BACKEND": "command-gateway",
                    "PILOT107_COMMAND_GATEWAY_URL": "http://gateway.invalid:8090",
                    "PILOT107_COMMAND_GATEWAY_TOKEN": "gateway-token-test",
                },
                project_root=self.root,
            )
        )

        factory = worker.stack.service.preflight_path_checker_factory
        self.assertIsNone(worker.stack.service.preflight_path_checker)
        self.assertIsNotNone(factory)
        assert factory is not None
        checker = factory("bob")
        self.assertIsInstance(checker, SimulatorPathChecker)
        self.assertEqual(checker.user, "bob")
        self.assertEqual(worker.config.command_gateway_token, "gateway-token-test")

    def test_demo_worker_uses_pure_path_preflight_for_simulated_user_home(self) -> None:
        service = build_worker_service(
            config_from_env(
                {
                    "PILOT107_WORKER_BACKEND": "demo",
                    "PILOT107_WORKER_ID": "demo-path-worker",
                },
                project_root=self.root,
            )
        )

        self.assertIsNone(service.stack.service.preflight_path_checker)
        self.assertIsNone(service.stack.service.preflight_path_checker_factory)

    def test_run_once_writes_health_file(self) -> None:
        config = config_from_env(
            {
                "PILOT107_WORKER_BACKEND": "in-memory",
                "PILOT107_WORKER_ID": "worker-health-test",
            },
            project_root=self.root,
        )
        service = build_worker_service(config)

        result = service.run_once()

        self.assertEqual(result.checked, 0)
        self.assertEqual(result.tasks_checked, 0)
        health = json.loads((self.root / "data" / "phase0" / "worker-health.json").read_text())
        self.assertTrue(health["ok"])
        self.assertEqual(health["worker_id"], "worker-health-test")
        self.assertEqual(health["backend"], "in-memory")
        self.assertEqual(health["checked"], 0)
        self.assertEqual(health["remediation_checked"], 0)
        self.assertEqual(health["remediation_advanced"], 0)
        self.assertEqual(health["cumulative_metrics"]["counters"]["ticks_total"], 1)
        self.assertIsNone(health["telemetry_error"])

        restarted = build_worker_service(config)
        restarted.run_once()
        restarted_health = json.loads(
            (self.root / "data" / "phase0" / "worker-health.json").read_text()
        )
        self.assertEqual(
            restarted_health["cumulative_metrics"]["counters"]["ticks_total"],
            2,
        )

    def test_health_redacts_error_messages_and_secret_shaped_fields(self) -> None:
        config = config_from_env(
            {
                "PILOT107_WORKER_BACKEND": "in-memory",
                "PILOT107_WORKER_ID": "worker-redaction-test",
            },
            project_root=self.root,
        )
        service = build_worker_service(config)
        result = WorkerTickResult(
            checked=1,
            terminal=0,
            errors=[
                WorkerRunError(
                    run_id="run-secret",
                    message=(
                        "postgresql://alice:db-password@db/control "
                        "Authorization=Bearer opaque-token"
                    ),
                )
            ],
        )

        service.write_health(result)

        health_text = config.health_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
        self.assertNotIn("db-password", health_text)
        self.assertNotIn("opaque-token", health_text)
        self.assertIn("<redacted>", health_text)

    def test_worker_advances_actionable_remediation_sessions(self) -> None:
        service = build_worker_service(
            config_from_env(
                {
                    "PILOT107_WORKER_BACKEND": "in-memory",
                    "PILOT107_WORKER_ID": "remediation-worker-test",
                },
                project_root=self.root,
            )
        )
        store = service.stack.store
        store.create_run(
            run_id="run_worker_remediation",
            owner="alice",
            workdir="/public/home/alice",
            script="exit 1",
        )
        with store.connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET state = 'FAILED', collection_state = 'succeeded',
                    diagnosis_state = 'skipped', exit_code = '1:0'
                WHERE run_id = 'run_worker_remediation'
                """
            )
        session, _ = service.stack.remediation_service.create(
            owner="alice",
            source_run_id="run_worker_remediation",
            request_key="worker-advance",
        )

        service.run_once()

        updated = service.stack.remediation_service.remediation_store.get_session(
            session.session_id
        )
        self.assertEqual(updated.state, RemediationState.BLOCKED)
        self.assertEqual(updated.stop_reason, "no_safe_action")
        self.assertEqual(service.last_remediation_checked, 1)
        self.assertEqual(service.last_remediation_advanced, 1)

    def test_worker_dispatches_submission_left_pending_by_api_process(self) -> None:
        db_path = self.root / "data" / "phase0" / "pilot107.db"
        store = RunStore(db_path)
        producer = RunService(
            store=store,
            backend=DemoSlurmBackend(),
            control_repository=SQLiteControlRepository(db_path),
            dispatcher_id="api-crashed",
        )
        run = producer.prepare(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\necho durable\n",
                resource_plan=ResourcePlan(
                    partition="debug",
                    qos="normal",
                    nodes=1,
                    ntasks=1,
                    cpus_per_task=1,
                    time_limit="00:05:00",
                ),
            ),
            run_id="run_pending_outbox",
        )
        producer.enqueue_submission(run.run_id)
        worker = build_worker_service(
            config_from_env(
                {
                    "PILOT107_WORKER_BACKEND": "demo",
                    "PILOT107_WORKER_ID": "submission-worker-test",
                    "PILOT107_WORKDIR_PREFLIGHT": "0",
                },
                project_root=self.root,
            )
        )

        result = worker.run_once()

        self.assertEqual(result.submissions_checked, 1)
        self.assertEqual(result.submission_errors, [])
        self.assertEqual(result.submissions_succeeded, 1)
        self.assertEqual(store.get_run(run.run_id).state, RunState.SUCCEEDED)
        health = json.loads((self.root / "data" / "phase0" / "worker-health.json").read_text())
        self.assertEqual(health["submissions_checked"], 1)
        self.assertEqual(health["submissions_succeeded"], 1)

    def test_demo_worker_reconciles_and_collects_evidence(self) -> None:
        db_path = self.root / "data" / "phase0" / "pilot107.db"
        store = RunStore(db_path)
        service = RunService(
            store=store,
            backend=_ReceiptBackend(),
        )
        run = service.submit(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\necho demo\n",
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
        worker = build_worker_service(
            config_from_env(
                {
                    "PILOT107_WORKER_BACKEND": "demo",
                    "PILOT107_WORKER_ID": "demo-worker-test",
                },
                project_root=self.root,
            )
        )

        for _ in range(10):
            worker.run_once()
            final = store.get_run(run.run_id)
            if (
                final.state == RunState.SUCCEEDED
                and final.collection_state == CollectionState.SUCCEEDED
            ):
                break

        final = store.get_run(run.run_id)
        objects = store.list_evidence_objects(run.run_id)
        paths = {obj.logical_path for obj in objects}
        self.assertEqual(final.state, RunState.SUCCEEDED)
        self.assertEqual(final.collection_state, CollectionState.SUCCEEDED)
        self.assertIn("derived/result_summary.v1.json", paths)
        self.assertIn("outputs/inventory.json", paths)


class _RejectLegacyLlmReads(dict[str, str]):
    def get(self, key: str, default=None):
        if key.startswith("PILOT107_LLM_"):
            raise AssertionError(f"legacy LLM setting was read: {key}")
        return super().get(key, default)


class _ReceiptBackend:
    def submit(self, intent: Any) -> SubmitReceipt:
        return SubmitReceipt(
            job_id="demo-test-job",
            run_state=RunState.SUBMITTED,
            strategy=SubmissionStrategy.DEMO,
            raw_response={"job_id": "demo-test-job"},
        )

    def get_job(self, *, user: str, job_id: str) -> NoReturn:
        raise AssertionError("not used")

    def cancel(self, *, user: str, job_id: str) -> NoReturn:
        raise AssertionError("not used")


if __name__ == "__main__":
    unittest.main()
