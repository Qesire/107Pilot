from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_vm_agent_task_smoke_requires_every_lifecycle_checkpoint() -> None:
    source = (ROOT / "scripts/smoke-vm-agent-task.py").read_text()

    for token in (
        "sandbox_succeeded",
        "task_id",
        "linked_run_id",
        "job_id",
        "evidence_refs",
        "capsule_state",
        "followup_turn_id",
    ):
        assert token in source


def test_vm_agent_task_smoke_is_a_required_runtime_bundle_step() -> None:
    acceptance = (ROOT / "scripts/accept-runtime-bundle.sh").read_text()
    exporter = (ROOT / "scripts/export-cpu-rc-bundle.sh").read_text()

    assert "step_agent_task_lifecycle" in acceptance
    assert "scripts/smoke-vm-agent-task.sh" in acceptance
    assert acceptance.index("image_binding|step_image_binding") < acceptance.index(
        "agent_task_lifecycle|step_agent_task_lifecycle"
    )
    for name in ("smoke-vm-agent-task.py", "smoke-vm-agent-task.sh"):
        assert name in exporter
