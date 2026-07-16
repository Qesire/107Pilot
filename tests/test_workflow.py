import tempfile
import unittest
from pathlib import Path

from pilot107.adapters.slurm import InMemorySlurmBackend, SlurmBackendError
from pilot107.core.resources import ResourcePlan
from pilot107.core.run_service import (
    RunService,
    RunSubmitRequest,
    SubmissionInProgressError,
    WorkflowDependencyError,
    WorkflowPolicy,
    WorkflowRetryNotReadyError,
)
from pilot107.core.run_store import RunStore
from pilot107.core.states import RunState


class WorkflowServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self._tmp.name) / "pilot107.db")
        self.backend = InMemorySlurmBackend()
        self.service = RunService(store=self.store, backend=self.backend)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_active_dependency_is_submitted_as_afterok_job_reference(self) -> None:
        parent = self.service.submit(_request())

        child = self.service.submit(
            _request(workflow=WorkflowPolicy(dependencies=(parent.run_id,)))
        )

        self.assertEqual(child.state, RunState.SUBMITTED)
        self.assertEqual(child.submit_response["dependency_job_ids"], [parent.job_id])
        event = next(
            item
            for item in self.store.list_events(child.run_id)
            if item.event_type == "workflow.dependencies_resolved"
        )
        self.assertEqual(event.payload["dependency_run_ids"], [parent.run_id])

    def test_succeeded_dependency_does_not_add_scheduler_dependency(self) -> None:
        parent = self.service.submit(_request())
        self.backend.advance_job(
            job_id=parent.job_id or "",
            raw_state="COMPLETED",
            exit_code="0:0",
        )
        self.service.reconcile_once(parent.run_id)

        child = self.service.submit(
            _request(workflow=WorkflowPolicy(dependencies=(parent.run_id,)))
        )

        self.assertEqual(child.submit_response["dependency_job_ids"], [])

    def test_failed_and_cross_owner_dependencies_are_blocked(self) -> None:
        failed = self.service.submit(_request())
        self.backend.advance_job(
            job_id=failed.job_id or "",
            raw_state="FAILED",
            exit_code="1:0",
        )
        self.service.reconcile_once(failed.run_id)
        failed_child = self.service.prepare(
            _request(workflow=WorkflowPolicy(dependencies=(failed.run_id,)))
        )
        other_owner = self.service.prepare(_request(owner="bob"))
        cross_owner = self.service.prepare(
            _request(owner="alice", workflow=WorkflowPolicy(dependencies=(other_owner.run_id,)))
        )

        with self.assertRaisesRegex(WorkflowDependencyError, "did not succeed"):
            self.service.submit_prepared(failed_child.run_id)
        with self.assertRaisesRegex(WorkflowDependencyError, "owner"):
            self.service.submit_prepared(cross_owner.run_id)

    def test_bounded_auto_retry_is_scheduled_and_submitted_once(self) -> None:
        workflow = WorkflowPolicy(
            max_attempts=2,
            backoff_seconds=0,
            automation_level="bounded_auto",
            require_approval=False,
        )
        root = self.service.submit(_request(workflow=workflow))
        self.backend.advance_job(
            job_id=root.job_id or "",
            raw_state="FAILED",
            exit_code="1:0",
        )

        self.service.reconcile_once(root.run_id)
        self.service.reconcile_once(root.run_id)
        children = self.store.list_child_runs(root.run_id)
        submitted = self.service.submit_due_workflow_retries()

        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].lineage_reason, "workflow_retry")
        self.assertEqual(children[0].attempt, 1)
        self.assertEqual([item.run_id for item in submitted], [children[0].run_id])

        retry = submitted[0]
        self.backend.advance_job(
            job_id=retry.job_id or "",
            raw_state="FAILED",
            exit_code="1:0",
        )
        self.service.reconcile_once(retry.run_id)

        self.assertEqual(self.store.list_child_runs(retry.run_id), [])
        self.assertEqual(
            self.store.list_events(retry.run_id)[-1].event_type,
            "workflow.retry_exhausted",
        )

    def test_retry_requires_bounded_auto_policy(self) -> None:
        root = self.service.submit(
            _request(
                workflow=WorkflowPolicy(
                    max_attempts=2,
                    automation_level="suggest",
                    require_approval=True,
                )
            )
        )
        self.backend.advance_job(
            job_id=root.job_id or "",
            raw_state="FAILED",
            exit_code="1:0",
        )

        self.service.reconcile_once(root.run_id)

        self.assertEqual(self.store.list_child_runs(root.run_id), [])
        self.assertEqual(
            self.store.list_events(root.run_id)[-1].event_type,
            "workflow.retry_approval_required",
        )

    def test_retry_backoff_prevents_early_submission(self) -> None:
        root = self.service.submit(
            _request(
                workflow=WorkflowPolicy(
                    max_attempts=2,
                    backoff_seconds=60,
                    automation_level="bounded_auto",
                    require_approval=False,
                )
            )
        )
        self.backend.advance_job(
            job_id=root.job_id or "",
            raw_state="FAILED",
            exit_code="1:0",
        )
        self.service.reconcile_once(root.run_id)
        retry = self.store.list_child_runs(root.run_id)[0]

        self.assertEqual(self.store.list_due_workflow_retries(), [])
        with self.assertRaises(WorkflowRetryNotReadyError):
            self.service.submit_prepared(retry.run_id)

    def test_idempotent_prepare_rejects_changed_workflow(self) -> None:
        self.service.prepare(_request(), run_id="run_deterministic", idempotent=True)

        with self.assertRaisesRegex(SlurmBackendError, "different content"):
            self.service.prepare(
                _request(
                    workflow=WorkflowPolicy(
                        max_attempts=2,
                        automation_level="bounded_auto",
                        require_approval=False,
                    )
                ),
                run_id="run_deterministic",
                idempotent=True,
            )

    def test_submit_rejects_a_run_already_claimed_by_another_worker(self) -> None:
        run = self.service.prepare(_request())
        self.assertTrue(self.store.claim_submission(run.run_id))

        with self.assertRaises(SubmissionInProgressError):
            self.service.submit_prepared(run.run_id)


def _request(
    *,
    owner: str = "alice",
    workflow: WorkflowPolicy | None = None,
) -> RunSubmitRequest:
    return RunSubmitRequest(
        owner=owner,
        workdir=Path(f"/public/home/{owner}"),
        script="#!/bin/bash\necho workflow\n",
        resource_plan=ResourcePlan(
            partition="debug",
            qos="normal",
            nodes=1,
            ntasks=1,
            cpus_per_task=1,
            time_limit="00:05:00",
        ),
        workflow=workflow or WorkflowPolicy(),
    )


if __name__ == "__main__":
    unittest.main()
