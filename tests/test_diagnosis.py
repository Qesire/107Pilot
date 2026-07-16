import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pilot107.adapters.slurm import JobSnapshot, SubmissionStrategy, SubmitReceipt
from pilot107.core import diagnosis as diagnosis_module
from pilot107.core.diagnosis import (
    DiagnosisContextBuilder,
    DiagnosisService,
    diagnose_run,
    load_known_error_rules,
)
from pilot107.core.run_store import RunRecord, RunStore
from pilot107.core.states import DiagnosisState, RunState
from pilot107.worker.evidence import EvidenceStore


class DiagnosisTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self._tmp.name) / "pilot107.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rule_engine_detects_runtime_and_qos_failures(self) -> None:
        run = self._failed_run(exit_code="1:0", terminal_state="FAILED")

        diagnoses = diagnose_run(
            run,
            evidence_text={
                "submission/submit.stderr": "sbatch: error: Invalid qos specification",
                "logs/stderr.tail.txt": "ModuleNotFoundError: No module named 'torch'",
            },
        )

        self.assertEqual(
            {diagnosis.rule_id for diagnosis in diagnoses},
            {
                "SLURM.INVALID_QOS",
                "RUNTIME.PYTHON_PACKAGE_MISSING",
                "RUNTIME.NONZERO_EXIT",
            },
        )
        self.assertTrue(
            all(diagnosis.evidence_refs for diagnosis in diagnoses),
        )

    def test_store_replaces_diagnoses_and_updates_run_state(self) -> None:
        run = self._failed_run(exit_code="137:0", terminal_state="OUT_OF_MEMORY")
        drafts = diagnose_run(
            run,
            evidence_text={"logs/stderr.tail.txt": "Detected oom-kill event"},
        )

        records = self.store.replace_diagnoses(
            run.run_id,
            [draft.to_record_payload() for draft in drafts],
        )

        self.assertIn("RUNTIME.OOM", {record.rule_id for record in records})
        oom = next(record for record in records if record.rule_id == "RUNTIME.OOM")
        self.assertEqual(oom.category, "runtime")
        self.assertEqual(oom.stage, "runtime")
        self.assertIn("fix", oom.fix_guide)
        self.assertEqual(self.store.get_run(run.run_id).diagnosis_state, DiagnosisState.SUCCEEDED)
        events = self.store.list_events(run.run_id)
        self.assertEqual(events[-1].event_type, "diagnosis.updated")

    def test_known_error_rules_load_from_yaml_and_preserve_legacy_rules(self) -> None:
        rules = load_known_error_rules()
        by_id = {rule.error_id: rule for rule in rules}

        self.assertEqual(
            by_id["SLURM.INVALID_QOS"].symptoms,
            ("invalid qos", "invalid qos specification", "invalidqos", "qosnotallowed"),
        )
        self.assertEqual(
            by_id["SLURM.INVALID_QOS"].suggested_patch,
            {"resources.qos": None},
        )
        self.assertEqual(
            by_id["RUNTIME.NONZERO_EXIT"].state_match,
            {"state": "FAILED", "exit_code_not_in": [None, "0:0"]},
        )
        self.assertIn("SLURM.SCRIPT_PATH_FROM_ZERO", by_id)
        self.assertIn("SLURM.WORKDIR_NOT_SHARED", by_id)
        self.assertIn("ARTIFACT.POSTPROCESS_FALSE_FAILURE", by_id)
        self.assertGreaterEqual(len(rules), 27)

    def test_platform_specific_rules_use_precise_evidence_and_safe_remediation(self) -> None:
        run = self._failed_run(exit_code="1:0", terminal_state="FAILED")

        diagnoses = diagnose_run(
            run,
            evidence_text={
                "submission/submit.stderr": (
                    "sbatch: error: Invalid account or account/partition combination specified"
                ),
                "logs/stderr.tail.txt": (
                    "bash: conda: command not found\n"
                    "Failed to initialize NVML: Driver/library version mismatch"
                ),
            },
        )
        by_id = {diagnosis.rule_id: diagnosis for diagnosis in diagnoses}

        self.assertIn("SLURM.INVALID_ASSOCIATION", by_id)
        self.assertIn("RUNTIME.CONDA_BATCH_NOT_INITIALIZED", by_id)
        self.assertIn("RUNTIME.NVML_DRIVER_LIBRARY_MISMATCH", by_id)
        self.assertNotIn("RUNTIME.COMMAND_NOT_FOUND", by_id)
        self.assertFalse(by_id["SLURM.INVALID_ASSOCIATION"].retryable)
        self.assertFalse(by_id["RUNTIME.NVML_DRIVER_LIBRARY_MISMATCH"].retryable)
        self.assertEqual(by_id["RUNTIME.CONDA_BATCH_NOT_INITIALIZED"].suggested_patch, {})

        pending_qos = diagnose_run(
            run,
            evidence_text={
                "slurm/runtime_status.json": '{"state":"PENDING","reason":"InvalidQOS"}'
            },
        )
        self.assertIn("SLURM.INVALID_QOS", {item.rule_id for item in pending_qos})

        qos_limits = diagnose_run(
            run,
            evidence_text={
                "slurm/runtime_status.json": (
                    "QOSMaxWallDurationPerJobLimit QOSMaxCpuPerJobLimit QOSGrpCpuLimit"
                )
            },
        )
        qos_limit_ids = {item.rule_id for item in qos_limits}
        self.assertTrue(
            {
                "SLURM.QOS_WALLTIME_REQUEST_LIMIT",
                "SLURM.QOS_CPU_REQUEST_LIMIT",
                "SLURM.QOS_CPU_CAPACITY_LIMIT",
            }.issubset(qos_limit_ids)
        )
        capacity = next(
            item for item in qos_limits if item.rule_id == "SLURM.QOS_CPU_CAPACITY_LIMIT"
        )
        self.assertEqual(capacity.suggested_patch, {})

    def test_context_builder_includes_runtime_and_gpu_probe_evidence(self) -> None:
        paths = DiagnosisContextBuilder(store=self.store).logical_paths

        self.assertIn("slurm/runtime_status.json", paths)
        self.assertIn("run/environment/gpu.json", paths)

    def test_installed_distribution_rule_directory_is_discoverable(self) -> None:
        class InstalledDistribution:
            files = (Path("../../../share/pilot107/known_errors/INDEX.yaml"),)

            def locate_file(self, item: Path) -> Path:
                return Path("/opt/pilot107/lib/python3.12/site-packages") / item

        with (
            patch.object(diagnosis_module, "_SOURCE_KNOWN_ERRORS_DIR", Path("/missing")),
            patch.object(
                diagnosis_module,
                "distribution",
                return_value=InstalledDistribution(),
            ),
        ):
            resolved = diagnosis_module._default_known_errors_dir()

        self.assertEqual(resolved, Path("/opt/pilot107/share/pilot107/known_errors"))

    def test_rule_engine_detects_terminal_state_and_state_match_rules(self) -> None:
        timeout_run = self._failed_run(exit_code="0:0", terminal_state="TIMEOUT")
        failed_run = self._failed_run(exit_code="2:0", terminal_state="FAILED")

        timeout = diagnose_run(timeout_run, evidence_text={})
        failed = diagnose_run(failed_run, evidence_text={})

        self.assertIn("RUNTIME.TIMEOUT", {diagnosis.rule_id for diagnosis in timeout})
        self.assertIn("RUNTIME.NONZERO_EXIT", {diagnosis.rule_id for diagnosis in failed})

    def test_successful_run_with_incidental_tmp_metadata_has_no_workdir_diagnosis(self) -> None:
        run = self.store.create_run(
            run_id="run_success_tmp_metadata",
            owner="alice",
            workdir="/public/home/alice/project",
            script="#!/bin/bash\ntrue\n",
        )
        self.store.apply_submit_receipt(
            run.run_id,
            SubmitReceipt(
                job_id="1002",
                run_state=RunState.SUBMITTED,
                strategy=SubmissionStrategy.COMMAND,
            ),
        )
        succeeded = self.store.apply_snapshot(
            run.run_id,
            JobSnapshot(
                job_id="1002",
                owner="alice",
                run_state=RunState.SUCCEEDED,
                raw_state_flags=["COMPLETED"],
                exit_code="0:0",
            ),
        )

        diagnoses = diagnose_run(
            succeeded,
            evidence_text={
                "environment/summary.json": '{"tmpdir":"/tmp"}',
                "run/environment/basic.json": (
                    '{"warning":"shared path status is established by '
                    'WorkDirPreflight, not this runtime probe"}'
                ),
            },
        )

        self.assertNotIn(
            "SLURM.WORKDIR_NOT_SHARED",
            {diagnosis.rule_id for diagnosis in diagnoses},
        )

    def test_store_marks_empty_diagnosis_as_skipped(self) -> None:
        run = self._failed_run(exit_code="0:0", terminal_state="FAILED")

        records = self.store.replace_diagnoses(run.run_id, [])

        self.assertEqual(records, [])
        self.assertEqual(self.store.get_run(run.run_id).diagnosis_state, DiagnosisState.SKIPPED)

    def test_context_builder_reads_allowed_evidence_snippets(self) -> None:
        run = self._failed_run(exit_code="1:0", terminal_state="FAILED")
        evidence_store = EvidenceStore(Path(self._tmp.name) / "evidence")
        artifact = evidence_store.write_text(
            run_id=run.run_id,
            logical_path="logs/stderr.tail.txt",
            content="ModuleNotFoundError: No module named 'torch'\n",
            content_type="text/plain",
        )
        self.store.upsert_evidence_objects(
            run.run_id,
            [
                {
                    "object_id": "ev_stderr",
                    "category": "logs",
                    "logical_path": artifact.logical_path,
                    "store_path": str(artifact.path),
                    "source_uri": f"evidence://runs/{run.run_id}/{artifact.logical_path}",
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "mime_type": artifact.content_type,
                    "collection_status": "collected",
                    "mutable_during_run": False,
                }
            ],
        )

        context = DiagnosisContextBuilder(store=self.store).build(run.run_id)
        records = DiagnosisService(store=self.store).diagnose(run.run_id)

        self.assertIn("logs/stderr.tail.txt", context.evidence_text)
        self.assertIn("RUNTIME.PYTHON_PACKAGE_MISSING", {record.rule_id for record in records})

    def test_missing_log_metadata_is_not_treated_as_user_error_text(self) -> None:
        run = self._failed_run(exit_code="42:0", terminal_state="FAILED")
        evidence_store = EvidenceStore(Path(self._tmp.name) / "evidence")
        artifact = evidence_store.write_json(
            run_id=run.run_id,
            logical_path="logs/stderr.tail.json",
            payload={
                "metadata": {
                    "status": "missing",
                    "stderr": "stat: No such file or directory",
                },
                "stream": "stderr",
                "tail": None,
            },
        )
        self.store.upsert_evidence_objects(
            run.run_id,
            [
                {
                    "object_id": "ev_missing_stderr",
                    "category": "logs",
                    "logical_path": artifact.logical_path,
                    "store_path": str(artifact.path),
                    "source_uri": f"evidence://runs/{run.run_id}/{artifact.logical_path}",
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "mime_type": artifact.content_type,
                    "collection_status": "collected",
                    "mutable_during_run": False,
                }
            ],
        )

        context = DiagnosisContextBuilder(store=self.store).build(run.run_id)
        records = DiagnosisService(store=self.store).diagnose(run.run_id)
        rule_ids = {record.rule_id for record in records}

        self.assertNotIn("logs/stderr.tail.json", context.evidence_text)
        self.assertEqual(rule_ids, {"RUNTIME.NONZERO_EXIT"})

    def _failed_run(self, *, exit_code: str, terminal_state: str) -> RunRecord:
        run = self.store.create_run(
            run_id=f"run_{exit_code.replace(':', '_')}_{terminal_state.lower()}",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\npython train.py\n",
        )
        self.store.apply_submit_receipt(
            run.run_id,
            SubmitReceipt(
                job_id="1001",
                run_state=RunState.SUBMITTED,
                strategy=SubmissionStrategy.COMMAND,
            ),
        )
        return self.store.apply_snapshot(
            run.run_id,
            JobSnapshot(
                job_id="1001",
                owner="alice",
                run_state=RunState.FAILED,
                raw_state_flags=[terminal_state],
                exit_code=exit_code,
            ),
        )


if __name__ == "__main__":
    unittest.main()
