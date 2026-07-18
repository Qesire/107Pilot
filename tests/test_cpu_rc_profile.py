from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pilot107.api.service import build_api_service, config_from_env
from pilot107.core.platform import load_capability_profile

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "config/platform_profiles/cpu-only-8c16g.json"


class CpuReleaseCandidateProfileTests(unittest.TestCase):
    def test_capability_profile_is_cpu_only_and_reserves_host_capacity(self) -> None:
        profile = load_capability_profile(PROFILE)

        self.assertEqual(profile.profile_id, "cpu-only-8c16g-rc")
        self.assertEqual(profile.default_partition, "CPU-RC")
        self.assertEqual(profile.default_qos, "qos_cpu_rc")
        self.assertTrue(all(not partition.gpu_types for partition in profile.partitions))
        self.assertTrue(all(qos.max_gpus == 0 for qos in profile.qos))
        self.assertLessEqual(max(qos.max_cpus or 0 for qos in profile.qos), 4)
        self.assertLessEqual(max(qos.max_memory_gb or 0 for qos in profile.qos), 6)

    def test_slurm_and_compose_envelopes_do_not_expose_gpu_resources(self) -> None:
        slurm = (ROOT / "simulator/compose/slurm-cpu-rc/slurm.conf").read_text()
        compose = (ROOT / "simulator/compose/compose.cpu-rc.yml").read_text()

        self.assertIn("NodeName=anode16 CPUs=4", slurm)
        self.assertIn("RealMemory=6144", slurm)
        self.assertIn("PartitionName=CPU-RC", slurm)
        self.assertNotIn("GresTypes", slurm)
        self.assertNotIn("GPU", slurm)
        self.assertIn('PILOT107_ALLOW_GPU_RECIPES: "false"', compose)
        self.assertIn("cpus: 4.0", compose)
        self.assertNotIn("worker-2:", compose)
        self.assertIn("slurmctld-state:/var/spool/slurm/ctld", compose)

    def test_cpu_api_hides_gpu_recipes_and_reports_only_cpu_capability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = config_from_env(
                {
                    "PILOT107_CAPABILITY_PROFILE_PATH": str(PROFILE),
                    "PILOT107_CONTRACT_PROFILE": "cpu-only",
                    "PILOT107_ALLOW_GPU_RECIPES": "false",
                },
                project_root=root,
            )
            api = build_api_service(config)
            recipes = api.handle_get("/api/v1/recipes").payload["items"]
            recipe = api.handle_get("/api/v1/recipes/recipe_python_cpu/versions/1.0.0").payload
            capability = api.handle_get("/api/v1/platform/capabilities").payload

        self.assertTrue(recipes)
        self.assertFalse(any("gpu" in item["recipe_id"].lower() for item in recipes))
        self.assertEqual(recipe["compatibility"]["partitions"]["allowed"], ["CPU-RC"])
        self.assertEqual(recipe["compatibility"]["qos"]["default"], "qos_cpu_rc")
        self.assertEqual([item["name"] for item in capability["partitions"]], ["CPU-RC"])
        self.assertEqual(capability["qos"][0]["max_gpus"], 0)

    def test_env_template_requires_generated_credentials_and_fixed_images(self) -> None:
        env = (ROOT / "simulator/compose/.env.cpu-rc.example").read_text()
        profile = json.loads(PROFILE.read_text())

        self.assertIn("REPLACE_WITH_RANDOM_GATEWAY_TOKEN", env)
        self.assertIn("PILOT107_API_IMAGE=pilot107/api:cpu-rc", env)
        self.assertEqual(profile["schema"], "pilot107.capability_profile.v1")

    def test_fresh_accounting_profile_is_seeded_before_controller_validation(self) -> None:
        script = (ROOT / "scripts/apply-cpu-rc-profile.sh").read_text()

        self.assertIn("exec -T slurmdbd sacctmgr", script)
        self.assertLess(script.index("add qos qos_cpu_rc"), script.index("scontrol ping"))
        self.assertIn("set QOS=qos_cpu_rc || true", script)
        self.assertIn("set DefaultQOS=qos_cpu_rc || true", script)


if __name__ == "__main__":
    unittest.main()
