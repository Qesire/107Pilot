import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, NoReturn

from pilot107.adapters.slurm import SimulatorPathChecker, SubmissionStrategy, SubmitReceipt
from pilot107.core.resources import ResourcePlan
from pilot107.core.run_service import RunService, RunSubmitRequest
from pilot107.core.run_store import RunStore
from pilot107.core.states import CollectionState, RunState
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
        self.assertEqual(config.backend, "docker-compose-command")
        self.assertEqual(config.allowed_roots, ("/public/home/alice",))
        self.assertEqual(config.compose_file, self.root / "simulator" / "compose" / "compose.yml")
        self.assertEqual(config.health_path, self.root / "data" / "phase0" / "worker-health.json")

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
        self.assertFalse(config.enable_docker_volume_evidence_transport)

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
