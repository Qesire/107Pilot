import tempfile
import unittest
from pathlib import Path

from pilot107.adapters.slurm import SubmissionStrategy, SubmitReceipt
from pilot107.api.evidence_query import EvidencePreviewUnavailable, EvidenceQueryService
from pilot107.core.run_store import RunStore
from pilot107.worker.evidence import EvidenceStore


class EvidenceQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.run_store = RunStore(root / "pilot107.db")
        self.evidence_store = EvidenceStore(root / "evidence")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_get_evidence_tree_returns_tasks_and_files(self) -> None:
        run = self.run_store.create_run(
            run_id="run_tree",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\nhostname\n",
        )
        self.run_store.apply_submit_receipt(
            run.run_id,
            SubmitReceipt(
                job_id="123",
                run_state=run.state,
                strategy=SubmissionStrategy.COMMAND,
                raw_response={"stdout": "123\n"},
            ),
        )
        self.evidence_store.write_json(
            run_id=run.run_id,
            logical_path="submission/slurm_submit_response.json",
            payload={"job_id": "123"},
        )
        self.evidence_store.write_text(
            run_id=run.run_id,
            logical_path="submission/user_script.original.sh",
            content="#!/bin/bash\nhostname\n",
            content_type="text/x-shellscript",
        )
        self.run_store.upsert_evidence_objects(
            run.run_id,
            [
                {
                    "object_id": "ev_submission",
                    "category": "submission",
                    "logical_path": "submission/user_script.original.sh",
                    "store_path": str(
                        self.evidence_store.run_root(run.run_id)
                        / "submission"
                        / "user_script.original.sh"
                    ),
                    "source_uri": "evidence://runs/run_tree/submission/user_script.original.sh",
                    "sha256": "0" * 64,
                    "size_bytes": 20,
                    "mime_type": "text/x-shellscript",
                    "collection_status": "collected",
                    "mutable_during_run": False,
                    "finalized_at": "2026-07-11T00:00:00+00:00",
                }
            ],
        )

        payload = EvidenceQueryService(
            store=self.run_store,
            evidence_store=self.evidence_store,
        ).get_evidence_tree(run.run_id)

        self.assertEqual(payload["run_id"], run.run_id)
        self.assertEqual(payload["job_id"], "123")
        self.assertEqual(payload["collection_state"], "pending")
        self.assertEqual(
            {task["task_type"] for task in payload["tasks"]},
            {
                "submission_snapshot",
                "runtime_status",
            },
        )
        root_children = {node["name"]: node for node in payload["tree"]["children"]}
        submission_children = {
            node["name"]: node for node in root_children["submission"]["children"]
        }
        self.assertEqual(
            submission_children["slurm_submit_response.json"]["content_type"],
            "application/json",
        )
        self.assertEqual(
            submission_children["user_script.original.sh"]["content_type"],
            "text/x-shellscript",
        )
        self.assertIn("sha256", submission_children["slurm_submit_response.json"])
        self.assertEqual(payload["objects"][0]["object_id"], "ev_submission")
        self.assertEqual(payload["objects"][0]["category"], "submission")
        self.assertNotIn("store_path", payload["objects"][0])

    def test_object_preview_is_bounded_and_verifies_complete_text(self) -> None:
        run = self.run_store.create_run(
            run_id="run_preview",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\ntrue\n",
        )
        artifact = self.evidence_store.write_text(
            run_id=run.run_id,
            logical_path="logs/stdout.tail.txt",
            content="first line\nsecond line\n",
            content_type="text/plain",
        )
        self.run_store.upsert_evidence_objects(
            run.run_id,
            [
                {
                    "object_id": "ev_preview",
                    "category": "logs",
                    "logical_path": artifact.logical_path,
                    "store_path": str(artifact.path),
                    "source_uri": "evidence://runs/run_preview/logs/stdout.tail.txt",
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "mime_type": artifact.content_type,
                    "collection_status": "collected",
                    "mutable_during_run": False,
                }
            ],
        )
        service = EvidenceQueryService(
            store=self.run_store,
            evidence_store=self.evidence_store,
        )

        complete = service.get_object_preview(run.run_id, "ev_preview")
        truncated = service.get_object_preview(run.run_id, "ev_preview", max_bytes=7)

        self.assertEqual(complete["preview"]["content"], "first line\nsecond line\n")
        self.assertEqual(complete["preview"]["integrity"], "verified")
        self.assertFalse(complete["preview"]["truncated"])
        self.assertTrue(truncated["preview"]["truncated"])
        self.assertEqual(truncated["preview"]["bytes_read"], 7)
        self.assertEqual(truncated["preview"]["integrity"], "not_checked")
        self.assertNotIn("store_path", complete)

    def test_object_preview_rejects_inconsistent_store_binding(self) -> None:
        run = self.run_store.create_run(
            run_id="run_binding",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\ntrue\n",
        )
        artifact = self.evidence_store.write_text(
            run_id=run.run_id,
            logical_path="logs/stderr.tail.txt",
            content="safe content\n",
            content_type="text/plain",
        )
        self.run_store.upsert_evidence_objects(
            run.run_id,
            [
                {
                    "object_id": "ev_bad_binding",
                    "category": "logs",
                    "logical_path": artifact.logical_path,
                    "store_path": str(Path(self._tmp.name) / "outside.txt"),
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "mime_type": artifact.content_type,
                    "collection_status": "collected",
                    "mutable_during_run": False,
                }
            ],
        )

        with self.assertRaisesRegex(EvidencePreviewUnavailable, "binding"):
            EvidenceQueryService(
                store=self.run_store,
                evidence_store=self.evidence_store,
            ).get_object_preview(run.run_id, "ev_bad_binding")


if __name__ == "__main__":
    unittest.main()
