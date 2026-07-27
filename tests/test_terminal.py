import json
import tempfile
import unittest
from pathlib import Path

from pilot107.adapters.slurm import CommandResult, InMemorySlurmBackend
from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.http_app import Pilot107HttpApi
from pilot107.core.resources import ResourcePlan
from pilot107.core.run_service import RunService, RunSubmitRequest
from pilot107.core.run_store import RunStore
from pilot107.core.terminal import TerminalCommandError, TerminalCommandService
from pilot107.worker.evidence import EvidenceStore


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        user: str | None = None,
        stdin: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> CommandResult:
        self.calls.append((argv, user))
        return CommandResult(returncode=0, stdout="diagnostic output\n", stderr="")

    def realpath(self, path: str, *, timeout_seconds: float = 10.0) -> str:
        return path

    def write_text(
        self,
        *,
        path: str,
        content: str,
        owner: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        return None


class TerminalCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = RunStore(root / "pilot107.db")
        self.evidence_store = EvidenceStore(root / "evidence")
        self.executor = RecordingExecutor()
        self.service = TerminalCommandService(executor=self.executor)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_catalog_executes_structured_identity_command_as_current_user(self) -> None:
        result = self.service.execute(command="identity", user="alice")

        self.assertEqual(result.argv, ("id",))
        self.assertEqual(self.executor.calls, [(["id"], "alice")])

    def test_run_status_requires_a_submitted_run(self) -> None:
        with self.assertRaisesRegex(TerminalCommandError, "submitted Run"):
            self.service.execute(command="run_status", user="alice")

    def test_http_route_binds_run_to_authenticated_owner(self) -> None:
        run_service = RunService(store=self.store, backend=InMemorySlurmBackend())
        run = run_service.submit(
            RunSubmitRequest(
                owner="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\ntrue\n",
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
            store=self.store,
            evidence_query=EvidenceQueryService(
                store=self.store,
                evidence_store=self.evidence_store,
            ),
            terminal_service=self.service,
            auth_required=True,
        )

        response = api.handle_post(
            "/api/v1/terminal/commands",
            body=json.dumps({"command": "run_status", "run_id": run.run_id}).encode(),
            headers={"X-Pilot107-User": "alice"},
        )
        forbidden = api.handle_post(
            "/api/v1/terminal/commands",
            body=json.dumps({"command": "run_status", "run_id": run.run_id}).encode(),
            headers={"X-Pilot107-User": "bob"},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["command"], "run_status")
        self.assertEqual(self.executor.calls[-1][0][0], "sacct")
        self.assertEqual(forbidden.status, 403)
