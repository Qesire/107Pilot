import unittest
from pathlib import Path

from pilot107.adapters.slurm import (
    CommandResult,
    DemoSlurmBackend,
    InMemorySlurmBackend,
    SimulatorPathChecker,
    SlurmAuthError,
    SlurmSubmissionRejected,
    SubmitIntent,
)
from pilot107.core.resources import ResourcePlan
from pilot107.core.states import RunState


def _valid_plan() -> ResourcePlan:
    return ResourcePlan(
        partition="debug",
        qos="normal",
        nodes=1,
        ntasks=1,
        cpus_per_task=1,
        time_limit="00:05:00",
    )


class InMemorySlurmBackendTests(unittest.TestCase):
    def test_submit_and_read_job(self) -> None:
        backend = InMemorySlurmBackend()

        receipt = backend.submit(
            SubmitIntent(
                user="alice",
                workdir=Path("/public/home/alice/run-1"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_valid_plan(),
            )
        )
        snapshot = backend.get_job(user="alice", job_id=receipt.job_id)

        self.assertEqual(receipt.run_state, RunState.PENDING)
        self.assertEqual(snapshot.owner, "alice")
        self.assertEqual(snapshot.raw_state_flags, ["PENDING"])

    def test_reject_invalid_resource_plan(self) -> None:
        backend = InMemorySlurmBackend()

        with self.assertRaises(SlurmSubmissionRejected):
            backend.submit(
                SubmitIntent(
                    user="alice",
                    workdir=Path("/public/home/alice/run-1"),
                    script="#!/bin/bash\nhostname\n",
                    resource_plan=ResourcePlan(
                        partition="debug",
                        qos="normal",
                        nodes=1,
                        ntasks=1,
                        cpus_per_task=1,
                    ),
                )
            )

    def test_idempotency_replays_same_job(self) -> None:
        backend = InMemorySlurmBackend()
        intent = SubmitIntent(
            user="alice",
            workdir=Path("/public/home/alice/run-1"),
            script="#!/bin/bash\nhostname\n",
            resource_plan=_valid_plan(),
            idempotency_key="run-1-submit",
        )

        first = backend.submit(intent)
        second = backend.submit(intent)

        self.assertEqual(first.job_id, second.job_id)
        self.assertTrue(second.raw_response["idempotent_replay"])

    def test_user_cannot_read_another_users_job(self) -> None:
        backend = InMemorySlurmBackend()
        receipt = backend.submit(
            SubmitIntent(
                user="alice",
                workdir=Path("/public/home/alice/run-1"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_valid_plan(),
            )
        )

        with self.assertRaises(SlurmAuthError):
            backend.get_job(user="bob", job_id=receipt.job_id)

    def test_cancel_sets_cancelled_state(self) -> None:
        backend = InMemorySlurmBackend()
        receipt = backend.submit(
            SubmitIntent(
                user="alice",
                workdir=Path("/public/home/alice/run-1"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_valid_plan(),
            )
        )

        snapshot = backend.cancel(user="alice", job_id=receipt.job_id)

        self.assertEqual(snapshot.run_state, RunState.CANCELLED)


class DemoSlurmBackendTests(unittest.TestCase):
    def test_demo_submit_reconciles_without_shared_memory(self) -> None:
        backend = DemoSlurmBackend()
        receipt = backend.submit(
            SubmitIntent(
                user="alice",
                workdir=Path("/public/home/alice/run-1"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_valid_plan(),
                idempotency_key="run-demo:submit",
            )
        )

        fresh_backend = DemoSlurmBackend()
        snapshot = fresh_backend.get_job(user="alice", job_id=receipt.job_id)

        self.assertTrue(receipt.job_id.startswith("demo-"))
        self.assertEqual(receipt.strategy, "demo")
        self.assertEqual(snapshot.owner, "alice")
        self.assertEqual(snapshot.run_state, RunState.SUCCEEDED)
        self.assertEqual(snapshot.exit_code, "0:0")

    def test_demo_cancel(self) -> None:
        backend = DemoSlurmBackend()
        receipt = backend.submit(
            SubmitIntent(
                user="alice",
                workdir=Path("/public/home/alice/run-1"),
                script="#!/bin/bash\nhostname\n",
                resource_plan=_valid_plan(),
            )
        )

        snapshot = backend.cancel(user="alice", job_id=receipt.job_id)

        self.assertEqual(snapshot.run_state, RunState.CANCELLED)


class SimulatorPathCheckerTests(unittest.TestCase):
    def test_probes_path_as_requested_slurm_user(self) -> None:
        executor = _PathExecutor({("-e", "/public/home/alice"): True})
        checker = SimulatorPathChecker(executor=executor, user="alice", timeout_seconds=3)

        self.assertTrue(checker.exists("/public/home/alice"))
        self.assertFalse(checker.readable("/public/home/alice/private"))
        self.assertEqual(
            executor.calls,
            [
                (["test", "-e", "/public/home/alice"], "alice", 3),
                (["test", "-r", "/public/home/alice/private"], "alice", 3),
            ],
        )


class _PathExecutor:
    def __init__(self, outcomes: dict[tuple[str, str], bool]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[list[str], str | None, float]] = []

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        user: str | None = None,
        stdin: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> CommandResult:
        self.calls.append((argv, user, timeout_seconds))
        return CommandResult(
            returncode=0 if self.outcomes.get((argv[1], argv[2]), False) else 1,
            stdout="",
            stderr="",
        )

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


if __name__ == "__main__":
    unittest.main()
