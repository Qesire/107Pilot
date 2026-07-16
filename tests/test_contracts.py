import tempfile
import unittest
from pathlib import Path

from pilot107.core.contracts import (
    ContractService,
    ContractStore,
    RecipeCatalog,
    render_submitted_script,
)
from pilot107.core.resources import REAL107_SIM_PARTITION_QOS, QosResourceLimit


class ContractServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = ContractService(
            catalog=RecipeCatalog(),
            store=ContractStore(Path(self._tmp.name) / "pilot107.db"),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_lists_builtin_recipe(self) -> None:
        summaries = self.service.catalog.list_summaries()

        self.assertEqual(summaries[0].recipe_id, "recipe_python_cpu")
        self.assertEqual(summaries[0].latest_version, "1.0.0")

    def test_validate_returns_effective_request(self) -> None:
        result = self.service.validate(_contract_payload())

        self.assertEqual(result.status, "OK")
        self.assertEqual(result.findings, [])
        self.assertEqual(result.effective_request["workdir"], "/public/home/alice")
        self.assertIn("echo contract-ok", result.effective_request["script"])
        self.assertEqual(result.effective_request["resource_plan"]["partition"], "debug")

    def test_validate_blocks_invalid_resource_plan(self) -> None:
        payload = _contract_payload()
        payload["resources"]["time_limit"] = None

        result = self.service.validate(payload)

        self.assertEqual(result.status, "BLOCK")
        self.assertEqual(result.findings[0].code, "RESOURCE.TIME_LIMIT_REQUIRED")

    def test_create_persists_contract_and_to_submit_request(self) -> None:
        contract = self.service.create(owner="alice", payload=_contract_payload())

        loaded = self.service.get(contract.contract_id)
        request = self.service.to_submit_request(loaded)

        self.assertEqual(loaded.owner, "alice")
        self.assertEqual(loaded.recipe_version_id, "recipe_python_cpu@1.0.0")
        self.assertEqual(request.owner, "alice")
        self.assertEqual(request.workdir, Path("/public/home/alice"))
        self.assertIn("echo contract-ok", request.script)
        self.assertEqual(request.resource_plan.partition, "debug")

    def test_render_quotes_workdir(self) -> None:
        payload = _contract_payload()
        payload["project"]["workdir"] = "/public/home/alice/has space"

        script = render_submitted_script(payload)

        self.assertIn("cd '/public/home/alice/has space'", script)

    def test_real107_sim_profile_blocks_qos_mismatch(self) -> None:
        service = ContractService(
            catalog=RecipeCatalog(),
            store=ContractStore(Path(self._tmp.name) / "profile.db"),
            partition_qos=REAL107_SIM_PARTITION_QOS,
        )
        payload = _contract_payload()
        payload["resources"]["partition"] = "Students"
        payload["resources"]["qos"] = "qos_p107-a100"

        result = service.validate(payload)

        self.assertEqual(result.status, "BLOCK")
        self.assertTrue(any(f.code == "RESOURCE.QOS_NOT_ALLOWED" for f in result.findings))

    def test_validate_blocks_qos_numeric_limits(self) -> None:
        service = ContractService(
            catalog=RecipeCatalog(),
            store=ContractStore(Path(self._tmp.name) / "limits.db"),
            partition_qos=REAL107_SIM_PARTITION_QOS,
            qos_limits={
                "qos_stu_default": QosResourceLimit(
                    max_cpus=4,
                    max_gpus=1,
                    max_memory_gb=16,
                    max_wall_hours=4,
                    source_authority="docs-main",
                )
            },
        )
        payload = _contract_payload()
        payload["resources"]["partition"] = "Students"
        payload["resources"]["qos"] = "qos_stu_default"
        payload["resources"]["cpus_per_task"] = 8
        payload["resources"]["gpus_total"] = 2
        payload["resources"]["memory"] = "32G"
        payload["resources"]["time_limit"] = "05:00:00"

        result = service.validate(payload)

        self.assertEqual(result.status, "BLOCK")
        self.assertIn(
            "RESOURCE.QOS_CPU_LIMIT_EXCEEDED",
            {finding.code for finding in result.findings},
        )


def _contract_payload() -> dict:
    return {
        "recipe_version_id": "recipe_python_cpu@1.0.0",
        "project": {
            "workdir": "/public/home/alice",
        },
        "entry": {
            "command": "echo contract-ok",
            "expected_outputs": ["result.txt"],
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
