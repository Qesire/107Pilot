import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from pilot107.core.contract_v2 import (
    CONTRACT_SCHEMA_V2,
    ContractV2Error,
    normalize_contract,
    parse_expected_output,
)
from pilot107.core.contracts import (
    ContractError,
    ContractService,
    ContractStore,
    RecipeCatalog,
    RecipeVersion,
)


class ContractV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "pilot107.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_normalizes_legacy_contract_without_losing_advanced_fields(self) -> None:
        payload = _legacy_contract()
        payload["owner"] = "transport-owner"
        payload["runtime"] = {
            "conda_env": "ml",
            "modules": ["cuda/12.4"],
            "environment": {"TOKENIZERS_PARALLELISM": "false"},
        }
        payload["workflow"] = {
            "dependencies": ["run_parent"],
            "retry": {"max_attempts": 3, "backoff_seconds": 15},
        }
        payload["extensions"] = {"faculty.example": {"priority": "teaching"}}

        normalized = normalize_contract(payload)

        self.assertEqual(normalized["schema_version"], CONTRACT_SCHEMA_V2)
        self.assertNotIn("owner", normalized)
        self.assertEqual(normalized["runtime"]["conda_env"], "ml")
        self.assertEqual(normalized["workflow"]["retry"]["max_attempts"], 3)
        self.assertEqual(normalized["outputs"]["expected"], ["result.txt"])
        self.assertEqual(normalized["extensions"], payload["extensions"])

    def test_rejects_unknown_top_level_field(self) -> None:
        payload = _legacy_contract()
        payload["typo_resources"] = {}

        with self.assertRaisesRegex(ContractV2Error, "unknown top-level"):
            normalize_contract(payload)

    def test_rejects_invalid_workflow_boundaries_before_persistence(self) -> None:
        duplicate = _legacy_contract()
        duplicate["workflow"] = {"dependencies": ["run_parent", "run_parent"]}
        excessive_backoff = _legacy_contract()
        excessive_backoff["workflow"] = {
            "retry": {"max_attempts": 2, "backoff_seconds": 86401}
        }

        with self.assertRaises(ContractV2Error) as duplicate_error:
            normalize_contract(duplicate)
        with self.assertRaises(ContractV2Error) as backoff_error:
            normalize_contract(excessive_backoff)

        self.assertEqual(
            duplicate_error.exception.code,
            "CONTRACT.WORKFLOW_DEPENDENCIES_INVALID",
        )
        self.assertEqual(
            backoff_error.exception.code,
            "CONTRACT.WORKFLOW_RETRY_INVALID",
        )

    def test_runtime_is_rendered_while_retry_approval_policy_is_explicit(self) -> None:
        store = ContractStore(self.db_path)
        service = ContractService(catalog=RecipeCatalog(store=store), store=store)
        payload = _legacy_contract()
        payload["runtime"] = {"conda_env": "ml"}
        payload["workflow"] = {
            "dependencies": ["run_parent"],
            "retry": {"max_attempts": 2, "backoff_seconds": 5},
        }

        result = service.validate(payload)

        self.assertEqual(result.status, "OK")
        self.assertEqual(
            {finding.code for finding in result.findings},
            {"WORKFLOW.RETRY_APPROVAL_REQUIRED"},
        )
        self.assertEqual(result.effective_request["contract"]["runtime"]["conda_env"], "ml")
        self.assertIn("conda activate ml", result.effective_request["script"])

    def test_migrates_existing_contract_row_to_v2(self) -> None:
        payload = _legacy_contract()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE contracts (
                    contract_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    recipe_version_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    field_sources_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO contracts VALUES (?, ?, ?, ?, '[]', ?, ?)",
                (
                    "contract_legacy",
                    "alice",
                    "recipe_python_cpu@1.0.0",
                    json.dumps(payload),
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )

        record = ContractStore(self.db_path).get_contract("contract_legacy")

        self.assertEqual(record.schema_version, CONTRACT_SCHEMA_V2)
        self.assertEqual(record.payload["schema_version"], CONTRACT_SCHEMA_V2)
        self.assertEqual(len(record.digest), 64)
        self.assertEqual(record.payload["outputs"]["expected"], ["result.txt"])

    def test_packaged_recipes_are_persisted_and_report_materializer_status(self) -> None:
        store = ContractStore(self.db_path)
        catalog = RecipeCatalog(store=store)

        versions = catalog.list_versions()
        packaged = [recipe for recipe in versions if recipe.source.startswith("packaged:")]
        template_dir = Path(__file__).resolve().parents[1] / "data/submission_templates"
        expected_packaged = len(
            [path for path in template_dir.glob("*.yaml") if path.name != "INDEX.yaml"]
        )

        self.assertEqual(len(packaged), expected_packaged)
        self.assertEqual(len(store.list_recipe_versions()), expected_packaged + 1)
        self.assertTrue(all(len(recipe.content_sha256) == 64 for recipe in packaged))
        self.assertTrue(all(recipe.materializer == "sbatch_template_v1" for recipe in packaged))

    def test_packaged_recipe_reports_missing_template_values_without_fallback(self) -> None:
        store = ContractStore(self.db_path)
        service = ContractService(catalog=RecipeCatalog(store=store), store=store)
        payload = _legacy_contract()
        payload["recipe_version_id"] = "recipe_student_cpu_basic@1.0.0"

        result = service.validate(payload)

        self.assertEqual(result.status, "BLOCK")
        self.assertIsNone(result.effective_request["script"])
        self.assertIn(
            "MATERIALIZER.VALUE_REQUIRED",
            {finding.code for finding in result.findings},
        )

    def test_recipe_versions_are_immutable_and_latest_uses_semver(self) -> None:
        store = ContractStore(self.db_path)
        older = _recipe("recipe_versioned", "1.9.0", "older")
        newer = _recipe("recipe_versioned", "1.10.0", "newer")
        catalog = RecipeCatalog(recipes=[newer, older], store=store)

        summary = catalog.list_summaries()[0]

        self.assertEqual(summary.latest_version, "1.10.0")
        with self.assertRaises(ContractError) as raised:
            RecipeCatalog(
                recipes=[_recipe("recipe_versioned", "1.10.0", "mutated")],
                store=store,
            )
        self.assertEqual(raised.exception.code, "RECIPE.VERSION_IMMUTABLE")

    def test_legacy_capability_overlay_does_not_block_catalog_startup(self) -> None:
        store = ContractStore(self.db_path)
        legacy = replace(
            _recipe("recipe_profiled", "1.0.0", "profiled"),
            compatibility={
                "partitions": {"default": "Students", "allowed": ["Students"]},
                "qos": {
                    "default": "qos_stu_default",
                    "allowed_by_partition": {"Students": ["qos_stu_default"]},
                },
            },
        )
        RecipeCatalog(recipes=[legacy], store=store)

        catalog = RecipeCatalog(
            recipes=[_recipe("recipe_profiled", "1.0.0", "profiled")],
            store=store,
            partition_qos={"CPU-RC": ("qos_cpu_rc",)},
            default_partition="CPU-RC",
            default_qos="qos_cpu_rc",
        )

        recipe = catalog.get("recipe_profiled", "1.0.0")
        self.assertEqual(recipe.compatibility["partitions"]["default"], "CPU-RC")
        self.assertEqual(recipe.compatibility["qos"]["default"], "qos_cpu_rc")

    def test_recipe_latest_obeys_semver_prerelease_precedence(self) -> None:
        catalog = RecipeCatalog(
            recipes=[
                _recipe("recipe_preview", "1.0.0-beta.2", "beta 2"),
                _recipe("recipe_preview", "1.0.0-beta.10", "beta 10"),
            ]
        )

        self.assertEqual(catalog.list_summaries()[0].latest_version, "1.0.0-beta.10")

    def test_invalid_legacy_contract_does_not_prevent_store_startup(self) -> None:
        payload = _legacy_contract()
        payload["unknown"] = "preserve for manual repair"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE contracts (
                    contract_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    recipe_version_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    field_sources_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO contracts VALUES (?, ?, ?, ?, '[]', ?, ?)",
                (
                    "contract_needs_repair",
                    "alice",
                    "recipe_python_cpu@1.0.0",
                    json.dumps(payload),
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )

        record = ContractStore(self.db_path).get_contract("contract_needs_repair")

        self.assertEqual(record.schema_version, "pilot107.contract/v1")
        self.assertEqual(record.payload["unknown"], "preserve for manual repair")


class ParseExpectedOutputTests(unittest.TestCase):
    """Round-8 P2-2: the shared parser extracts a path string from an
    ``outputs.expected`` entry (str or typed object) and rejects bad shapes."""

    def test_str_item_returned(self) -> None:
        self.assertEqual(parse_expected_output("metrics.json"), "metrics.json")

    def test_str_item_stripped(self) -> None:
        self.assertEqual(parse_expected_output("  metrics.json  "), "metrics.json")

    def test_empty_str_raises(self) -> None:
        with self.assertRaises(ContractV2Error) as raised:
            parse_expected_output("   ")
        self.assertEqual(raised.exception.code, "CONTRACT.OUTPUTS_INVALID")

    def test_dict_with_path_returned(self) -> None:
        self.assertEqual(
            parse_expected_output({"path": "metrics.json", "type": "json"}),
            "metrics.json",
        )

    def test_dict_path_stripped(self) -> None:
        self.assertEqual(
            parse_expected_output({"path": "  metrics.json  "}),
            "metrics.json",
        )

    def test_dict_without_path_raises(self) -> None:
        with self.assertRaises(ContractV2Error) as raised:
            parse_expected_output({"type": "json"})
        self.assertEqual(raised.exception.code, "CONTRACT.OUTPUTS_INVALID")
        self.assertIn("path", str(raised.exception))

    def test_dict_with_non_string_path_raises(self) -> None:
        with self.assertRaises(ContractV2Error) as raised:
            parse_expected_output({"path": 123})
        self.assertEqual(raised.exception.code, "CONTRACT.OUTPUTS_INVALID")

    def test_dict_with_empty_path_raises(self) -> None:
        with self.assertRaises(ContractV2Error) as raised:
            parse_expected_output({"path": ""})
        self.assertEqual(raised.exception.code, "CONTRACT.OUTPUTS_INVALID")

    def test_non_str_non_dict_raises(self) -> None:
        for bad in (123, 1.5, None, True, ["metrics.json"], ("a", "b")):
            with self.subTest(value=bad):
                with self.assertRaises(ContractV2Error) as raised:
                    parse_expected_output(bad)
                self.assertEqual(raised.exception.code, "CONTRACT.OUTPUTS_INVALID")


class SuccessConditionsValidationTests(unittest.TestCase):
    """Round-8 P2-3: reject unsupported success_conditions at normalize time."""

    def test_default_success_conditions_normalized_and_valid(self) -> None:
        payload = _legacy_contract()
        # No success_conditions field; normalize should default + validate.
        normalized = normalize_contract(payload)
        self.assertEqual(
            normalized["outputs"]["success_conditions"],
            ["slurm_exit_code_zero"],
        )

    def test_supported_condition_ok(self) -> None:
        payload = _legacy_contract()
        payload["outputs"] = {
            "expected": ["result.txt"],
            "success_conditions": ["slurm_exit_code_zero"],
        }
        normalized = normalize_contract(payload)
        self.assertEqual(
            normalized["outputs"]["success_conditions"],
            ["slurm_exit_code_zero"],
        )

    def test_empty_conditions_ok(self) -> None:
        # Empty list = no conditions; evaluation falls back to exit-code-only.
        payload = _legacy_contract()
        payload["outputs"] = {
            "expected": ["result.txt"],
            "success_conditions": [],
        }
        normalized = normalize_contract(payload)
        self.assertEqual(normalized["outputs"]["success_conditions"], [])

    def test_unsupported_condition_rejected(self) -> None:
        payload = _legacy_contract()
        payload["outputs"] = {
            "expected": ["result.txt"],
            "success_conditions": ["slurm_exit_code_zero", "custom_metric_above_5"],
        }
        with self.assertRaises(ContractV2Error) as raised:
            normalize_contract(payload)
        self.assertEqual(
            raised.exception.code,
            "CONTRACT.SUCCESS_CONDITION_UNSUPPORTED",
        )
        self.assertIn("custom_metric_above_5", str(raised.exception))
        self.assertIn("slurm_exit_code_zero", str(raised.exception))

    def test_typed_expected_output_object_normalizes(self) -> None:
        # P2-2 integration: a typed object expected output survives normalize
        # (the parser accepts it at validate time).
        payload = _legacy_contract()
        payload["outputs"] = {
            "expected": [{"path": "metrics.json", "type": "json"}],
            "success_conditions": ["slurm_exit_code_zero"],
        }
        normalized = normalize_contract(payload)
        self.assertEqual(
            normalized["outputs"]["expected"],
            [{"path": "metrics.json", "type": "json"}],
        )

    def test_typed_expected_output_missing_path_rejected_at_normalize(self) -> None:
        # P2-2 defensive: a dict without a string path is rejected at normalize.
        payload = _legacy_contract()
        payload["outputs"] = {
            "expected": [{"type": "json"}],
            "success_conditions": ["slurm_exit_code_zero"],
        }
        with self.assertRaises(ContractV2Error) as raised:
            normalize_contract(payload)
        self.assertEqual(raised.exception.code, "CONTRACT.OUTPUTS_INVALID")


def _legacy_contract() -> dict:
    return {
        "recipe_version_id": "recipe_python_cpu@1.0.0",
        "project": {"workdir": "/public/home/alice"},
        "entry": {"command": "echo ok", "expected_outputs": ["result.txt"]},
        "resources": {
            "partition": "debug",
            "qos": "normal",
            "nodes": 1,
            "ntasks": 1,
            "cpus_per_task": 1,
            "memory_value": 512,
            "memory_unit": "M",
            "time_limit": "00:05:00",
        },
    }


def _recipe(recipe_id: str, version: str, title: str) -> RecipeVersion:
    return RecipeVersion(
        recipe_id=recipe_id,
        version=version,
        title=title,
        description="version test",
        trust_level="L1",
        parameter_schema={"required": []},
        compatibility={},
        risk_declaration={},
    )


if __name__ == "__main__":
    unittest.main()
