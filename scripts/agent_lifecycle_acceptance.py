#!/usr/bin/env python3
"""Shared revision-bound manifests and safety gates for lifecycle acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCHEMA = "pilot107.agent-lifecycle-acceptance/v1"
ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENTS = {"source": "D0", "runtime": "D1", "s1": "S1", "r1": "R1"}
SOURCE_CASES = (
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
RUNTIME_CASES = (
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
    "capacity_idle_sessions",
    "capacity_concurrent_turns",
    "capacity_active_watches",
    "connection_command_and_byte_budgets",
)
S1_CASES = ("deployment", "resource_ceilings", "restart_recovery")
R1_CASES = (
    "success",
    "exit_42",
    "cancel",
    "auth_expired",
    "evidence",
    "runtime_watch",
    "resource_availability",
    "model_unavailable_deterministic_fallback",
)
R1_REQUIRED_FLAGS = (
    "--target",
    "--owner",
    "--approved-root",
    "--authorization-id",
    "--confirm-real-107",
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}$")


@dataclass(frozen=True)
class StepResult:
    name: str
    cases: tuple[str, ...]
    command: tuple[str, ...]
    status: str
    returncode: int
    started_at: str
    ended_at: str
    log_path: str
    log_sha256: str

    def payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "cases": list(self.cases),
            "command": list(self.command),
            "status": self.status,
            "returncode": self.returncode,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "log_path": self.log_path,
            "log_sha256": self.log_sha256,
        }


@dataclass(frozen=True)
class StepSpec:
    name: str
    cases: tuple[str, ...]
    command: tuple[str, ...]


def _revision() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def _artifact_dir() -> Path:
    configured = os.environ.get("PILOT107_AGENT_LIFECYCLE_ARTIFACT_DIR")
    if configured:
        destination = Path(configured)
    else:
        destination = ROOT / "artifacts" / "acceptance" / "agent-lifecycle" / _revision()
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def _timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _tracked_worktree_clean() -> bool:
    unstaged = subprocess.run(["git", "diff", "--quiet"], cwd=ROOT, check=False).returncode
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=ROOT, check=False
    ).returncode
    return unstaged == 0 and staged == 0


def _untracked_status() -> str:
    return subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files"],
        cwd=ROOT,
        text=True,
    )


def _authorization_contract() -> dict[str, object]:
    return {
        "required_flags": list(R1_REQUIRED_FLAGS),
        "approval_inferred": False,
        "simulator_endpoints_allowed": False,
    }


def manifest(profile: str) -> dict[str, Any]:
    environment = ENVIRONMENTS[profile]
    cases = {
        "source": SOURCE_CASES,
        "runtime": RUNTIME_CASES,
        "s1": S1_CASES,
        "r1": R1_CASES,
    }[profile]
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "mode": "manifest",
        "profile": profile,
        "environment": environment,
        "release_revision": _revision(),
        "status": "not_run",
        "generated_at": _timestamp(),
        "cases": list(cases),
    }
    if profile == "runtime":
        payload["capacity"] = {
            "idle_sessions": 100,
            "concurrent_turns": 10,
            "active_watches": 100,
            "max_commands_per_turn": 32,
            "max_bytes_per_turn": 1_048_576,
            "max_bytes_per_tool_result": 65_536,
        }
        payload["stack_policy"] = {
            "backend": "docker-slurm-25.11.2-simulator",
            "clean_before": True,
            "clean_after": True,
        }
    elif profile == "s1":
        payload["target_resources"] = {"cpu_cores": 8, "memory_gib": 16}
        payload["execution_contract"] = {
            "confirmation_env": "PILOT107_S1_CONFIRMED=1",
            "bundle_env": "PILOT107_S1_BUNDLE_DIR",
            "public_url_env": "PILOT107_PUBLIC_URL",
        }
    elif profile == "r1":
        payload["authorization"] = _authorization_contract()
    return payload


def build_acceptance_report(
    *,
    profile: str,
    revision: str,
    started_at: str,
    ended_at: str,
    step_results: list[StepResult],
    detail: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Aggregate required cases fail-closed from objective step results."""

    payload = manifest(profile)
    case_results: dict[str, str] = {}
    for case in payload["cases"]:
        supporting = [step for step in step_results if case in step.cases]
        if not supporting:
            case_results[case] = "MISSING"
        elif any(step.status != "PASS" for step in supporting):
            case_results[case] = "FAIL"
        else:
            case_results[case] = "PASS"
    passed = (
        bool(step_results)
        and all(status == "PASS" for status in case_results.values())
        and all(step.status == "PASS" for step in step_results)
    )
    payload.update(
        {
            "mode": "acceptance",
            "release_revision": revision,
            "started_at": started_at,
            "ended_at": ended_at,
            "status": "PASS" if passed else "FAIL",
            "process_exit_code": 0 if passed else 1,
            "case_results": case_results,
            "steps": [step.payload() for step in step_results],
        }
    )
    if detail:
        payload.update(detail)
    return payload


def _write(payload: dict[str, Any], filename: str) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    destination = _artifact_dir() / filename
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(destination)


def emit_manifest(profile: str) -> int:
    payload = manifest(profile)
    _write(payload, f"{profile}-manifest.json")
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def emit_terminal_report(
    profile: str,
    *,
    status: str,
    reason: str,
    detail: dict[str, object] | None = None,
) -> dict[str, Any]:
    payload = manifest(profile)
    payload.update(
        {
            "mode": "acceptance",
            "status": status,
            "reason": reason,
            "ended_at": _timestamp(),
        }
    )
    if detail:
        payload.update(detail)
    _write(payload, f"{profile}-report.json")
    return payload


def _run_step(
    spec: StepSpec,
    *,
    steps_dir: Path,
    environment: dict[str, str],
) -> StepResult:
    started_at = _timestamp()
    log_path = steps_dir / f"{spec.name}.log"
    print(f"=== {spec.name} ===", flush=True)
    try:
        with log_path.open("wb") as log:
            completed = subprocess.run(
                list(spec.command),
                cwd=ROOT,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
        returncode = completed.returncode
    except OSError as exc:
        log_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
        returncode = 127
    ended_at = _timestamp()
    status = "PASS" if returncode == 0 else "FAIL"
    print(f"=== {spec.name} {status} (rc={returncode}) ===", flush=True)
    return StepResult(
        name=spec.name,
        cases=spec.cases,
        command=spec.command,
        status=status,
        returncode=returncode,
        started_at=started_at,
        ended_at=ended_at,
        log_path=f"{steps_dir.name}/{spec.name}.log",
        log_sha256=hashlib.sha256(log_path.read_bytes()).hexdigest(),
    )


def _source_steps() -> tuple[StepSpec, ...]:
    compose = (
        "docker",
        "compose",
        "--env-file",
        "simulator/compose/.env.example",
        "-f",
        "simulator/compose/compose.yml",
    )
    return (
        StepSpec(
            "python_lint",
            ("python_lint",),
            ("uv", "run", "ruff", "check", "src", "tests", "scripts"),
        ),
        StepSpec("python_types", ("python_types",), ("uv", "run", "mypy", "src")),
        StepSpec(
            "schema_contracts",
            ("schema_contracts",),
            (
                "uv",
                "run",
                "pytest",
                "tests/agent/test_lifecycle_schemas.py",
                "tests/test_schema_migrations.py",
                "tests/test_contract_v2.py",
                "tests/test_contracts.py",
                "-q",
            ),
        ),
        StepSpec("python_unit", ("python_unit",), ("uv", "run", "pytest", "-q")),
        StepSpec(
            "agentd_typecheck",
            ("agentd_type_unit_build",),
            ("npm", "--prefix", "services/pilot-agentd", "run", "typecheck"),
        ),
        StepSpec(
            "agentd_unit",
            ("agentd_type_unit_build",),
            ("npm", "--prefix", "services/pilot-agentd", "test", "--", "--run"),
        ),
        StepSpec(
            "agentd_build",
            ("agentd_type_unit_build",),
            ("npm", "--prefix", "services/pilot-agentd", "run", "build"),
        ),
        StepSpec("web_typecheck", ("web_type_unit_build",), ("npm", "run", "typecheck")),
        StepSpec("web_unit", ("web_type_unit_build",), ("npm", "test", "--", "--run")),
        StepSpec("web_build", ("web_type_unit_build",), ("npm", "run", "build")),
        StepSpec(
            "web_static_drift",
            ("web_type_unit_build",),
            ("git", "diff", "--exit-code", "--", "src/pilot107/web/static"),
        ),
        StepSpec("web_browser", ("web_browser_workflow",), ("npm", "run", "test:ui")),
        StepSpec(
            "compose_config",
            ("compose_config",),
            ("sh", "simulator/compose/scripts/check-compose-config.sh"),
        ),
        StepSpec(
            "sim_core_start",
            ("sim_core",),
            ("bash", "scripts/start-sim-core.sh"),
        ),
        StepSpec(
            "sim_core_check",
            ("sim_core",),
            ("bash", "scripts/check-sim-core.sh"),
        ),
        StepSpec(
            "sim_core_cleanup",
            (),
            (*compose, "down", "--volumes", "--remove-orphans"),
        ),
    )


def _runtime_steps() -> tuple[StepSpec, ...]:
    compose = (
        "docker",
        "compose",
        "--env-file",
        "simulator/compose/.env.example",
        "-f",
        "simulator/compose/compose.yml",
        "--profile",
        "apps",
    )
    clean = (*compose, "down", "--volumes", "--remove-orphans")
    return (
        StepSpec("clean_stack_before", (), clean),
        StepSpec("build_app_images", (), ("bash", "scripts/build-app-images.sh")),
        StepSpec("check_app_images", (), ("bash", "scripts/check-app-images.sh")),
        StepSpec("bootstrap_sim_core", (), ("bash", "scripts/start-sim-core.sh")),
        StepSpec(
            "agent_restart_owner_capacity",
            (
                "worker_agentd_browser_restart",
                "two_owner_isolation",
                "capacity_idle_sessions",
                "capacity_concurrent_turns",
                "connection_command_and_byte_budgets",
            ),
            ("bash", "scripts/fault-pilot-agent-a1.sh"),
        ),
        StepSpec(
            "long_pending_turn_release",
            ("long_pending_turn_release",),
            ("bash", "scripts/smoke-pilot-agent-a3-live.sh"),
        ),
        StepSpec(
            "formal_run_publication_watch",
            (
                "blank_project_gold_path",
                "publish_conflict",
                "runtime_log_replay_and_terminal_drain",
            ),
            ("bash", "scripts/smoke-pilot-agent-a4-live.sh"),
        ),
        StepSpec(
            "resource_observation",
            ("resource_missingness_and_summary",),
            ("bash", "scripts/smoke-observability-live.sh"),
        ),
        StepSpec(
            "failed_run_repair",
            ("failed_run_code_repair",),
            ("bash", "scripts/smoke-pilot-agent-repair-live.sh"),
        ),
        StepSpec(
            "market_lifecycle",
            ("market_application_and_publication",),
            ("bash", "scripts/smoke-pilot-agent-market-live.sh"),
        ),
        StepSpec(
            "artifact_array_recovery",
            ("artifact_aware_array_recovery",),
            ("bash", "scripts/smoke-experiment-pipeline-live.sh"),
        ),
        StepSpec(
            "runtime_watch_capacity",
            ("capacity_active_watches", "runtime_log_replay_and_terminal_drain"),
            ("bash", "scripts/smoke-runtime-watch-live.sh"),
        ),
        StepSpec(
            "model_and_large_file_boundaries",
            (
                "model_unavailable_deterministic_fallback",
                "large_file_metadata_only",
            ),
            ("bash", "scripts/smoke-agent-lifecycle-boundaries-live.sh"),
        ),
        StepSpec("clean_stack_after", (), clean),
    )


def _image_bindings(environment: dict[str, str]) -> list[dict[str, str]]:
    bindings: list[dict[str, str]] = []
    for variable in (
        "PILOT107_API_IMAGE",
        "PILOT107_WORKER_IMAGE",
        "PILOT107_WEB_IMAGE",
        "PILOT107_AGENTD_IMAGE",
    ):
        reference = environment[variable]
        inspected = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        bindings.append(
            {
                "variable": variable,
                "reference": reference,
                "image_id": inspected.stdout.strip() if inspected.returncode == 0 else "",
            }
        )
    return bindings


def run_local_profile(profile: str) -> int:
    if profile not in {"source", "runtime"}:
        raise ValueError("local profile must be source or runtime")
    revision = _revision()
    started_at = _timestamp()
    if not _tracked_worktree_clean():
        report = emit_terminal_report(
            profile,
            status="FAIL",
            reason="tracked_worktree_dirty",
            detail={"process_exit_code": 1},
        )
        print(
            f"{profile} acceptance requires a clean tracked worktree: {report['release_revision']}",
            file=sys.stderr,
        )
        return 1
    artifact_dir = _artifact_dir()
    steps_dir = artifact_dir / f"{profile}-steps"
    steps_dir.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    short_revision = revision[:12]
    if profile == "runtime":
        environment.update(
            {
                "PILOT107_API_IMAGE": f"pilot107/api:lifecycle-{short_revision}",
                "PILOT107_WORKER_IMAGE": f"pilot107/worker:lifecycle-{short_revision}",
                "PILOT107_WEB_IMAGE": f"pilot107/web:lifecycle-{short_revision}",
                "PILOT107_AGENTD_IMAGE": f"pilot107/agentd:lifecycle-{short_revision}",
            }
        )
    specs = _source_steps() if profile == "source" else _runtime_steps()
    results = [_run_step(spec, steps_dir=steps_dir, environment=environment) for spec in specs]
    detail: dict[str, object] = {
        "tracked_worktree_clean": True,
        "untracked_files_status": _untracked_status(),
    }
    if profile == "runtime":
        detail["images"] = _image_bindings(environment)
    report = build_acceptance_report(
        profile=profile,
        revision=revision,
        started_at=started_at,
        ended_at=_timestamp(),
        step_results=results,
        detail=detail,
    )
    _write(report, f"{profile}-report.json")
    print(
        f"{profile} acceptance {report['status']} "
        f"revision={revision} report={artifact_dir / f'{profile}-report.json'}"
    )
    return int(report["process_exit_code"])


def verify_s1_host(bundle_dir: Path, revision: str) -> int:
    """Fail closed unless the current host and bundle match the S1 envelope."""

    manifest_path = bundle_dir / "RELEASE_MANIFEST.json"
    try:
        release = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"S1 bundle manifest is unreadable: {exc}", file=sys.stderr)
        return 1
    if release.get("release_revision") != revision:
        print("S1 bundle revision does not match the acceptance revision", file=sys.stderr)
        return 1
    cpu_count = os.cpu_count() or 0
    try:
        mem_total_kib = int(
            next(
                line.split()[1]
                for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
                if line.startswith("MemTotal:")
            )
        )
    except (OSError, StopIteration, ValueError):
        print("S1 host memory could not be measured", file=sys.stderr)
        return 1
    minimum_memory_kib = 14 * 1024 * 1024
    if cpu_count < 8 or mem_total_kib < minimum_memory_kib:
        print(
            f"S1 host is below the 8C/16G envelope: cpu={cpu_count} memory_kib={mem_total_kib}",
            file=sys.stderr,
        )
        return 1
    compose_dir = bundle_dir / "simulator" / "compose"
    cpu_rc = (compose_dir / "compose.cpu-rc.yml").read_text(encoding="utf-8")
    if "cpus: 8.0" not in cpu_rc or "mem_limit: 15g" not in cpu_rc:
        print("S1 bundle does not carry the expected worker resource ceilings", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "cpu_count": cpu_count,
                "memory_kib": mem_total_kib,
                "release_revision": revision,
                "worker_cpu_ceiling": 8,
                "worker_memory_ceiling": "15g",
            },
            sort_keys=True,
        )
    )
    return 0


def run_s1() -> int:
    revision = _revision()
    bundle_value = os.environ.get("PILOT107_S1_BUNDLE_DIR", "")
    public_url = os.environ.get("PILOT107_PUBLIC_URL", "")
    confirmed = os.environ.get("PILOT107_S1_CONFIRMED") == "1"
    bundle_dir = Path(bundle_value) if bundle_value else None
    if not confirmed or not public_url or bundle_dir is None or not bundle_dir.is_dir():
        emit_terminal_report(
            "s1",
            status="not_run",
            reason="infrastructure_missing",
            detail={
                "requirements_present": {
                    "confirmed_s1_host": confirmed,
                    "bundle_dir": bool(bundle_dir and bundle_dir.is_dir()),
                    "public_url": bool(public_url),
                }
            },
        )
        print("S1 not run: confirmed host, bundle, and public URL are required", file=sys.stderr)
        return 77
    if not _tracked_worktree_clean():
        emit_terminal_report(
            "s1",
            status="FAIL",
            reason="tracked_worktree_dirty",
            detail={"process_exit_code": 1},
        )
        return 1
    artifact_dir = _artifact_dir()
    steps_dir = artifact_dir / "s1-steps"
    steps_dir.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.update(
        {
            "PILOT107_BUNDLE_DIR": str(bundle_dir),
            "PILOT107_ACCEPT_ARTIFACT_DIR": str(artifact_dir / "s1-runtime"),
            "PILOT107_ACCEPT_SEAL_MODE": "1",
        }
    )
    specs = (
        StepSpec(
            "host_and_resource_ceilings",
            ("resource_ceilings",),
            (
                sys.executable,
                "scripts/agent_lifecycle_acceptance.py",
                "verify-s1-host",
                str(bundle_dir),
                revision,
            ),
        ),
        StepSpec(
            "deployment_and_restart",
            ("deployment", "restart_recovery"),
            ("bash", "scripts/accept-runtime-bundle.sh"),
        ),
    )
    started_at = _timestamp()
    results = [_run_step(spec, steps_dir=steps_dir, environment=environment) for spec in specs]
    report = build_acceptance_report(
        profile="s1",
        revision=revision,
        started_at=started_at,
        ended_at=_timestamp(),
        step_results=results,
        detail={
            "tracked_worktree_clean": True,
            "bundle_dir": str(bundle_dir),
            "public_url": public_url,
        },
    )
    _write(report, "s1-report.json")
    print(f"S1 acceptance {report['status']} revision={revision}")
    return int(report["process_exit_code"])


def _is_simulator_target(target: str) -> bool:
    candidate = target.strip().lower()
    parsed = urlsplit(candidate if "://" in candidate else f"ssh://{candidate}")
    host = (parsed.hostname or candidate.rsplit("@", 1)[-1]).strip("[]")
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        return True
    simulator_tokens = ("pilot107-sim", "simulator", "docker", "login-node-sim")
    return host.endswith(".local") or any(token in host for token in simulator_tokens)


def _r1_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--target")
    parser.add_argument("--owner")
    parser.add_argument("--approved-root")
    parser.add_argument("--authorization-id")
    parser.add_argument("--confirm-real-107", action="store_true")
    parser.add_argument("--help", action="store_true")
    return parser


def run_r1(arguments: list[str]) -> int:
    parser = _r1_parser()
    try:
        values, extras = parser.parse_known_args(arguments)
    except SystemExit:
        return 2
    if values.help:
        print("R1 requires: " + " ".join(R1_REQUIRED_FLAGS))
        return 0
    missing = [
        flag
        for flag, present in (
            ("--target", values.target),
            ("--owner", values.owner),
            ("--approved-root", values.approved_root),
            ("--authorization-id", values.authorization_id),
            ("--confirm-real-107", values.confirm_real_107),
        )
        if not present
    ]
    if missing or extras:
        emit_terminal_report(
            "r1",
            status="not_run",
            reason="authorization_missing",
            detail={
                "authorization": _authorization_contract(),
                "missing_flags": missing,
                "unexpected_arguments": extras,
            },
        )
        print(
            "R1 not run: explicit authorization is incomplete ("
            + ", ".join((*missing, *extras))
            + ")",
            file=sys.stderr,
        )
        return 77
    assert values.target is not None
    if _is_simulator_target(values.target):
        emit_terminal_report(
            "r1",
            status="refused",
            reason="simulator_target",
            detail={"authorization": _authorization_contract()},
        )
        print("R1 refuses simulator endpoints before any network access", file=sys.stderr)
        return 2
    if not _SAFE_ID.fullmatch(values.target):
        emit_terminal_report("r1", status="refused", reason="target_invalid")
        print("R1 target is not a safe SSH alias", file=sys.stderr)
        return 2
    assert values.owner is not None and values.authorization_id is not None
    if not _SAFE_ID.fullmatch(values.owner) or not _SAFE_ID.fullmatch(values.authorization_id):
        emit_terminal_report("r1", status="refused", reason="invalid_authorization")
        print("R1 owner or authorization id is invalid", file=sys.stderr)
        return 2
    assert values.approved_root is not None
    if not re.fullmatch(
        rf"^/(?:public/home/{re.escape(values.owner)}|"
        rf"home/[A-Za-z0-9._-]+/{re.escape(values.owner)})"
        r"/pilot107-smoke-[A-Za-z0-9._-]+$",
        values.approved_root,
    ):
        emit_terminal_report("r1", status="refused", reason="approved_root_invalid")
        print(
            "R1 approved root must be a fresh owner-scoped pilot107-smoke-* path",
            file=sys.stderr,
        )
        return 2
    control_path_value = os.environ.get("PILOT107_R1_CONTROL_PATH", "")
    control_path = Path(control_path_value) if control_path_value else None
    if control_path is None or not control_path.is_absolute() or not control_path.is_socket():
        emit_terminal_report(
            "r1",
            status="not_run",
            reason="infrastructure_missing",
            detail={"authorization": _authorization_contract()},
        )
        print(
            "R1 not run: an active absolute PILOT107_R1_CONTROL_PATH is required",
            file=sys.stderr,
        )
        return 77
    active_control = subprocess.run(
        [
            "ssh",
            "-S",
            str(control_path),
            "-o",
            "BatchMode=yes",
            "-O",
            "check",
            values.target,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if active_control.returncode != 0:
        emit_terminal_report(
            "r1",
            status="not_run",
            reason="authorization_expired",
            detail={"authorization": _authorization_contract()},
        )
        print("R1 not run: the delegated ControlMaster is not active", file=sys.stderr)
        return 77
    if not _tracked_worktree_clean():
        emit_terminal_report(
            "r1",
            status="FAIL",
            reason="tracked_worktree_dirty",
            detail={"process_exit_code": 1, "authorization": _authorization_contract()},
        )
        return 1
    revision = _revision()
    artifact_dir = _artifact_dir()
    steps_dir = artifact_dir / "r1-steps"
    steps_dir.mkdir(parents=True, exist_ok=True)
    jobs_dir = artifact_dir / "r1-jobs"
    environment = dict(os.environ)
    environment.update(
        {
            "PILOT107_REAL107_SSH_TARGET": values.target,
            "PILOT107_REAL107_SSH_CONTROL_PATH": str(control_path),
            "PILOT107_REAL107_WORKDIR": values.approved_root,
            "PILOT107_REAL107_JOB_OUTPUT_DIR": str(jobs_dir),
        }
    )
    specs = (
        StepSpec(
            "fixed_real_jobs",
            ("success", "exit_42", "cancel"),
            ("bash", "scripts/smoke-real107-ssh-jobs.sh"),
        ),
        StepSpec(
            "typed_real107_lifecycle",
            (
                "auth_expired",
                "evidence",
                "runtime_watch",
                "resource_availability",
                "model_unavailable_deterministic_fallback",
            ),
            (
                "uv",
                "run",
                "python",
                "scripts/smoke_agent_lifecycle_r1.py",
                "--target",
                values.target,
                "--owner",
                values.owner,
                "--approved-root",
                values.approved_root,
                "--control-path",
                str(control_path),
                "--job-summary",
                str(jobs_dir / "summary.txt"),
            ),
        ),
    )
    started_at = _timestamp()
    results = [_run_step(spec, steps_dir=steps_dir, environment=environment) for spec in specs]
    report = build_acceptance_report(
        profile="r1",
        revision=revision,
        started_at=started_at,
        ended_at=_timestamp(),
        step_results=results,
        detail={
            "authorization": {
                **_authorization_contract(),
                "target": values.target,
                "owner": values.owner,
                "approved_root": values.approved_root,
                "authorization_id": values.authorization_id,
                "confirmed_real_107": True,
            },
            "tracked_worktree_clean": True,
        },
    )
    _write(report, "r1-report.json")
    print(f"R1 acceptance {report['status']} revision={revision}")
    return int(report["process_exit_code"])


def main(arguments: list[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if arguments is None else arguments)
    if raw_arguments[:1] == ["r1"]:
        return run_r1(raw_arguments[1:])
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    manifest_parser = subparsers.add_parser("manifest")
    manifest_parser.add_argument("profile", choices=tuple(ENVIRONMENTS))
    not_run_parser = subparsers.add_parser("not-run")
    not_run_parser.add_argument("profile", choices=tuple(ENVIRONMENTS))
    not_run_parser.add_argument("reason")
    subparsers.add_parser("source")
    subparsers.add_parser("runtime")
    subparsers.add_parser("s1")
    verify_s1_parser = subparsers.add_parser("verify-s1-host")
    verify_s1_parser.add_argument("bundle_dir", type=Path)
    verify_s1_parser.add_argument("revision")
    r1_parser = subparsers.add_parser("r1")
    r1_parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(raw_arguments)
    if args.command == "manifest":
        return emit_manifest(args.profile)
    if args.command == "not-run":
        emit_terminal_report(args.profile, status="not_run", reason=args.reason)
        return 77
    if args.command in {"source", "runtime"}:
        return run_local_profile(args.command)
    if args.command == "s1":
        return run_s1()
    if args.command == "verify-s1-host":
        return verify_s1_host(args.bundle_dir, args.revision)
    if args.command == "r1":
        return run_r1(args.arguments)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
