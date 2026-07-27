import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pilot107.adapters.slurm import JobSnapshot, SubmissionStrategy, SubmitReceipt
from pilot107.core.agent import AgentExplainService
from pilot107.core.code_context import (
    CodeContextPolicy,
    CodeContextService,
    LocalWorkspaceReader,
    SshWorkspaceConfig,
    SshWorkspaceReader,
    locate_error_locations,
)
from pilot107.core.evidence_binding import EvidenceBinder
from pilot107.core.run_store import RunStore
from pilot107.core.states import RunState
from pilot107.worker.evidence import EvidenceStore


class CodeContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.workspace = self.root / "public" / "home" / "alice" / "demo"
        (self.workspace / "src").mkdir(parents=True)
        (self.workspace / "src" / "train.py").write_text(
            "def train():\n"
            "    values = [1, 2, 3]\n"
            "    return values[4]\n"
            "\n"
            "train()\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q"], cwd=self.workspace, check=True)
        subprocess.run(
            ["git", "config", "user.email", "pilot107-test@example.invalid"],
            cwd=self.workspace,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Pilot107 Test"],
            cwd=self.workspace,
            check=True,
        )
        subprocess.run(["git", "add", "src/train.py"], cwd=self.workspace, check=True)
        subprocess.run(["git", "commit", "-qm", "initial source"], cwd=self.workspace, check=True)
        self.store = RunStore(self.root / "pilot107.db")
        self.evidence_store = EvidenceStore(self.root / "evidence")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_traceback_selects_bounded_snapshot_window(self) -> None:
        run = self.store.create_run(
            run_id="run_code_context",
            owner="alice",
            workdir=str(self.workspace),
            script="#!/bin/bash\npython src/train.py\n",
        )
        service = CodeContextService(
            reader=LocalWorkspaceReader(allowed_roots=(self.workspace.parent,)),
            policy=CodeContextPolicy(context_before_lines=1, context_after_lines=1),
        )

        bundle = service.capture(
            run,
            evidence_texts=(
                'Traceback (most recent call last):\n'
                '  File "src/train.py", line 3, in train\n'
                "IndexError: list index out of range\n",
            ),
        )

        self.assertTrue(bundle.snapshot_id.startswith("codesnap_"))
        self.assertEqual(bundle.workspace, str(self.workspace))
        self.assertFalse(bundle.dirty)
        self.assertEqual(len(bundle.chunks), 1)
        chunk = bundle.chunks[0]
        self.assertEqual(chunk.path, "src/train.py")
        self.assertEqual((chunk.start_line, chunk.end_line), (2, 4))
        self.assertIn("return values[4]", chunk.content)
        self.assertIn(bundle.snapshot_id, chunk.source_ref)

    def test_outside_and_secret_paths_are_not_read(self) -> None:
        (self.workspace / ".env").write_text("API_KEY=top-secret\n", encoding="utf-8")
        run = self.store.create_run(
            run_id="run_secret_context",
            owner="alice",
            workdir=str(self.workspace),
            script="#!/bin/bash\ntrue\n",
        )
        service = CodeContextService(
            reader=LocalWorkspaceReader(allowed_roots=(self.workspace.parent,)),
        )

        bundle = service.capture(
            run,
            evidence_texts=(
                f'File "{self.workspace / ".env"}", line 1\n'
                'File "/etc/passwd", line 1\n',
            ),
        )

        self.assertEqual(bundle.chunks, ())
        self.assertIn("source_path_excluded:.env", bundle.warnings)

    def test_dirty_diff_changes_the_snapshot_fingerprint(self) -> None:
        run = self.store.create_run(
            run_id="run_dirty_context",
            owner="alice",
            workdir=str(self.workspace),
            script="#!/bin/bash\npython src/train.py\n",
        )
        service = CodeContextService(
            reader=LocalWorkspaceReader(allowed_roots=(self.workspace.parent,)),
        )
        evidence = ('File "src/train.py", line 3\nIndexError\n',)
        clean = service.capture(run, evidence_texts=evidence)
        (self.workspace / "src" / "train.py").write_text(
            "def train():\n    return [1][2]\n\ntrain()\n",
            encoding="utf-8",
        )

        dirty = service.capture(run, evidence_texts=evidence)

        self.assertTrue(dirty.dirty)
        self.assertNotEqual(clean.snapshot_id, dirty.snapshot_id)
        self.assertNotEqual(clean.worktree_fingerprint, dirty.worktree_fingerprint)

    def test_agent_explanation_exposes_citable_code_context(self) -> None:
        run = self._failed_run()
        artifact = self.evidence_store.write_text(
            run_id=run.run_id,
            logical_path="logs/stderr.tail.txt",
            content='Traceback\n  File "src/train.py", line 3, in train\nIndexError\n',
            content_type="text/plain",
        )
        evidence_ref = f"evidence://runs/{run.run_id}/{artifact.logical_path}"
        self.store.upsert_evidence_objects(
            run.run_id,
            [
                {
                    "object_id": "ev_code_stderr",
                    "category": "logs",
                    "logical_path": artifact.logical_path,
                    "store_path": str(artifact.path),
                    "source_uri": evidence_ref,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "mime_type": artifact.content_type,
                    "collection_status": "collected",
                    "mutable_during_run": False,
                }
            ],
        )
        self.store.replace_diagnoses(
            run.run_id,
            [
                {
                    "diagnosis_id": "diag_code_failure",
                    "rule_id": "RUNTIME.NONZERO_EXIT",
                    "severity": "error",
                    "summary": "作业以非零退出码结束。",
                    "evidence_refs": [evidence_ref],
                    "confidence": "high",
                }
            ],
        )
        context_service = CodeContextService(
            reader=LocalWorkspaceReader(allowed_roots=(self.workspace.parent,)),
        )
        explanation = AgentExplainService(
            store=self.store,
            evidence_binder=EvidenceBinder(store=self.store, evidence_root=self.root / "evidence"),
            code_context_service=context_service,
        ).explain(run.run_id)

        self.assertIsNotNone(explanation.code_context)
        assert explanation.code_context is not None
        self.assertEqual(explanation.code_context.chunks[0].path, "src/train.py")
        self.assertTrue(any(fact.fact_id.startswith("fact_code_") for fact in explanation.facts))
        payload = explanation.to_payload()
        self.assertEqual(
            payload["code_context"]["snapshot_id"],
            explanation.code_context.snapshot_id,
        )

    def test_ssh_reader_requires_an_existing_control_master_and_quotes_remote_args(self) -> None:
        reader = SshWorkspaceReader(
            config=SshWorkspaceConfig(
                target="pilot107-slurm",
                control_path=Path("/run/pilot107/ssh/real107.sock"),
                port=22,
            ),
            allowed_roots=("/public/home/alice",),
        )
        with patch("pilot107.core.code_context.subprocess.run") as run:
            run.side_effect = [
                SimpleNamespace(returncode=0, stdout="Master running\n", stderr=""),
                SimpleNamespace(returncode=0, stdout="abc123\n", stderr=""),
            ]
            output = reader.git(
                "/public/home/alice/project name",
                ("rev-parse", "--verify", "HEAD"),
            )

        self.assertEqual(output, "abc123\n")
        check_argv = run.call_args_list[0].args[0]
        remote_argv = run.call_args_list[1].args[0]
        self.assertEqual(
            check_argv[:5],
            ["ssh", "-S", "/run/pilot107/ssh/real107.sock", "-O", "check"],
        )
        self.assertIn("'/public/home/alice/project name'", remote_argv[-1])
        self.assertIn("ControlMaster=no", remote_argv)

    def test_location_parser_rejects_ancestor_escape(self) -> None:
        locations = locate_error_locations(
            evidence_texts=('File "../other/train.py", line 3\n',),
            workspace="/public/home/alice/project",
            max_locations=3,
        )
        self.assertEqual(locations, ())

    def _failed_run(self):
        run = self.store.create_run(
            run_id="run_code_agent",
            owner="alice",
            workdir=str(self.workspace),
            script="#!/bin/bash\npython src/train.py\n",
        )
        self.store.apply_submit_receipt(
            run.run_id,
            SubmitReceipt(
                job_id="4001",
                run_state=RunState.SUBMITTED,
                strategy=SubmissionStrategy.COMMAND,
            ),
        )
        return self.store.apply_snapshot(
            run.run_id,
            JobSnapshot(
                job_id="4001",
                owner="alice",
                run_state=RunState.FAILED,
                raw_state_flags=["FAILED"],
                exit_code="1:0",
            ),
        )


if __name__ == "__main__":
    unittest.main()
