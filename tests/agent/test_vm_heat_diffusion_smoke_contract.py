from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "scripts/smoke-vm-heat-diffusion-agent.py"
WRAPPER = ROOT / "scripts/smoke-vm-heat-diffusion-agent.sh"


def test_heat_smoke_is_high_level_model_driven_and_never_writes_solver_files() -> None:
    source = SMOKE.read_text()
    tree = ast.parse(source)

    assert '"origin": "blank"' in source
    assert '"profile_id": "experiment_builder"' in source
    assert '"model_profile_id": model_profile_id' in source
    assert '"cpus": 4' in source
    assert "review the bound Project and complete its approved validation workflow" in source
    assert "2D heat equation" in source
    assert "grids 64, 128, and 256" in source
    assert "audit_heat_diffusion_outputs" in source
    assert "/agent-changesets/" in source
    assert "/agent-sessions/" in source
    assert "/agent-tasks/" in source
    assert "/runs/" in source
    assert "/evidence" in source
    assert "/evidence/objects/" in source
    assert "/capsule" in source
    assert "/platform/snapshots/latest" in source

    forbidden = (
        "/patch",
        "/files/write",
        "#include",
        "omp_parallel",
        "int main(",
    )
    for token in forbidden:
        assert token not in source

    function_names = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    assert {
        "run_smoke",
        "_poll_turn",
        "_download_outputs",
        "_evidence_preview_text",
    } <= function_names


def test_heat_smoke_requires_model_identity_approvals_and_scientific_outputs() -> None:
    source = SMOKE.read_text()

    for token in (
        "campus-default",
        "qwen3.8-reasoner",
        "PILOT107_HEAT_SMOKE_AUTO_APPROVE",
        "turn_completed",
        "provider_timeout",
        "tool_step_budget_exhausted",
        "raw-results.csv",
        "convergence.json",
        "scaling.json",
        "report.md",
        "convergence.svg",
        "scaling.svg",
        "srun -c 1",
        "srun -c 2",
        "srun -c 4",
        "change_set_digest",
        "platform_snapshot_id",
        "formal_job_id",
        "capsule_ref",
    ):
        assert token in source


def test_heat_smoke_is_shipped_after_general_agent_lifecycle() -> None:
    acceptance = (ROOT / "scripts/accept-runtime-bundle.sh").read_text()
    exporter = (ROOT / "scripts/export-cpu-rc-bundle.sh").read_text()

    assert WRAPPER.is_file()
    assert "step_heat_diffusion_agent_demo" in acceptance
    assert acceptance.index("agent_task_lifecycle|step_agent_task_lifecycle") < acceptance.index(
        "heat_diffusion_agent_demo|step_heat_diffusion_agent_demo"
    )
    for name in ("smoke-vm-heat-diffusion-agent.py", "smoke-vm-heat-diffusion-agent.sh"):
        assert exporter.count(name) >= 2
