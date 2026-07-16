import unittest
from pathlib import Path

from pilot107.adapters.slurm import (
    InMemorySlurmBackend,
    SlurmAuthError,
    SlurmControlBackend,
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


def _exercise_slurm_control_contract(backend: SlurmControlBackend) -> None:
    intent = SubmitIntent(
        user="alice",
        workdir=Path("/public/home/alice/run-contract"),
        script="#!/bin/bash\nhostname\n",
        resource_plan=_valid_plan(),
        idempotency_key="contract-run-submit",
    )

    receipt = backend.submit(intent)
    replay = backend.submit(intent)
    snapshot = backend.get_job(user="alice", job_id=receipt.job_id)

    assert receipt.job_id
    assert replay.job_id == receipt.job_id
    assert snapshot.owner == "alice"
    assert snapshot.run_state in {RunState.PENDING, RunState.SUBMITTED, RunState.RUNNING}

    try:
        backend.get_job(user="bob", job_id=receipt.job_id)
    except SlurmAuthError:
        pass
    else:
        raise AssertionError("backend allowed cross-user job read")

    cancelled = backend.cancel(user="alice", job_id=receipt.job_id)
    assert cancelled.run_state == RunState.CANCELLED


class SlurmBackendContractTests(unittest.TestCase):
    def test_in_memory_backend_satisfies_slurm_control_contract(self) -> None:
        _exercise_slurm_control_contract(InMemorySlurmBackend())


if __name__ == "__main__":
    unittest.main()
