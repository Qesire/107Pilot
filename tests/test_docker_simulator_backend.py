import unittest
from pathlib import Path

from pilot107.adapters.slurm import (
    CommandResult,
    DockerComposeExecutor,
    DockerComposeTarget,
    DockerSimulatorCommandBackend,
    SlurmSubmissionRejected,
    SlurmTransportError,
    SubmitIntent,
)
from pilot107.core.resources import ResourcePlan
from pilot107.core.states import RunState


class FakeDockerExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None, str | None]] = []
        self.writes: list[tuple[str, str, str]] = []
        self.squeue_output = ""
        self.sacct_output = "9001|alice|COMPLETED|0:0\n"

    def realpath(self, path: str, *, timeout_seconds: float = 10.0) -> str:
        parts: list[str] = []
        for part in path.split("/"):
            if part in {"", "."}:
                continue
            if part == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(part)
        return "/" + "/".join(parts)

    def write_text(
        self,
        *,
        path: str,
        content: str,
        owner: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.writes.append((path, content, owner))

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        user: str | None = None,
        stdin: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> CommandResult:
        self.calls.append((argv, cwd, user))
        match argv[0]:
            case "sbatch":
                return CommandResult(0, "9001\n", "")
            case "squeue":
                return CommandResult(0, self.squeue_output, "")
            case "sacct":
                return CommandResult(0, self.sacct_output, "")
            case "scancel":
                return CommandResult(0, "", "")
            case _:
                return CommandResult(1, "", "unexpected command")


def _plan() -> ResourcePlan:
    return ResourcePlan(
        partition="debug",
        qos="normal",
        nodes=1,
        ntasks=1,
        cpus_per_task=1,
        time_limit="00:05:00",
    )


class DockerComposeExecutorTests(unittest.TestCase):
    def test_build_exec_argv_preserves_structured_arguments(self) -> None:
        executor = DockerComposeExecutor(
            DockerComposeTarget(
                compose_file=Path("/repo/simulator/compose/compose.yml"),
                env_file=Path("/repo/simulator/compose/.env.example"),
                workdir=Path("/repo/simulator/compose"),
            )
        )

        argv = executor.build_exec_argv(
            ["sacct", "-j", "1"], cwd="/public/home/alice", user="alice"
        )

        self.assertEqual(argv[:2], ["docker", "compose"])
        self.assertIn("--env-file", argv)
        self.assertIn("--workdir", argv)
        self.assertIn("--user", argv)
        self.assertEqual(argv[-4:], ["login-node-sim", "sacct", "-j", "1"])


class DockerSimulatorCommandBackendTests(unittest.TestCase):
    def test_submit_stages_script_in_container_and_runs_as_user(self) -> None:
        executor = FakeDockerExecutor()
        backend = DockerSimulatorCommandBackend(
            executor=executor,  # type: ignore[arg-type]
            allowed_roots=["/public/home/alice"],
        )

        receipt = backend.submit(
            SubmitIntent(
                user="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_plan(),
            )
        )

        self.assertEqual(receipt.job_id, "9001")
        self.assertTrue(executor.writes[0][0].startswith("/public/home/alice/pilot107-submit-"))
        self.assertTrue(executor.writes[0][0].endswith(".sbatch"))
        self.assertEqual(executor.writes[0][2], "alice")
        self.assertEqual(executor.calls[0][0][0], "sbatch")
        self.assertEqual(executor.calls[0][2], "alice")

    def test_submit_uses_unique_script_path_for_idempotency_key(self) -> None:
        executor = FakeDockerExecutor()
        backend = DockerSimulatorCommandBackend(
            executor=executor,  # type: ignore[arg-type]
            allowed_roots=["/public/home/alice"],
        )

        backend.submit(
            SubmitIntent(
                user="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\necho one\n",
                resource_plan=_plan(),
                idempotency_key="run-one:submit",
            )
        )
        backend.submit(
            SubmitIntent(
                user="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\necho two\n",
                resource_plan=_plan(),
                idempotency_key="run-two:submit",
            )
        )

        self.assertNotEqual(executor.writes[0][0], executor.writes[1][0])
        self.assertIn("run-one_submit", executor.writes[0][0])
        self.assertIn("run-two_submit", executor.writes[1][0])

    def test_submit_passes_explicit_job_name_to_container_argv(self) -> None:
        executor = FakeDockerExecutor()
        backend = DockerSimulatorCommandBackend(
            executor=executor,  # type: ignore[arg-type]
            allowed_roots=["/public/home/alice"],
        )

        backend.submit(
            SubmitIntent(
                user="alice",
                workdir=Path("/public/home/alice"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_plan(),
                job_name="pilot107-run-0123456789abcdef",
            )
        )

        argv = executor.calls[0][0]
        name_index = argv.index("--job-name")
        self.assertEqual(argv[name_index + 1], "pilot107-run-0123456789abcdef")

    def test_submit_rejects_container_path_outside_allowed_roots(self) -> None:
        executor = FakeDockerExecutor()
        backend = DockerSimulatorCommandBackend(
            executor=executor,  # type: ignore[arg-type]
            allowed_roots=["/public/home/alice"],
        )

        with self.assertRaises(SlurmSubmissionRejected):
            backend.submit(
                SubmitIntent(
                    user="alice",
                    workdir=Path("/public/home/bob"),
                    script="#!/bin/bash\nhostname\n",
                    resource_plan=_plan(),
                )
            )

    def test_owner_scoped_root_template_rejects_another_users_home(self) -> None:
        executor = FakeDockerExecutor()
        backend = DockerSimulatorCommandBackend(
            executor=executor,  # type: ignore[arg-type]
            allowed_roots=["/public/home/{user}"],
        )

        with self.assertRaises(SlurmSubmissionRejected):
            backend.submit(
                SubmitIntent(
                    user="alice",
                    workdir=Path("/public/home/bob"),
                    script="#!/bin/bash\nhostname\n",
                    resource_plan=_plan(),
                )
            )

        backend.submit(
            SubmitIntent(
                user="bob",
                workdir=Path("/public/home/bob"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_plan(),
            )
        )
        self.assertEqual(executor.writes[-1][2], "bob")

    def test_get_finished_job_uses_accounting(self) -> None:
        executor = FakeDockerExecutor()
        backend = DockerSimulatorCommandBackend(
            executor=executor,  # type: ignore[arg-type]
            allowed_roots=["/public/home/alice"],
        )

        snapshot = backend.get_job(user="alice", job_id="9001")

        self.assertEqual(snapshot.run_state, RunState.SUCCEEDED)
        self.assertEqual(snapshot.owner, "alice")
        self.assertEqual(snapshot.exit_code, "0:0")

    def test_get_job_aggregates_array_task_rows_under_parent_job_id(self) -> None:
        executor = FakeDockerExecutor()
        executor.squeue_output = (
            "9001_0|alice|RUNNING|anode16\n"
            "9001_[1-3]|alice|PENDING|Dependency\n"
        )
        backend = DockerSimulatorCommandBackend(
            executor=executor,  # type: ignore[arg-type]
            allowed_roots=["/public/home/alice"],
        )

        snapshot = backend.get_job(user="alice", job_id="9001")

        self.assertEqual(snapshot.job_id, "9001")
        self.assertEqual(snapshot.run_state, RunState.RUNNING)
        self.assertEqual(snapshot.raw_state_flags, ["RUNNING", "PENDING"])

    def test_get_finished_job_aggregates_array_accounting_rows(self) -> None:
        executor = FakeDockerExecutor()
        executor.sacct_output = (
            "9001_0|alice|COMPLETED|0:0\n"
            "9001_1|alice|COMPLETED|0:0\n"
        )
        backend = DockerSimulatorCommandBackend(
            executor=executor,  # type: ignore[arg-type]
            allowed_roots=["/public/home/alice"],
        )

        snapshot = backend.get_job(user="alice", job_id="9001")

        self.assertEqual(snapshot.run_state, RunState.SUCCEEDED)
        self.assertEqual(snapshot.exit_code, "0:0")
        self.assertIn("JobID,User,State,ExitCode", executor.calls[-1][0])

    def test_get_finished_job_treats_empty_accounting_owner_as_retryable(self) -> None:
        executor = FakeDockerExecutor()
        executor.sacct_output = "9001||COMPLETED|0:0\n"
        backend = DockerSimulatorCommandBackend(
            executor=executor,  # type: ignore[arg-type]
            allowed_roots=["/public/home/alice"],
        )

        with self.assertRaises(SlurmTransportError):
            backend.get_job(user="alice", job_id="9001")

    def test_cancel_runs_as_owner(self) -> None:
        executor = FakeDockerExecutor()
        executor.squeue_output = "9001|alice|RUNNING|None\n"
        backend = DockerSimulatorCommandBackend(
            executor=executor,  # type: ignore[arg-type]
            allowed_roots=["/public/home/alice"],
        )

        snapshot = backend.cancel(user="alice", job_id="9001")

        self.assertEqual(snapshot.run_state, RunState.CANCELLED)
        self.assertEqual(executor.calls[-1][0], ["scancel", "9001"])
        self.assertEqual(executor.calls[-1][2], "alice")


if __name__ == "__main__":
    unittest.main()
