from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
_SPEC = importlib.util.spec_from_file_location(
    "agent_lifecycle_acceptance", SCRIPTS / "agent_lifecycle_acceptance.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_ACCEPTANCE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _ACCEPTANCE
_SPEC.loader.exec_module(_ACCEPTANCE)
StepResult = _ACCEPTANCE.StepResult
build_acceptance_report = _ACCEPTANCE.build_acceptance_report
SCHEMA = "pilot107.agent-lifecycle-acceptance/v1"
REQUIRED_D1 = {
    "blank_project_gold_path",
    "failed_run_code_repair",
    "long_pending_turn_release",
    "runtime_log_replay_and_terminal_drain",
    "resource_missingness_and_summary",
    "publish_conflict",
    "worker_agentd_browser_restart",
    "two_owner_isolation",
    "market_application_and_publication",
    "model_unavailable_deterministic_fallback",
    "large_file_metadata_only",
    "artifact_aware_array_recovery",
}


def _run(
    script: str,
    *arguments: str,
    artifact_dir: Path,
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "PILOT107_AGENT_LIFECYCLE_ARTIFACT_DIR": str(artifact_dir),
    }
    return subprocess.run(
        ["bash", str(SCRIPTS / script), *arguments],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _manifest(script: str, *, artifact_dir: Path) -> dict[str, Any]:
    completed = _run(script, "--manifest", artifact_dir=artifact_dir)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    written = artifact_dir / f"{payload['profile']}-manifest.json"
    assert json.loads(written.read_text(encoding="utf-8")) == payload
    return payload


@pytest.mark.parametrize(
    ("profile", "environment"),
    [
        ("source", "D0"),
        ("runtime", "D1"),
        ("s1", "S1"),
        ("r1", "R1"),
    ],
)
def test_entrypoint_emits_revision_bound_manifest(
    tmp_path: Path,
    profile: str,
    environment: str,
) -> None:
    """Catch a manifest that is absent, mislabeled, or detached from HEAD."""

    payload = _manifest(
        f"accept-agent-lifecycle-{profile}.sh",
        artifact_dir=tmp_path / profile,
    )
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()

    assert payload["schema"] == SCHEMA
    assert payload["profile"] == profile
    assert payload["environment"] == environment
    assert payload["release_revision"] == revision
    assert payload["status"] == "not_run"
    assert payload["mode"] == "manifest"
    assert payload["cases"]


def test_source_manifest_covers_every_source_gate(tmp_path: Path) -> None:
    """Catch a source candidate that can skip a required engineering gate."""

    payload = _manifest(
        "accept-agent-lifecycle-source.sh",
        artifact_dir=tmp_path,
    )

    assert {
        "python_lint",
        "python_types",
        "python_unit",
        "schema_contracts",
        "agentd_type_unit_build",
        "web_type_unit_build",
        "web_browser_workflow",
        "compose_config",
        "sim_core",
    } <= set(payload["cases"])


def test_source_sim_core_is_self_contained_and_cleans_up() -> None:
    """Catch a source gate that silently depends on an ambient running stack."""

    steps = _ACCEPTANCE._source_steps()
    names = [step.name for step in steps]

    start = names.index("sim_core_start")
    check = names.index("sim_core_check")
    cleanup = names.index("sim_core_cleanup")
    assert start < check < cleanup
    assert steps[start].command == ("bash", "scripts/start-sim-core.sh")
    assert steps[check].command == ("bash", "scripts/check-sim-core.sh")
    assert steps[cleanup].command[-2:] == ("--volumes", "--remove-orphans")


def test_runtime_manifest_covers_cases_and_capacity_budgets(tmp_path: Path) -> None:
    """Catch a D1 pack that omits a lifecycle or bounded-capacity scenario."""

    payload = _manifest(
        "accept-agent-lifecycle-runtime.sh",
        artifact_dir=tmp_path,
    )

    assert set(payload["cases"]) >= REQUIRED_D1
    assert payload["capacity"] == {
        "idle_sessions": 100,
        "concurrent_turns": 10,
        "active_watches": 100,
        "max_commands_per_turn": 32,
        "max_bytes_per_turn": 1_048_576,
        "max_bytes_per_tool_result": 65_536,
    }
    assert payload["stack_policy"] == {
        "backend": "docker-slurm-25.11.2-simulator",
        "clean_before": True,
        "clean_after": True,
    }


def test_runtime_bootstraps_accounting_profile_before_shared_stack_cases() -> None:
    """Catch a clean D1 volume that crash-loops before accounts and QoS exist."""

    steps = _ACCEPTANCE._runtime_steps()
    names = [step.name for step in steps]

    assert "bootstrap_sim_core" in names
    bootstrap = names.index("bootstrap_sim_core")
    assert bootstrap < names.index("long_pending_turn_release")
    assert steps[bootstrap].command == ("bash", "scripts/start-sim-core.sh")


def test_s1_manifest_requires_target_vm_resource_and_restart_evidence(
    tmp_path: Path,
) -> None:
    """Catch an S1 gate that could pass without the specified VM envelope."""

    payload = _manifest(
        "accept-agent-lifecycle-s1.sh",
        artifact_dir=tmp_path,
    )

    assert payload["target_resources"] == {"cpu_cores": 8, "memory_gib": 16}
    assert payload["execution_contract"] == {
        "confirmation_env": "PILOT107_S1_CONFIRMED=1",
        "bundle_env": "PILOT107_S1_BUNDLE_DIR",
        "public_url_env": "PILOT107_PUBLIC_URL",
    }
    assert {
        "deployment",
        "resource_ceilings",
        "restart_recovery",
    } <= set(payload["cases"])


def test_r1_manifest_requires_explicit_authorization_and_real_cluster_cases(
    tmp_path: Path,
) -> None:
    """Catch an R1 gate that infers approval or omits a production failure mode."""

    payload = _manifest(
        "accept-agent-lifecycle-r1.sh",
        artifact_dir=tmp_path,
    )

    assert payload["authorization"] == {
        "required_flags": [
            "--target",
            "--owner",
            "--approved-root",
            "--authorization-id",
            "--confirm-real-107",
        ],
        "approval_inferred": False,
        "simulator_endpoints_allowed": False,
    }
    assert {
        "success",
        "exit_42",
        "cancel",
        "auth_expired",
        "evidence",
        "runtime_watch",
        "resource_availability",
        "model_unavailable_deterministic_fallback",
    } <= set(payload["cases"])


def test_r1_without_authorization_records_not_run(tmp_path: Path) -> None:
    """Catch a real-cluster entrypoint that silently assumes authorization."""

    completed = _run(
        "accept-agent-lifecycle-r1.sh",
        artifact_dir=tmp_path,
    )

    assert completed.returncode == 77
    report = json.loads((tmp_path / "r1-report.json").read_text(encoding="utf-8"))
    assert report["environment"] == "R1"
    assert report["status"] == "not_run"
    assert report["reason"] == "authorization_missing"
    assert report["authorization"]["approval_inferred"] is False


def test_s1_without_target_infrastructure_records_not_run(tmp_path: Path) -> None:
    """Catch a VM gate that labels missing target infrastructure as evidence."""

    completed = _run(
        "accept-agent-lifecycle-s1.sh",
        artifact_dir=tmp_path,
    )

    assert completed.returncode == 77
    report = json.loads((tmp_path / "s1-report.json").read_text(encoding="utf-8"))
    assert report["environment"] == "S1"
    assert report["status"] == "not_run"
    assert report["reason"] == "infrastructure_missing"
    assert report["target_resources"] == {"cpu_cores": 8, "memory_gib": 16}


def test_r1_refuses_simulator_target_before_network_access(tmp_path: Path) -> None:
    """Catch accidental relabeling of D1 simulator evidence as R1 evidence."""

    completed = _run(
        "accept-agent-lifecycle-r1.sh",
        "--target",
        "pilot107-sim-login-node-sim-1",
        "--owner",
        "alice",
        "--approved-root",
        "/public/home/alice/pilot107-r1-acceptance",
        "--authorization-id",
        "approval-2026-08-25",
        "--confirm-real-107",
        artifact_dir=tmp_path,
    )

    assert completed.returncode == 2
    assert "simulator" in completed.stderr.lower()
    report = json.loads((tmp_path / "r1-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "refused"
    assert report["reason"] == "simulator_target"


def test_r1_real_target_without_active_control_socket_is_not_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch an R1 gate that opens a new login instead of requiring delegated auth."""

    monkeypatch.delenv("PILOT107_R1_CONTROL_PATH", raising=False)
    completed = _run(
        "accept-agent-lifecycle-r1.sh",
        "--target",
        "real107.example.edu",
        "--owner",
        "alice",
        "--approved-root",
        "/public/home/alice/pilot107-smoke-acceptance",
        "--authorization-id",
        "approval-2026-08-25",
        "--confirm-real-107",
        artifact_dir=tmp_path,
    )

    assert completed.returncode == 77
    report = json.loads((tmp_path / "r1-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "not_run"
    assert report["reason"] == "infrastructure_missing"


def test_r1_refuses_option_like_target_before_network_access(tmp_path: Path) -> None:
    """Catch an SSH target that could be interpreted as a client option."""

    completed = _run(
        "accept-agent-lifecycle-r1.sh",
        "--target=-oProxyCommand=unsafe",
        "--owner",
        "alice",
        "--approved-root",
        "/public/home/alice/pilot107-smoke-acceptance",
        "--authorization-id",
        "approval-2026-08-25",
        "--confirm-real-107",
        artifact_dir=tmp_path,
    )

    assert completed.returncode == 2
    report = json.loads((tmp_path / "r1-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "refused"
    assert report["reason"] == "target_invalid"


def _passed_step(name: str, *cases: str) -> StepResult:
    return StepResult(
        name=name,
        cases=cases,
        command=("true",),
        status="PASS",
        returncode=0,
        started_at="2026-08-25T00:00:00+00:00",
        ended_at="2026-08-25T00:00:01+00:00",
        log_path=f"steps/{name}.log",
        log_sha256="a" * 64,
    )


def test_report_fails_closed_when_a_required_case_has_no_step() -> None:
    """Catch an aggregate PASS that silently omits a manifest case."""

    report = build_acceptance_report(
        profile="source",
        revision="b" * 40,
        started_at="2026-08-25T00:00:00+00:00",
        ended_at="2026-08-25T00:00:02+00:00",
        step_results=[_passed_step("lint", "python_lint")],
    )

    assert report["status"] == "FAIL"
    assert report["process_exit_code"] == 1
    assert report["case_results"]["python_lint"] == "PASS"
    assert report["case_results"]["python_types"] == "MISSING"


def test_report_fails_when_any_supporting_step_fails() -> None:
    """Catch a case reported PASS when one of its required commands failed."""

    steps = [_passed_step(f"source-{case}", case) for case in SOURCE_CASES_FOR_TEST]
    failed = steps[-1]
    steps[-1] = StepResult(
        name=failed.name,
        cases=failed.cases,
        command=failed.command,
        status="FAIL",
        returncode=9,
        started_at=failed.started_at,
        ended_at=failed.ended_at,
        log_path=failed.log_path,
        log_sha256=failed.log_sha256,
    )

    report = build_acceptance_report(
        profile="source",
        revision="c" * 40,
        started_at="2026-08-25T00:00:00+00:00",
        ended_at="2026-08-25T00:00:02+00:00",
        step_results=steps,
    )

    assert report["status"] == "FAIL"
    assert report["case_results"][failed.cases[0]] == "FAIL"
    assert report["process_exit_code"] == 1


SOURCE_CASES_FOR_TEST = (
    "python_lint",
    "python_types",
    "python_unit",
    "schema_contracts",
    "agentd_type_unit_build",
    "web_type_unit_build",
    "web_browser_workflow",
    "compose_config",
    "sim_core",
)
