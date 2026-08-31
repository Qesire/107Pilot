import json
import tempfile
import unittest
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

from pilot107.adapters.slurm import JobSnapshot, SubmissionStrategy, SubmitReceipt
from pilot107.core.evidence_binding import EvidenceBinder
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
        for path in sorted(
            Path(self._tmp.name).rglob("*"), key=lambda item: len(item.parts), reverse=True
        ):
            if path.is_symlink():
                continue
            with suppress(FileNotFoundError):
                path.chmod(0o700 if path.is_dir() else 0o600)
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
        provenance = json.loads(
            (result.capsule_dir / "provenance.json").read_text(encoding="utf-8")
        )
        seal = self.store.get_evidence_seal(run_id)
        self.assertEqual(provenance["source_evidence_seal_digest"], seal.digest)
        self.assertEqual(provenance["source_evidence_seal_ref"], seal.marker_ref)

    def test_build_rejects_evidence_that_has_not_been_sealed(self) -> None:
        run_id = self._collected_run(seal=False)
        service = RawCapsuleService(
            store=self.store,
            evidence_store=self.evidence_store,
            capsule_root=self.capsule_root,
        )

        with self.assertRaisesRegex(CapsuleError, "sealed"):
            service.build_raw_capsule(run_id)

        self.assertNotEqual(self.store.get_run(run_id).capsule_state, CapsuleState.READY)

    def test_verify_fails_when_file_is_modified(self) -> None:
        run_id = self._collected_run()
        result = RawCapsuleService(
            store=self.store,
            evidence_store=self.evidence_store,
            capsule_root=self.capsule_root,
        ).build_raw_capsule(run_id)
        copied_script = result.capsule_dir / "submission" / "user_script.original.sh"
        copied_script.chmod(0o600)
        copied_script.write_text("changed\n", encoding="utf-8")

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
        manifest_path.chmod(0o600)
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
        self.assertNotEqual(self.store.get_run(run_id).capsule_state, CapsuleState.READY)

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

    def test_crash_after_publish_before_ready_recovers_identical_capsule(self) -> None:
        class SimulatedProcessCrash(BaseException):
            pass

        run_id = self._collected_run()
        service = RawCapsuleService(
            store=self.store,
            evidence_store=self.evidence_store,
            capsule_root=self.capsule_root,
        )
        original_update = self.store.update_capsule_state
        crashed = False

        def crash_before_ready(run_id, state, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal crashed
            if state is CapsuleState.READY and not crashed:
                crashed = True
                raise SimulatedProcessCrash("process stopped before READY receipt")
            return original_update(run_id, state, **kwargs)

        with (
            patch.object(self.store, "update_capsule_state", crash_before_ready),
            self.assertRaises(SimulatedProcessCrash),
        ):
            service.build_raw_capsule(run_id)

        capsule_dir = self.capsule_root / "runs" / run_id / "raw"
        published_digest = (capsule_dir / "manifest.json").read_bytes()
        recovered = service.build_raw_capsule(run_id)

        self.assertEqual((capsule_dir / "manifest.json").read_bytes(), published_digest)
        self.assertEqual(recovered.manifest_sha256, service.get_raw_capsule(run_id).manifest_sha256)
        self.assertEqual(self.store.get_run(run_id).capsule_state, CapsuleState.READY)

    def _collected_run(self, *, seal: bool = True) -> str:
        run = self.store.create_run(
            run_id="run_capsule",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\nhostname\n",
            workspace_digest="a" * 64,
            source_revision="workspace-snapshot:sha256:" + "a" * 64,
            platform_snapshot_ref="snapshot:platform-1",
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
        refs = self._write_evidence(run.run_id)
        if seal:
            EvidenceBinder(
                store=self.store,
                evidence_root=self.evidence_store.root,
            ).seal_terminal_evidence(
                run.run_id,
                refs,
                {
                    "workspace_revision": None,
                    "workspace_digest": "a" * 64,
                    "legacy_boundary": True,
                    "source_revision": "workspace-snapshot:sha256:" + "a" * 64,
                    "platform_snapshot_ref": "snapshot:platform-1",
                },
            )
        return run.run_id

    def _write_evidence(self, run_id: str) -> tuple[str, ...]:
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
        manifest = self.evidence_store.write_json(
            run_id=run_id,
            logical_path="manifest/manifest.json",
            payload={
                "schema": "pilot107.evidence_manifest.v1",
                "run_id": run_id,
                "owner": "alice",
                "job_id": "123",
                "workspace_revision": None,
                "workspace_digest": "a" * 64,
                "legacy_boundary": True,
                "source_revision": "workspace-snapshot:sha256:" + "a" * 64,
                "platform_snapshot_ref": "snapshot:platform-1",
                "artifacts": [
                    {
                        "logical_path": artifact.logical_path,
                        "size_bytes": artifact.size_bytes,
                        "sha256": artifact.sha256,
                        "content_type": artifact.content_type,
                        "evidence_ref": (
                            f"evidence://runs/{run_id}/{artifact.logical_path}"
                        ),
                    }
                    for artifact in artifacts
                ],
                "warnings": [],
            },
        )
        all_artifacts = [*artifacts, manifest]
        finalized_at = "2026-08-31T00:00:00+00:00"
        self.store.upsert_evidence_objects(
            run_id,
            [
                {
                    "object_id": f"ev-{index}",
                    "category": artifact.logical_path.split("/", 1)[0],
                    "logical_path": artifact.logical_path,
                    "store_path": str(artifact.path),
                    "source_uri": f"evidence://runs/{run_id}/{artifact.logical_path}",
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "mime_type": artifact.content_type,
                    "collection_status": "collected",
                    "mutable_during_run": False,
                    "finalized_at": finalized_at,
                    "workspace_revision": None,
                    "workspace_digest": "a" * 64,
                    "source_revision": "workspace-snapshot:sha256:" + "a" * 64,
                    "platform_snapshot_ref": "snapshot:platform-1",
                }
                for index, artifact in enumerate(all_artifacts)
            ],
        )
        return tuple(
            f"evidence://runs/{run_id}/{artifact.logical_path}" for artifact in all_artifacts
        )


if __name__ == "__main__":
    unittest.main()
