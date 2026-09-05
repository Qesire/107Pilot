from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "data" / "submission_templates"


def test_submission_templates_are_indexed_and_include_success_protocol() -> None:
    index = (TEMPLATE_DIR / "INDEX.yaml").read_text(encoding="utf-8")
    expected_files = {
        "recipe_fail_closed_merge_gate.yaml",
        "recipe_gpu_shard_array_atomic.yaml",
        "recipe_student_cpu_basic.yaml",
        "recipe_student_gpu_array.yaml",
        "recipe_resilient_submission.yaml",
        "recipe_structured_preflight_gate.yaml",
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


def test_runbook_derived_templates_encode_recovery_invariants() -> None:
    gpu = (TEMPLATE_DIR / "recipe_gpu_shard_array_atomic.yaml").read_text()
    merge = (TEMPLATE_DIR / "recipe_fail_closed_merge_gate.yaml").read_text()
    preflight = (TEMPLATE_DIR / "recipe_structured_preflight_gate.yaml").read_text()

    assert "CUDA_VISIBLE_DEVICES" in gpu
    assert "node-local output -> shared tmp" in gpu
    assert "metadata-pattern" in merge
    assert "partial_merge_forbidden" in merge
    assert "structured_contract_consistency" in preflight
    assert "effective-contract.json" in preflight


def test_runbook_derived_templates_have_explicit_portable_resource_headers() -> None:
    filenames = {
        "recipe_structured_preflight_gate.yaml",
        "recipe_gpu_shard_array_atomic.yaml",
        "recipe_fail_closed_merge_gate.yaml",
    }
    for filename in filenames:
        content = (TEMPLATE_DIR / filename).read_text()
        assert content.count("#SBATCH --account=") == 1
        assert content.count("#SBATCH --partition=") == 1
        assert content.count("#SBATCH --qos=") == 1
        assert content.count("#SBATCH --mem=") == 1
        assert "/home/scc/" not in content
        assert "pb230" not in content

    gpu = (TEMPLATE_DIR / "recipe_gpu_shard_array_atomic.yaml").read_text()
    assert "max_concurrency: 2" in gpu
    assert 'CUDA_VISIBLE_DEVICES" != NoDevFiles' in gpu
