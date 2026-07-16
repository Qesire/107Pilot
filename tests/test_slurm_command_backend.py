import tempfile
import unittest
from pathlib import Path

from pilot107.adapters.slurm import (
    CommandResult,
    CommandSubmitBackend,
    SlurmAuthError,
    SlurmSubmissionRejected,
    SubmitIntent,
)
from pilot107.core.resources import ResourcePlan
from pilot107.core.states import RunState


class FakeRunner:
    def __init__(self, results: list[CommandResult]) -> None:
        self.results = results
        self.calls: list[list[str]] = []

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        timeout_seconds: float,
    ) -> CommandResult:
        self.calls.append(argv)
        return self.results.pop(0)


def _plan(partition: str = "debug") -> ResourcePlan:
    return ResourcePlan(
        partition=partition,
        qos="normal",
        nodes=1,
        ntasks=1,
        cpus_per_task=1,
        time_limit="00:05:00",
    )


class CommandSubmitBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workdir = self.root / "public" / "home" / "alice" / "run-1"
        self.workdir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_submit_uses_argv_without_shell(self) -> None:
        runner = FakeRunner([CommandResult(0, "4321\n", "")])
        backend = CommandSubmitBackend(allowed_roots=[self.root], runner=runner)

        receipt = backend.submit(
            SubmitIntent(
                user="alice",
                workdir=self.workdir,
                script="#!/bin/bash\nhostname\n",
                resource_plan=_plan(),
            )
        )

        self.assertEqual(receipt.job_id, "4321")
        argv = runner.calls[0]
        self.assertEqual(argv[0], "sbatch")
        self.assertIn("--parsable", argv)
        self.assertNotIn(";", argv)
        staged_scripts = list(self.workdir.glob("pilot107-submit-*.sbatch"))
        self.assertEqual(len(staged_scripts), 1)

    def test_submit_passes_afterok_dependencies_as_argv(self) -> None:
        runner = FakeRunner([CommandResult(0, "4322\n", "")])
        backend = CommandSubmitBackend(allowed_roots=[self.root], runner=runner)

        backend.submit(
            SubmitIntent(
                user="alice",
                workdir=self.workdir,
                script="#!/bin/bash\nhostname\n",
                resource_plan=_plan(),
                dependency_job_ids=("120", "121"),
            )
        )

        argv = runner.calls[0]
        dependency_index = argv.index("--dependency")
        self.assertEqual(argv[dependency_index + 1], "afterok:120:121")

    def test_rejects_workdir_outside_allowed_roots(self) -> None:
        runner = FakeRunner([CommandResult(0, "4321\n", "")])
        backend = CommandSubmitBackend(allowed_roots=[self.root / "other"], runner=runner)

        with self.assertRaises((SlurmSubmissionRejected, OSError)):
            backend.submit(
                SubmitIntent(
                    user="alice",
                    workdir=self.workdir,
                    script="#!/bin/bash\nhostname\n",
                    resource_plan=_plan(),
                )
            )

    def test_rejects_unsafe_partition_value(self) -> None:
        runner = FakeRunner([CommandResult(0, "4321\n", "")])
        backend = CommandSubmitBackend(allowed_roots=[self.root], runner=runner)

        with self.assertRaises(SlurmSubmissionRejected):
            backend.submit(
                SubmitIntent(
                    user="alice",
                    workdir=self.workdir,
                    script="#!/bin/bash\nhostname\n",
                    resource_plan=_plan("debug;rm"),
                )
            )

    def test_get_job_from_squeue(self) -> None:
        runner = FakeRunner([CommandResult(0, "4321|alice|RUNNING|None\n", "")])
        backend = CommandSubmitBackend(allowed_roots=[self.root], runner=runner)

        snapshot = backend.get_job(user="alice", job_id="4321")

        self.assertEqual(snapshot.run_state, RunState.RUNNING)
        self.assertEqual(runner.calls[0][0], "squeue")

    def test_get_job_falls_back_to_sacct(self) -> None:
        runner = FakeRunner(
            [
                CommandResult(0, "", ""),
                CommandResult(0, "4321|alice|COMPLETED|0:0\n", ""),
            ]
        )
        backend = CommandSubmitBackend(allowed_roots=[self.root], runner=runner)

        snapshot = backend.get_job(user="alice", job_id="4321")

        self.assertEqual(snapshot.run_state, RunState.SUCCEEDED)
        self.assertEqual(snapshot.exit_code, "0:0")
        self.assertEqual(runner.calls[1][0], "sacct")

    def test_get_job_rejects_foreign_owner(self) -> None:
        runner = FakeRunner([CommandResult(0, "4321|bob|RUNNING|None\n", "")])
        backend = CommandSubmitBackend(allowed_roots=[self.root], runner=runner)

        with self.assertRaises(SlurmAuthError):
            backend.get_job(user="alice", job_id="4321")

    def test_cancel_checks_owner_then_scancel(self) -> None:
        runner = FakeRunner(
            [
                CommandResult(0, "4321|alice|RUNNING|None\n", ""),
                CommandResult(0, "", ""),
            ]
        )
        backend = CommandSubmitBackend(allowed_roots=[self.root], runner=runner)

        snapshot = backend.cancel(user="alice", job_id="4321")

        self.assertEqual(snapshot.run_state, RunState.CANCELLED)
        self.assertEqual(runner.calls[1], ["scancel", "4321"])


if __name__ == "__main__":
    unittest.main()
