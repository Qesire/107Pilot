import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/report_sim_behavior_fidelity.py"

spec = importlib.util.spec_from_file_location("report_sim_behavior_fidelity", SCRIPT)
assert spec is not None
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class SimulatorBehaviorReportTests(unittest.TestCase):
    def test_build_report_marks_fallback_version_as_limited(self) -> None:
        profile = {
            "schema": "pilot107.simulator_real107_behavior.v1",
            "profile_id": "simulator-real107-behavior",
            "slurm": {
                "target_version": "25.11.x",
                "fallback_version": "23.11.x",
                "api_version": "v0.0.41",
                "accounting_storage_enforce": ["associations", "qos", "limits"],
                "auth": {
                    "rest_primary": "jwt_bearer",
                    "simulator_fallback": "trusted_header_simulated_users",
                },
            },
        }
        manifest = {
            "image_family": "pilot107/slurm-sim",
            "target": {"slurm_version": "25.11.x"},
            "fallback": {"slurm_version": "23.11.x"},
            "runtime_fidelity": {
                "scheduler_gres": "fake",
                "cuda_driver": "unavailable",
                "nvml": "unavailable",
                "real_gpu_devices": "unavailable",
            },
        }
        commands = [
            module.CommandResult(
                "scontrol_version",
                ("scontrol", "--version"),
                0,
                "slurm 23.11.4\n",
                "",
            ),
            module.CommandResult(
                "scontrol_show_part",
                ("scontrol", "show", "part"),
                0,
                "PartitionName=Students AllowQos=qos_stu_default\n",
                "",
            ),
            module.CommandResult(
                "qos_table",
                ("sacctmgr", "show", "qos"),
                0,
                "qos_stu_medium_2gpu\n",
                "",
            ),
            module.CommandResult(
                "assoc_table",
                ("sacctmgr", "show", "assoc"),
                0,
                "bob|students|normal,qos_stu_default|qos_stu_default\n",
                "",
            ),
            module.CommandResult("rest_auth_probe", ("probe",), 0, "", ""),
        ]
        observations = {
            "commands": commands,
            "rest_probe": {"summary": {"status": "supported"}},
            "behavior_checks": [
                {"id": "invalid_qos", "expected": "rejected", "status": "pass"},
                {
                    "id": "limited_user_unauthorized_qos",
                    "expected": "rejected",
                    "status": "pass",
                },
                {
                    "id": "student_competition_account_overreach",
                    "expected": "rejected",
                    "status": "pass",
                },
                {"id": "limited_student_cpu", "expected": "completed", "status": "pass"},
                {
                    "id": "legal_student_gpu_scheduler",
                    "expected": "completed",
                    "status": "pass",
                },
            ],
        }

        report = module.build_report(
            profile=profile,
            manifest=manifest,
            generated_at="2026-07-15T00:00:00+00:00",
            observations=observations,
        )

        self.assertEqual(report["schema"], "pilot107.simulator_real_behavior_fidelity.v1")
        self.assertEqual(report["summary"]["status"], "limited")
        self.assertEqual(report["slurm"]["version_status"], "fallback")
        self.assertEqual(report["scheduler_fidelity"]["association"], "pass")
        self.assertIn("25.11 target image is not yet default", report["known_differences"][0])

    def test_build_report_marks_target_rest_supported_without_fallback_difference(self) -> None:
        profile = {
            "schema": "pilot107.simulator_real107_behavior.v1",
            "profile_id": "simulator-real107-behavior",
            "slurm": {
                "target_version": "25.11.2",
                "fallback_version": "23.11.x",
                "api_version": "v0.0.41",
                "accounting_storage_enforce": ["associations", "qos", "limits"],
                "auth": {
                    "rest_primary": "jwt_bearer",
                    "simulator_fallback": "trusted_header_simulated_users",
                },
            },
        }
        manifest = {
            "image_family": "pilot107/slurm-sim",
            "target": {"slurm_version": "25.11.2", "status": "current"},
            "fallback": {"slurm_version": "23.11.x", "status": "fallback"},
            "runtime_fidelity": {
                "scheduler_gres": "fake",
                "cuda_driver": "unavailable",
                "nvml": "unavailable",
                "real_gpu_devices": "unavailable",
            },
        }
        observations = {
            "commands": [
                module.CommandResult(
                    "scontrol_version",
                    ("scontrol", "--version"),
                    0,
                    "slurm 25.11.2\n",
                    "",
                ),
                module.CommandResult(
                    "scontrol_show_part",
                    ("scontrol", "show", "part"),
                    0,
                    "PartitionName=Students\n",
                    "",
                ),
                module.CommandResult(
                    "qos_table",
                    ("sacctmgr", "show", "qos"),
                    0,
                    "qos_stu_medium_2gpu\n",
                    "",
                ),
                module.CommandResult(
                    "assoc_table",
                    ("sacctmgr", "show", "assoc"),
                    0,
                    "bob|students|normal,qos_stu_default|qos_stu_default\n",
                    "",
                ),
                module.CommandResult("rest_auth_probe", ("probe",), 0, "", ""),
            ],
            "rest_probe": {"summary": {"status": "supported"}},
            "behavior_checks": [
                {"id": "invalid_qos", "expected": "rejected", "status": "pass"},
                {"id": "limited_user_unauthorized_qos", "expected": "rejected", "status": "pass"},
                {
                    "id": "student_competition_account_overreach",
                    "expected": "rejected",
                    "status": "pass",
                },
                {"id": "limited_student_cpu", "expected": "completed", "status": "pass"},
                {"id": "legal_student_gpu_scheduler", "expected": "completed", "status": "pass"},
            ],
        }

        report = module.build_report(
            profile=profile,
            manifest=manifest,
            generated_at="2026-07-15T00:00:00+00:00",
            observations=observations,
        )

        self.assertEqual(report["slurm"]["version_status"], "target")
        self.assertEqual(report["rest_api"]["probe_status"], "supported")
        self.assertIsNone(report["rest_api"]["known_difference"])
        self.assertNotIn("fallback", " ".join(report["known_differences"]).lower())

    def test_profile_loader_reads_behavior_matrix(self) -> None:
        profile = module.load_simple_yaml(
            ROOT / "config/platform_profiles/simulator-real107-behavior.yaml"
        )

        self.assertEqual(profile["profile_id"], "simulator-real107-behavior")
        self.assertEqual(
            profile["behavior_matrix"]["limited_user_unauthorized_qos"]["expected"],
            "rejected",
        )


if __name__ == "__main__":
    unittest.main()
