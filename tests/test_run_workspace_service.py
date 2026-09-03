import tempfile
import unittest
from pathlib import Path

from pilot107.adapters.slurm import JobSnapshot, SubmissionStrategy, SubmitReceipt
from pilot107.core.contracts import ContractService, ContractStore, RecipeCatalog
from pilot107.core.run_store import RunStore
from pilot107.core.states import RunState
from pilot107.services.run_workspace_service import RunWorkspaceService


class RunWorkspaceServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "pilot107.db"
        self.run_store = RunStore(self.db_path)
        self.contract_store = ContractStore(self.db_path)
        self.contract_service = ContractService(
            catalog=RecipeCatalog(),
            store=self.contract_store,
        )
        self.service = RunWorkspaceService(
            store=self.run_store,
            contract_store=self.contract_store,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_failed_run_projects_diagnosis_evidence_and_provenance(self) -> None:
        contract = self.contract_service.create(owner="alice", payload=_contract_payload())
        self.run_store.create_run(
            run_id="run_parent",
            owner="alice",
            workdir="/public/home/alice/project",
            script="#!/bin/bash\npython train.py\n",
            contract_id=contract.contract_id,
            job_name="workspace-parent",
        )
        run = self.run_store.create_run(
            run_id="run_workspace_failed",
            owner="alice",
            workdir="/public/home/alice/project",
            script="#!/bin/bash\npython train.py\n",
            contract_id=contract.contract_id,
            parent_run_id="run_parent",
            lineage_reason="repair_retry",
            job_name="workspace-failure",
        )
        self.run_store.apply_submit_receipt(
            run.run_id,
            SubmitReceipt(
                job_id="41001",
                run_state=RunState.SUBMITTED,
                strategy=SubmissionStrategy.COMMAND,
            ),
        )
        self.run_store.apply_snapshot(
            run.run_id,
            JobSnapshot(
                job_id="41001",
                owner="alice",
                run_state=RunState.FAILED,
                raw_state_flags=["FAILED"],
                exit_code="1:0",
            ),
        )
        self.run_store.upsert_evidence_objects(
            run.run_id,
            [
                {
                    "object_id": "ev_stderr",
                    "category": "logs",
                    "logical_path": "logs/stderr.tail.txt",
                    "store_path": "/tmp/not-read-by-workspace/stderr.txt",
                    "collection_status": "collected",
                    "mutable_during_run": False,
                },
                {
                    "object_id": "ev_result",
                    "category": "outputs",
                    "logical_path": "outputs/result.json",
                    "store_path": "/tmp/not-read-by-workspace/result.json",
                    "collection_status": "collected",
                    "mutable_during_run": False,
                },
            ],
        )
        self.run_store.replace_diagnoses(
            run.run_id,
            [
                {
                    "diagnosis_id": "diag_workspace_missing_torch",
                    "rule_id": "RUNTIME.PYTHON_PACKAGE_MISSING",
                    "severity": "error",
                    "summary": "Python 运行环境缺少作业需要的包。",
                    "evidence_refs": ["evidence://runs/run_workspace_failed/logs/stderr.tail.txt"],
                    "suggested_patch": {},
                    "retryable": True,
                    "confidence": "high",
                    "category": "optional_dependency",
                    "stage": "runtime",
                    "fix_guide": {"fix": "install torch"},
                }
            ],
        )

        payload = self.service.get(run.run_id, owner="alice")

        self.assertEqual(payload["outcome"]["kind"], "failed")
        self.assertEqual(payload["attention"]["severity"], "critical")
        self.assertEqual(payload["next_action"]["kind"], "prepare_repair")
        self.assertEqual(payload["evidence_summary"]["object_count"], 2)
        self.assertEqual(payload["evidence_summary"]["result_count"], 1)
        self.assertTrue(payload["evidence_summary"]["stderr_available"])
        self.assertFalse(payload["evidence_summary"]["stdout_available"])
        self.assertEqual(payload["evidence_summary"]["diagnosis_count"], 1)
        self.assertEqual(payload["provenance"]["contract_id"], contract.contract_id)
        self.assertEqual(payload["provenance"]["contract_digest"], contract.digest)
        self.assertEqual(payload["provenance"]["parent_run_id"], "run_parent")

    def test_success_does_not_claim_scientific_validity(self) -> None:
        run = self.run_store.create_run(
            run_id="run_workspace_success",
            owner="alice",
            workdir="/public/home/alice/project",
            script="#!/bin/bash\ntrue\n",
        )
        self.run_store.apply_submit_receipt(
            run.run_id,
            SubmitReceipt(
                job_id="41002",
                run_state=RunState.SUBMITTED,
                strategy=SubmissionStrategy.COMMAND,
            ),
        )
        self.run_store.apply_snapshot(
            run.run_id,
            JobSnapshot(
                job_id="41002",
                owner="alice",
                run_state=RunState.SUCCEEDED,
                raw_state_flags=["COMPLETED"],
                exit_code="0:0",
            ),
        )
        for task in self.run_store.list_collection_tasks(run.run_id):
            self.run_store.mark_collection_task_succeeded(task["task_id"])

        payload = self.service.get(run.run_id, owner="alice")

        self.assertEqual(payload["outcome"]["kind"], "succeeded")
        self.assertIn("科学结果仍需", payload["outcome"]["summary"])
        self.assertEqual(payload["next_action"]["kind"], "view_results")
        self.assertNotIn("scientific_status", payload)

    def test_owner_boundary_is_fail_closed(self) -> None:
        run = self.run_store.create_run(
            run_id="run_workspace_private",
            owner="alice",
            workdir="/public/home/alice",
            script="true",
        )

        with self.assertRaises(PermissionError):
            self.service.get(run.run_id, owner="bob")


def _contract_payload() -> dict:
    return {
        "recipe_version_id": "recipe_python_cpu@1.0.0",
        "project": {
            "name": "workspace-read-model",
            "workdir": "/public/home/alice/project",
        },
        "entry": {
            "command": "python train.py",
            "expected_outputs": ["outputs/result.json"],
        },
        "resources": {
            "partition": "debug",
            "qos": "normal",
            "nodes": 1,
            "ntasks": 1,
            "cpus_per_task": 1,
            "time_limit": "00:05:00",
        },
    }


if __name__ == "__main__":
    unittest.main()
