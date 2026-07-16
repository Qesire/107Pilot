from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "data" / "submission_templates"


def test_submission_templates_are_indexed_and_include_success_protocol() -> None:
    index = (TEMPLATE_DIR / "INDEX.yaml").read_text(encoding="utf-8")
    expected_files = {
        "recipe_student_cpu_basic.yaml",
        "recipe_student_gpu_array.yaml",
        "recipe_resilient_submission.yaml",
    }

    for filename in expected_files:
        path = TEMPLATE_DIR / filename
        content = path.read_text(encoding="utf-8")
        assert filename in index
        assert "parameter_schema:" in content
        assert "compatibility:" in content
        assert "sbatch_template: |" in content
        assert "set -Eeuo pipefail" in content
        assert "success_protocol:" in content
        assert "COMPLETE" in content

