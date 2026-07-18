import sqlite3
import tempfile
import unittest
from pathlib import Path

from pilot107.adapters.slurm import JobSnapshot, SubmissionStrategy, SubmitReceipt
from pilot107.core.run_store import (
    CollectionTaskFenceConflict,
    RunStore,
    RunStoreFenceConflict,
)
from pilot107.core.states import CollectionState, RunState


class RunStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self._tmp.name) / "pilot107.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_run_lineage_tracks_attempts_and_children(self) -> None:
        root = self.store.create_run(
            run_id="run_root",
            owner="alice",
            workdir="/public/home/alice",
            script="echo root",
        )
        child = self.store.create_run(
            run_id="run_child",
            owner="alice",
            workdir="/public/home/alice",
            script="echo child",
            parent_run_id=root.run_id,
            lineage_reason="manual_retry",
        )
        grandchild = self.store.create_run(
            run_id="run_grandchild",
            owner="alice",
            workdir="/public/home/alice",
            script="echo grandchild",
            parent_run_id=child.run_id,
            lineage_reason="manual_retry",
        )

        self.assertEqual([root.attempt, child.attempt, grandchild.attempt], [0, 1, 2])
        self.assertEqual(
            [run.run_id for run in self.store.list_run_lineage(grandchild.run_id)],
            ["run_root", "run_child", "run_grandchild"],
        )
        self.assertEqual(
            [run.run_id for run in self.store.list_child_runs(root.run_id)],
            ["run_child"],
        )
        self.assertIsNone(self.store.list_events(child.run_id)[0].payload["remediation_plan_id"])

    def test_migrates_existing_run_with_workflow_defaults(self) -> None:
        db_path = Path(self._tmp.name) / "legacy.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE runs (
                    run_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    state TEXT NOT NULL,
                    collection_state TEXT NOT NULL,
                    diagnosis_state TEXT NOT NULL,
                    capsule_state TEXT NOT NULL,
                    result_status TEXT NOT NULL,
                    job_id TEXT,
                    workdir TEXT NOT NULL,
                    script TEXT NOT NULL,
                    exit_code TEXT,
                    terminal_state TEXT,
                    submit_strategy TEXT,
                    submit_response_json TEXT NOT NULL DEFAULT '{}',
                    resource_plan_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, owner, state, collection_state, diagnosis_state,
                    capsule_state, result_status, workdir, script, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "run_legacy",
                    "alice",
                    "VALIDATED",
                    "pending",
                    "pending",
                    "pending",
                    "UNKNOWN",
                    "/public/home/alice",
                    "echo legacy",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )

        run = RunStore(db_path).get_run("run_legacy")

        self.assertEqual(run.workflow, {})
        self.assertIsNone(run.retry_not_before)

    def test_submission_claim_is_atomic(self) -> None:
        run = self.store.create_run(
            run_id="run_claim",
            owner="alice",
            workdir="/public/home/alice",
            script="echo once",
        )

        first = self.store.claim_submission(run.run_id)
        second = self.store.claim_submission(run.run_id)

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(self.store.get_run(run.run_id).state, RunState.SUBMITTING)
        self.assertEqual(
            [event.event_type for event in self.store.list_events(run.run_id)].count(
                "run.submitting"
            ),
            1,
        )

    def test_submission_result_rejects_stale_fencing_token_after_reclaim(self) -> None:
        run = self.store.create_run(
            run_id="run_fenced_claim",
            owner="alice",
            workdir="/public/home/alice",
            script="echo once",
        )
        self.assertTrue(
            self.store.claim_submission(
                run.run_id,
                lease_owner="worker-a",
                fencing_token=1,
            )
        )
        self.assertFalse(
            self.store.claim_submission(
                run.run_id,
                lease_owner="worker-b",
                fencing_token=1,
            )
        )
        self.assertTrue(
            self.store.claim_submission(
                run.run_id,
                lease_owner="worker-b",
                fencing_token=2,
            )
        )
        receipt = SubmitReceipt(
            job_id="1234",
            run_state=RunState.SUBMITTED,
            strategy=SubmissionStrategy.IN_MEMORY,
            raw_response={"job_id": "1234"},
        )
        with self.assertRaises(RunStoreFenceConflict):
            self.store.apply_submit_receipt(
                run.run_id,
                receipt,
                lease_owner="worker-a",
                fencing_token=1,
            )

        submitted = self.store.apply_submit_receipt(
            run.run_id,
            receipt,
            lease_owner="worker-b",
            fencing_token=2,
        )
        self.assertEqual(submitted.job_id, "1234")

    def test_agent_remediation_requires_approved_action_for_parent(self) -> None:
        parent = self.store.create_run(
            run_id="run_parent",
            owner="alice",
            workdir="/public/home/alice",
            script="echo root",
        )
        self.store.create_agent_advice(
            advice_id="advice_1",
            run_id=parent.run_id,
            owner="alice",
            request_key="request_1",
            state="ready",
            source_run_updated_at=parent.updated_at,
            evidence_bundle_sha256="evidence",
            provider="none",
            model=None,
            payload={
                "actions": [
                    {
                        "action_id": "action_1",
                        "policy_status": "allowed_preview",
                    }
                ]
            },
        )
        self.store.decide_agent_advice(
            advice_id="advice_1",
            expected_version=1,
            expected_state="ready",
            new_state="approved",
            decision="approve",
            actor="alice",
            action_ids=["action_1"],
            note=None,
        )

        child = self.store.create_run(
            run_id="run_remediated",
            owner="alice",
            workdir="/public/home/alice",
            script="echo fixed",
            parent_run_id=parent.run_id,
            lineage_reason="agent_remediation",
            remediation_plan_id="advice_1:action_1",
        )

        self.assertEqual(child.remediation_plan_id, "advice_1:action_1")

    def test_agent_remediation_rejects_unverifiable_reference(self) -> None:
        parent = self.store.create_run(
            run_id="run_parent_unverified",
            owner="alice",
            workdir="/public/home/alice",
            script="echo root",
        )

        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.store.create_run(
                run_id="run_invalid_remediation",
                owner="alice",
                workdir="/public/home/alice",
                script="echo fixed",
                parent_run_id=parent.run_id,
                lineage_reason="agent_remediation",
                remediation_plan_id="advice_missing:action_missing",
            )

    def test_run_lineage_rejects_cross_owner_parent(self) -> None:
        self.store.create_run(
            run_id="run_alice",
            owner="alice",
            workdir="/public/home/alice",
            script="echo root",
        )

        with self.assertRaisesRegex(ValueError, "owner"):
            self.store.create_run(
                run_id="run_bob",
                owner="bob",
                workdir="/public/home/bob",
                script="echo child",
                parent_run_id="run_alice",
            )

    def test_list_active_job_runs_excludes_terminal_runs_and_missing_job_ids(self) -> None:
        active = self.store.create_run(
            run_id="run_active",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\nsleep 1\n",
        )
        self.store.apply_submit_receipt(
            active.run_id,
            SubmitReceipt(
                job_id="1001",
                run_state=RunState.SUBMITTED,
                strategy=SubmissionStrategy.COMMAND,
            ),
        )

        terminal = self.store.create_run(
            run_id="run_terminal",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\nhostname\n",
        )
        self.store.apply_submit_receipt(
            terminal.run_id,
            SubmitReceipt(
                job_id="1002",
                run_state=RunState.SUBMITTED,
                strategy=SubmissionStrategy.COMMAND,
            ),
        )
        self.store.apply_snapshot(
            terminal.run_id,
            JobSnapshot(
                job_id="1002",
                owner="alice",
                run_state=RunState.SUCCEEDED,
                raw_state_flags=["COMPLETED"],
                exit_code="0:0",
            ),
        )

        self.store.create_run(
            run_id="run_no_job",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\nhostname\n",
        )

        active_runs = self.store.list_active_job_runs()

        self.assertEqual([run.run_id for run in active_runs], ["run_active"])

    def test_collection_task_state_updates_refresh_collection_state(self) -> None:
        run = self.store.create_run(
            run_id="run_tasks",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\nhostname\n",
        )
        self.store.apply_submit_receipt(
            run.run_id,
            SubmitReceipt(
                job_id="1003",
                run_state=RunState.SUBMITTED,
                strategy=SubmissionStrategy.COMMAND,
            ),
        )
        self.store.apply_snapshot(
            run.run_id,
            JobSnapshot(
                job_id="1003",
                owner="alice",
                run_state=RunState.SUCCEEDED,
                raw_state_flags=["COMPLETED"],
                exit_code="0:0",
            ),
        )

        due = self.store.list_due_collection_tasks()
        self.assertEqual(len(due), 7)

        running = self.store.mark_collection_task_running(due[0].task_id, lease_owner="worker-1")
        self.assertEqual(running.state, "running")
        self.assertEqual(self.store.get_run(run.run_id).collection_state, CollectionState.RUNNING)

        for task in due:
            self.store.mark_collection_task_succeeded(task.task_id, payload={"artifacts": []})

        self.assertEqual(self.store.get_run(run.run_id).collection_state, CollectionState.SUCCEEDED)

    def test_acquire_due_collection_tasks_sets_lease_atomically(self) -> None:
        run = self._terminal_run_with_tasks()

        first = self.store.acquire_due_collection_tasks(
            lease_owner="worker-1",
            limit=2,
            lease_seconds=60,
        )
        second = self.store.acquire_due_collection_tasks(
            lease_owner="worker-2",
            limit=10,
            lease_seconds=60,
        )

        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 5)
        leased = [self.store.get_collection_task(task.task_id) for task in first]
        self.assertEqual({task.lease_owner for task in leased}, {"worker-1"})
        self.assertTrue(all(task.lease_expires_at for task in leased))
        self.assertEqual(self.store.get_run(run.run_id).collection_state, CollectionState.RUNNING)

    def test_acquire_due_collection_tasks_reclaims_expired_running_task(self) -> None:
        self._terminal_run_with_tasks()
        task = self.store.list_due_collection_tasks(limit=1)[0]
        self.store.mark_collection_task_running(
            task.task_id,
            lease_owner="worker-old",
            lease_expires_at="2000-01-01T00:00:00+00:00",
        )

        acquired = self.store.acquire_due_collection_tasks(
            lease_owner="worker-new",
            limit=7,
            lease_seconds=60,
        )

        self.assertIn(task.task_id, {acquired_task.task_id for acquired_task in acquired})
        reclaimed = self.store.get_collection_task(task.task_id)
        self.assertEqual(reclaimed.lease_owner, "worker-new")
        self.assertEqual(reclaimed.attempts, 2)

    def test_mark_collection_task_succeeded_rejects_wrong_lease_owner(self) -> None:
        self._terminal_run_with_tasks()
        task = self.store.acquire_due_collection_tasks(
            lease_owner="worker-1",
            limit=1,
            lease_seconds=60,
        )[0]

        with self.assertRaises(RuntimeError):
            self.store.mark_collection_task_succeeded(
                task.task_id,
                lease_owner="worker-2",
            )

        self.store.mark_collection_task_succeeded(
            task.task_id,
            lease_owner="worker-1",
        )
        self.assertEqual(self.store.get_collection_task(task.task_id).state, "succeeded")

    def test_collection_task_rejects_stale_token_even_when_owner_name_is_reused(self) -> None:
        self._terminal_run_with_tasks()
        task = self.store.list_due_collection_tasks(limit=1)[0]
        first = self.store.claim_collection_task(
            task.task_id,
            lease_owner="worker-reused",
            fencing_token=1,
            generation=task.generation,
            lease_expires_at="2026-07-18T01:00:00+00:00",
        )
        self.assertIsNotNone(first)
        second = self.store.claim_collection_task(
            task.task_id,
            lease_owner="worker-reused",
            fencing_token=2,
            generation=task.generation,
            lease_expires_at="2026-07-18T02:00:00+00:00",
        )
        self.assertIsNotNone(second)

        with self.assertRaises(CollectionTaskFenceConflict):
            self.store.mark_collection_task_succeeded(
                task.task_id,
                lease_owner="worker-reused",
                fencing_token=1,
            )

        succeeded = self.store.mark_collection_task_succeeded(
            task.task_id,
            lease_owner="worker-reused",
            fencing_token=2,
        )
        self.assertEqual(succeeded.state, "succeeded")

    def test_retryable_collection_task_failure_marks_run_degraded(self) -> None:
        run = self._terminal_run_with_tasks()
        task = self.store.acquire_due_collection_tasks(
            lease_owner="worker-1",
            limit=1,
            lease_seconds=60,
        )[0]

        failed = self.store.mark_collection_task_failed(
            task.task_id,
            message="temporary accounting delay",
            retryable=True,
            lease_owner="worker-1",
        )

        self.assertEqual(failed.state, "failed_retryable")
        self.assertEqual(self.store.get_run(run.run_id).collection_state, CollectionState.DEGRADED)

    def test_permanent_collection_task_failure_marks_run_failed(self) -> None:
        run = self._terminal_run_with_tasks()
        task = self.store.acquire_due_collection_tasks(
            lease_owner="worker-1",
            limit=1,
            lease_seconds=60,
        )[0]

        failed = self.store.mark_collection_task_failed(
            task.task_id,
            message="unauthorized evidence root",
            retryable=False,
            lease_owner="worker-1",
        )

        self.assertEqual(failed.state, "failed_permanent")
        self.assertEqual(self.store.get_run(run.run_id).collection_state, CollectionState.FAILED)

    def test_upsert_and_list_evidence_objects(self) -> None:
        run = self.store.create_run(
            run_id="run_evidence",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\nhostname\n",
        )

        objects = self.store.upsert_evidence_objects(
            run.run_id,
            [
                {
                    "object_id": "ev_1",
                    "category": "logs",
                    "logical_path": "logs/stdout.tail.json",
                    "store_path": "/tmp/evidence/logs/stdout.tail.json",
                    "source_uri": "evidence://runs/run_evidence/logs/stdout.tail.json",
                    "sha256": "0" * 64,
                    "size_bytes": 12,
                    "mime_type": "application/json",
                    "collection_status": "collected",
                    "mutable_during_run": True,
                    "finalized_at": "2026-07-11T00:00:00+00:00",
                }
            ],
        )

        self.assertEqual(len(objects), 1)
        self.assertEqual(objects[0].category, "logs")
        self.assertTrue(objects[0].mutable_during_run)
        self.assertEqual(
            [
                obj.logical_path
                for obj in self.store.list_evidence_objects(run.run_id, category="logs")
            ],
            ["logs/stdout.tail.json"],
        )

    def _terminal_run_with_tasks(self):
        run = self.store.create_run(
            run_id="run_tasks",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\nhostname\n",
        )
        self.store.apply_submit_receipt(
            run.run_id,
            SubmitReceipt(
                job_id="1003",
                run_state=RunState.SUBMITTED,
                strategy=SubmissionStrategy.COMMAND,
            ),
        )
        self.store.apply_snapshot(
            run.run_id,
            JobSnapshot(
                job_id="1003",
                owner="alice",
                run_state=RunState.SUCCEEDED,
                raw_state_flags=["COMPLETED"],
                exit_code="0:0",
            ),
        )
        return run


if __name__ == "__main__":
    unittest.main()
