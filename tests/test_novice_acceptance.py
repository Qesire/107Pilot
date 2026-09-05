from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from pilot107.core.novice_acceptance import (
    REQUIRED_TASKS,
    evaluate_novice_acceptance,
    evaluate_novice_study_readiness,
)


class NoviceAcceptanceTests(unittest.TestCase):
    def test_five_traceable_terminal_free_sessions_pass_under_ten_minute_median(
        self,
    ) -> None:
        payload = self._study([300, 420, 480, 540, 900])

        report = evaluate_novice_acceptance(payload)

        self.assertEqual(report.status, "passed")
        self.assertEqual(report.median_first_success_seconds, 480)
        self.assertTrue(report.terminal_free)
        self.assertEqual(report.issues, ())

    def test_fewer_than_five_valid_sessions_remains_pending(self) -> None:
        report = evaluate_novice_acceptance(self._study([300, 420, 480, 540]))

        self.assertEqual(report.status, "pending")
        self.assertIn("need at least 5 participants", report.issues[0])

    def test_terminal_use_fails_basic_flow_gate(self) -> None:
        payload = self._study([300, 420, 480, 540, 600])
        payload["participants"][2]["used_terminal"] = True

        report = evaluate_novice_acceptance(payload)

        self.assertEqual(report.status, "failed")
        self.assertFalse(report.terminal_free)
        self.assertTrue(any("used_terminal" in issue for issue in report.issues))

    def test_missing_task_or_traceability_fails(self) -> None:
        payload = self._study([300, 420, 480, 540, 600])
        payload["participants"][0]["tasks"]["located_failure_reason"] = False
        payload["participants"][1]["evidence_refs"] = []

        report = evaluate_novice_acceptance(payload)

        self.assertEqual(report.status, "failed")
        self.assertTrue(any("located_failure_reason" in issue for issue in report.issues))
        self.assertTrue(any("evidence_refs" in issue for issue in report.issues))

    def test_automation_cannot_be_counted_as_human_evidence(self) -> None:
        payload = self._study([300, 420, 480, 540, 600])
        payload["participants"][4]["automated"] = True

        report = evaluate_novice_acceptance(payload)

        self.assertEqual(report.status, "failed")
        self.assertTrue(any("automated must be false" in issue for issue in report.issues))

    def test_median_over_ten_minutes_fails(self) -> None:
        report = evaluate_novice_acceptance(self._study([601, 620, 640, 660, 680]))

        self.assertEqual(report.status, "failed")
        self.assertEqual(report.median_first_success_seconds, 640)

    def test_duplicate_contract_or_success_run_cannot_count_twice(self) -> None:
        payload = self._study([300, 420, 480, 540, 600])
        payload["participants"][1]["contract_id"] = payload["participants"][0]["contract_id"]
        payload["participants"][2]["success_run_id"] = payload["participants"][0]["success_run_id"]

        report = evaluate_novice_acceptance(payload)

        self.assertEqual(report.status, "failed")
        self.assertTrue(any("contract_id must be unique" in item for item in report.issues))
        self.assertTrue(any("success_run_id must be unique" in item for item in report.issues))

    def test_live_study_readiness_requires_fixed_identity_and_real_failure(self) -> None:
        report = evaluate_novice_study_readiness(
            session={"identity_mode": "fixed_user", "user": "alice", "switchable": False},
            templates={"items": [self._eligible_template()]},
            failure_run={
                "run_id": "run_failure",
                "owner": "alice",
                "state": "FAILED",
                "submit_strategy": "command",
                "job_id": "44",
                "collection_state": "succeeded",
                "diagnosis_state": "succeeded",
            },
            failure_evidence={
                "objects": [
                    {"logical_path": "logs/stdout.tail.json"},
                    {"logical_path": "derived/result_summary.v1.json"},
                    {"logical_path": "slurm/accounting.json"},
                ]
            },
            failure_diagnoses={
                "items": [
                    {
                        "rule_id": "RUNTIME.NONZERO_EXIT",
                        "evidence_refs": ["evidence://runs/run_failure/slurm/accounting.json"],
                    }
                ]
            },
        )

        self.assertEqual(report.status, "ready")
        self.assertEqual(report.template_release_id, "release_cpu")
        self.assertEqual(report.issues, ())

    def test_demo_or_degraded_failure_is_not_study_ready(self) -> None:
        report = evaluate_novice_study_readiness(
            session={"identity_mode": "demo", "user": "alice", "switchable": True},
            templates={"items": [self._eligible_template()]},
            failure_run={
                "run_id": "run_failure",
                "owner": "alice",
                "state": "FAILED",
                "submit_strategy": "demo",
                "job_id": "demo-1",
                "collection_state": "degraded",
                "diagnosis_state": "skipped",
            },
            failure_evidence={"objects": []},
            failure_diagnoses={"items": []},
        )

        self.assertEqual(report.status, "not_ready")
        self.assertGreaterEqual(len(report.issues), 6)

    @staticmethod
    def _eligible_template() -> dict:
        return {
            "release_id": "release_cpu",
            "withdrawn_at": None,
            "payload": {"recipe_version_id": "recipe_python_cpu@1.0.0"},
            "compatibility": {"gpu": False},
            "gate_report": {"status": "OK"},
        }

    def _study(self, durations: list[int]) -> dict:
        started = datetime(2026, 7, 16, 8, 0, tzinfo=UTC)
        participants = []
        for index, duration in enumerate(durations, start=1):
            run_id = f"run_success_{index}"
            participants.append(
                {
                    "participant_id": f"p{index:02d}",
                    "slurm_experience": "none",
                    "automated": False,
                    "completed": True,
                    "used_terminal": False,
                    "started_at": started.isoformat(),
                    "first_success_at": (started + timedelta(seconds=duration)).isoformat(),
                    "contract_id": f"contract_{index}",
                    "success_run_id": run_id,
                    "failure_run_id": f"run_failure_{index}",
                    "evidence_refs": [
                        f"evidence://runs/{run_id}/logs/stdout.tail.json",
                        f"evidence://runs/{run_id}/outputs/inventory.json",
                        f"evidence://runs/run_failure_{index}/logs/stderr.tail.json",
                    ],
                    "tasks": {task: True for task in REQUIRED_TASKS},
                }
            )
        return {
            "schema_version": "pilot107.novice-acceptance/v1",
            "study_id": "phase3d-study-test",
            "evidence_source": "facilitated_human_study",
            "participants": participants,
        }
