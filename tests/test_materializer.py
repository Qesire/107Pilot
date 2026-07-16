import tempfile
import unittest
from pathlib import Path

from pilot107.core.contracts import (
    ContractService,
    ContractStore,
    RecipeCatalog,
    RecipeVersion,
)


class ContractMaterializerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        store = ContractStore(Path(self._tmp.name) / "pilot107.db")
        self.service = ContractService(catalog=RecipeCatalog(store=store), store=store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_packaged_cpu_template_materializes_runtime_and_command(self) -> None:
        payload = _contract("recipe_student_cpu_basic@1.0.0")
        payload["runtime"] = {
            "modules": ["cuda/12.4"],
            "conda_env": "course-ml",
            "environment": {"KIT_ROOT": "/public/home/alice/kit", "OMP_NUM_THREADS": "4"},
        }

        result = self.service.validate(payload)

        self.assertEqual(result.status, "OK")
        script = result.effective_request["script"]
        self.assertIn("#SBATCH --partition=debug", script)
        self.assertIn("module load cuda/12.4", script)
        self.assertIn("export OMP_NUM_THREADS=4", script)
        self.assertIn("conda activate course-ml", script)
        self.assertIn("echo materialized > \"${tmp}\"", script)
        self.assertNotIn("{{", script)

    def test_gpu_array_template_materializes_array_and_gpu_values(self) -> None:
        payload = _contract("recipe_student_gpu_array@1.0.0")
        payload["resources"].update(
            {
                "partition": "P107-A100",
                "qos": "qos_p107-a100",
                "gpus_per_node": 1,
                "array": {"expression": "0-3", "max_concurrency": 2},
            }
        )
        payload["runtime"] = {
            "environment": {"KIT_ROOT": "/public/home/alice/kit"},
        }

        result = self.service.validate(payload)

        self.assertEqual(result.status, "OK")
        self.assertIn("#SBATCH --gres=gpu:1", result.effective_request["script"])
        self.assertIn("#SBATCH --array=0-3%2", result.effective_request["script"])

    def test_literal_secret_and_unverified_container_are_blocked(self) -> None:
        secret = _contract("recipe_python_cpu@1.0.0")
        secret["runtime"] = {"environment": {"API_KEY": "do-not-persist"}}
        container = _contract("recipe_python_cpu@1.0.0")
        container["runtime"] = {"container_image": "docker.io/example/course:latest"}

        secret_result = self.service.validate(secret)
        container_result = self.service.validate(container)

        self.assertEqual(secret_result.status, "BLOCK")
        self.assertEqual(container_result.status, "BLOCK")
        self.assertIn(
            "MATERIALIZER.SECRET_LITERAL_FORBIDDEN",
            {item.code for item in secret_result.findings},
        )
        self.assertIn(
            "MATERIALIZER.CONTAINER_CAPABILITY_REQUIRED",
            {item.code for item in container_result.findings},
        )

    def test_non_secret_tokenizer_environment_names_are_allowed(self) -> None:
        payload = _contract("recipe_python_cpu@1.0.0")
        payload["runtime"] = {
            "environment": {
                "TOKENIZERS_PARALLELISM": "false",
                "MAX_TOKENS": "4096",
            }
        }

        result = self.service.validate(payload)

        self.assertEqual(result.status, "OK")
        self.assertIn("export TOKENIZERS_PARALLELISM=false", result.effective_request["script"])

    def test_malformed_packaged_template_returns_structured_block(self) -> None:
        store = ContractStore(Path(self._tmp.name) / "malformed.db")
        recipe = RecipeVersion(
            recipe_id="recipe_malformed",
            version="1.0.0",
            title="Malformed template",
            description="Test-only malformed Jinja template.",
            trust_level="L1",
            parameter_schema={"required": []},
            compatibility={},
            risk_declaration={},
            materializer="sbatch_template_v1",
            sbatch_template="#!/bin/bash\n{{ broken\n",
        )
        service = ContractService(
            catalog=RecipeCatalog(recipes=[recipe], store=store),
            store=store,
        )

        result = service.validate(_contract(recipe.recipe_version_id))

        self.assertEqual(result.status, "BLOCK")
        self.assertIsNone(result.effective_request["script"])
        self.assertIn(
            "MATERIALIZER.TEMPLATE_ERROR",
            {item.code for item in result.findings},
        )


def _contract(recipe_version_id: str) -> dict:
    return {
        "recipe_version_id": recipe_version_id,
        "project": {"name": "materializer-test", "workdir": "/public/home/alice"},
        "entry": {"command": "echo materialized"},
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
