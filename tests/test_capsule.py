import json
import tempfile
import unittest
from pathlib import Path

from pilot107.adapters.slurm import JobSnapshot, SubmissionStrategy, SubmitReceipt
from pilot107.core.run_store import RunStore
from pilot107.core.states import CapsuleState, CollectionState, RunState
from pilot107.worker.capsule import CapsuleError, RawCapsuleService, verify_raw_capsule
from pilot107.worker.evidence import EvidenceStore


class RawCapsuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = RunStore(root / "pilot107.db")
        self.evidence_store = EvidenceStore(root / "evidence")
        self.capsule_root = root / "capsules"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_build_raw_capsule_and_verify(self) -> None:
        run_id = self._collected_run()
        service = RawCapsuleService(
            store=self.store,
            evidence_store=self.evidence_store,
            capsule_root=self.capsule_root,
        )

        result = service.build_raw_capsule(run_id)
        verify = verify_raw_capsule(result.capsule_dir)
        read = service.get_raw_capsule(run_id)

        self.assertTrue(verify.valid, verify.errors)
        self.assertEqual(verify.capsule_id, result.capsule_id)
        self.assertGreaterEqual(verify.checked_files, 4)
        self.assertEqual(self.store.get_run(run_id).capsule_state, CapsuleState.READY)
        self.assertTrue((result.capsule_dir / "submission" / "user_script.original.sh").exists())
        manifest = json.loads((result.capsule_dir / "manifest.json").read_text(encoding="utf-8"))
        manifest_paths = {file["logical_path"] for file in manifest["files"]}
        self.assertIn("submission/user_script.original.sh", manifest_paths)
        self.assertIn("outputs/inventory.json", manifest_paths)
        self.assertTrue(read.valid, read.errors)
        self.assertEqual(read.capsule_id, result.capsule_id)
        self.assertEqual(read.manifest_sha256, result.manifest_sha256)
        self.assertEqual(read.files_copied, result.files_copied)
        self.assertEqual(read.manifest["run_id"], run_id)

    def test_verify_fails_when_file_is_modified(self) -> None:
        run_id = self._collected_run()
        result = RawCapsuleService(
            store=self.store,
            evidence_store=self.evidence_store,
            capsule_root=self.capsule_root,
        ).build_raw_capsule(run_id)
        (result.capsule_dir / "submission" / "user_script.original.sh").write_text(
            "changed\n", encoding="utf-8"
        )

        verify = verify_raw_capsule(result.capsule_dir)
        read = RawCapsuleService(
            store=self.store,
            evidence_store=self.evidence_store,
            capsule_root=self.capsule_root,
        ).get_raw_capsule(run_id)

        self.assertFalse(verify.valid)
        self.assertTrue(any("mismatch" in error for error in verify.errors))
        self.assertFalse(read.valid)

    def test_build_rejects_unsafe_evidence_manifest_path(self) -> None:
        run_id = self._collected_run()
        manifest_path = self.evidence_store.run_root(run_id) / "manifest" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"].append(
            {
                "logical_path": "../escape.txt",
                "sha256": "bad",
                "size_bytes": 1,
                "content_type": "text/plain",
            }
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        service = RawCapsuleService(
            store=self.store,
            evidence_store=self.evidence_store,
            capsule_root=self.capsule_root,
        )

        with self.assertRaises(CapsuleError):
            service.build_raw_capsule(run_id)
        self.assertEqual(self.store.get_run(run_id).capsule_state, CapsuleState.FAILED)

    def test_build_wraps_filesystem_failure_as_capsule_error(self) -> None:
        run_id = self._collected_run()
        invalid_root = Path(self._tmp.name) / "capsule-root-is-a-file"
        invalid_root.write_text("not a directory", encoding="utf-8")
        service = RawCapsuleService(
            store=self.store,
            evidence_store=self.evidence_store,
            capsule_root=invalid_root,
        )

        with self.assertRaisesRegex(CapsuleError, "raw Capsule build failed"):
            service.build_raw_capsule(run_id)

        self.assertEqual(self.store.get_run(run_id).capsule_state, CapsuleState.FAILED)

    def _collected_run(self) -> str:
        run = self.store.create_run(
            run_id="run_capsule",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\nhostname\n",
        )
        self.store.apply_submit_receipt(
            run.run_id,
            SubmitReceipt(
                job_id="123",
                run_state=RunState.SUBMITTED,
                strategy=SubmissionStrategy.COMMAND,
                raw_response={"stdout": "123\n"},
            ),
        )
        self.store.apply_snapshot(
            run.run_id,
            JobSnapshot(
                job_id="123",
                owner="alice",
                run_state=RunState.SUCCEEDED,
                raw_state_flags=["COMPLETED"],
                exit_code="0:0",
            ),
        )
        for task in self.store.list_collection_tasks(run.run_id):
            self.store.mark_collection_task_succeeded(task["task_id"])
        self.assertEqual(self.store.get_run(run.run_id).collection_state, CollectionState.SUCCEEDED)
        self._write_evidence(run.run_id)
        return run.run_id

    def _write_evidence(self, run_id: str) -> None:
        artifacts = [
            self.evidence_store.write_text(
                run_id=run_id,
                logical_path="submission/user_script.original.sh",
                content="#!/bin/bash\nhostname\n",
                content_type="text/x-shellscript",
            ),
            self.evidence_store.write_json(
                run_id=run_id,
                logical_path="slurm/accounting.json",
                payload={"state": "COMPLETED"},
            ),
            self.evidence_store.write_json(
                run_id=run_id,
                logical_path="outputs/inventory.json",
                payload={"files": []},
            ),
        ]
        self.evidence_store.write_json(
            run_id=run_id,
            logical_path="manifest/manifest.json",
            payload={
                "schema": "pilot107.evidence_manifest.v1",
                "run_id": run_id,
                "artifacts": [
                    {
                        "logical_path": artifact.logical_path,
                        "size_bytes": artifact.size_bytes,
                        "sha256": artifact.sha256,
                        "content_type": artifact.content_type,
                    }
                    for artifact in artifacts
                ],
                "warnings": [],
            },
        )


if __name__ == "__main__":
    unittest.main()
