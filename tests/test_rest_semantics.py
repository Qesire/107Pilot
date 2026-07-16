import unittest

from pilot107.core.rest_semantics import RestSemanticLevel, check_slurm_rest_semantics


class RestSemanticTests(unittest.TestCase):
    def test_http_payload_with_errors_is_semantic_error(self) -> None:
        result = check_slurm_rest_semantics(
            {"errors": [{"description": "bad partition"}], "warnings": []}
        )

        self.assertEqual(result.level, RestSemanticLevel.ERROR)
        self.assertFalse(result.is_success)

    def test_missing_required_field_is_semantic_error(self) -> None:
        result = check_slurm_rest_semantics(
            {"errors": [], "warnings": []}, required_fields=["jobs"]
        )

        self.assertEqual(result.level, RestSemanticLevel.ERROR)
        self.assertEqual(result.missing_fields, ["jobs"])

    def test_warnings_are_success_with_warning(self) -> None:
        result = check_slurm_rest_semantics({"errors": [], "warnings": ["deprecated"]})

        self.assertEqual(result.level, RestSemanticLevel.WARNING)
        self.assertTrue(result.is_success)

    def test_partial_payload_with_errors_can_be_warning(self) -> None:
        result = check_slurm_rest_semantics(
            {
                "errors": [{"description": "Slurmdb query failed"}],
                "warnings": [{"description": "TRES unavailable"}],
                "partitions": [{"name": "Students"}],
            },
            required_fields=["partitions"],
            partial_fields=["partitions"],
        )

        self.assertEqual(result.level, RestSemanticLevel.WARNING)
        self.assertTrue(result.is_success)
        self.assertEqual(result.errors[0]["description"], "Slurmdb query failed")


if __name__ == "__main__":
    unittest.main()
