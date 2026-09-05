#!/usr/bin/env python3
"""Generate a machine-readable Docker simulator behavior fidelity report."""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "config/platform_profiles/simulator-real107-behavior.yaml"
MANIFEST_PATH = ROOT / "simulator/images/slurm/version-manifest.json"
TARGET_MANIFEST_PATH = ROOT / "simulator/images/slurm/version-manifest.25.11.json"


@dataclass(frozen=True)
class CommandResult:
    name: str
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "argv": list(self.argv),
            "returncode": self.returncode,
            "stdout": _trim(self.stdout),
            "stderr": _trim(self.stderr),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "simulator/reports/behavior-fidelity"),
        help="Directory for timestamped report JSON.",
    )
    parser.add_argument(
        "--skip-smoke",
        action="store_true",
        help="Collect static simulator facts only; do not submit behavior smoke jobs.",
    )
    args = parser.parse_args(argv)

    profile = load_simple_yaml(PROFILE_PATH)
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    generated_at = utc_now()

    observations = collect_observations(run_smoke=not args.skip_smoke)
    report = build_report(
        profile=profile,
        manifest=manifest,
        generated_at=generated_at,
        observations=observations,
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{generated_at.replace(':', '').replace('+', 'Z')}.json"
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"sim behavior fidelity report {report['summary']['status']}")
    print(f"artifact={output_path}")
    return 0 if report["summary"]["status"] in {"pass", "limited"} else 1


def collect_observations(*, run_smoke: bool) -> dict[str, Any]:
    observations: dict[str, Any] = {"commands": [], "behavior_checks": []}

    apply_result = run_command(
        "apply_profile",
        ("bash", str(ROOT / "scripts/apply-sim-real107-profile.sh")),
        timeout_seconds=120,
    )
    observations["commands"].append(apply_result)

    for name, command in (
        ("scontrol_version", compose_exec_args("login-node-sim", "scontrol", "--version")),
        (
            "scontrol_show_part",
            compose_exec_args("login-node-sim", "scontrol", "show", "part"),
        ),
        (
            "sinfo_summary",
            compose_exec_args("login-node-sim", "sinfo", "-h", "-o", "%P|%N|%T|%G"),
        ),
        (
            "qos_table",
            compose_exec_args(
                "login-node-sim",
                "sacctmgr",
                "-nP",
                "show",
                "qos",
                "format=Name,MaxWall,MaxTRESPerJob,GrpTRES",
            ),
        ),
        (
            "assoc_table",
            compose_exec_args(
                "login-node-sim",
                "sacctmgr",
                "-nP",
                "show",
                "assoc",
                "where",
                "user=alice,bob",
                "format=User,Account,QOS,DefaultQOS",
            ),
        ),
    ):
        observations["commands"].append(run_command(name, command, timeout_seconds=30))

    if run_smoke and apply_result.ok:
        observations["behavior_checks"].extend(run_behavior_checks())
    else:
        observations["behavior_checks"].append(
            {
                "id": "behavior_smoke",
                "status": "skipped",
                "reason": "disabled by --skip-smoke" if run_smoke is False else "apply failed",
            }
        )

    rest_result = run_command(
        "rest_auth_probe",
        ("bash", str(ROOT / "scripts/probe-sim-rest-auth.sh")),
        timeout_seconds=60,
    )
    observations["commands"].append(rest_result)
    observations["rest_probe"] = load_json_if_exists(ROOT / "artifacts/probes/sim_rest_auth.json")
    return observations


def run_behavior_checks() -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    checks.append(
        expect_submit_rejected(
            check_id="invalid_qos",
            user="alice",
            workdir="/public/home/alice",
            args=("--partition", "Students", "--qos", "definitely_not_allowed"),
        )
    )
    checks.append(
        expect_submit_rejected(
            check_id="limited_user_unauthorized_qos",
            user="bob",
            workdir="/public/home/bob",
            args=("--partition", "Students", "--qos", "qos_stu_medium_2gpu"),
        )
    )
    checks.append(
        expect_submit_rejected(
            check_id="student_competition_account_overreach",
            user="alice",
            workdir="/public/home/alice",
            args=("--partition", "P107-A100", "--qos", "qos_p107-a100"),
        )
    )
    checks.append(
        submit_and_wait_completed(
            check_id="limited_student_cpu",
            user="bob",
            workdir="/public/home/bob",
            partition="Students",
            qos="qos_stu_default",
            args=("--cpus-per-task", "1", "--time", "00:05:00"),
        )
    )
    checks.append(
        submit_and_wait_completed(
            check_id="legal_student_gpu_scheduler",
            user="alice",
            workdir="/public/home/alice",
            partition="Students",
            qos="qos_stu_medium_2gpu",
            args=("--gres", "gpu:A100:1", "--cpus-per-task", "1", "--time", "00:05:00"),
        )
    )
    return checks


def expect_submit_rejected(
    *,
    check_id: str,
    user: str,
    workdir: str,
    args: tuple[str, ...],
) -> dict[str, Any]:
    command = compose_exec_args(
        "login-node-sim",
        "sbatch",
        "--parsable",
        *args,
        "--wrap",
        "hostname",
        user=user,
        workdir=workdir,
    )
    result = run_command(check_id, command, timeout_seconds=30)
    return {
        "id": check_id,
        "expected": "rejected",
        "status": "pass" if result.returncode != 0 else "fail",
        "returncode": result.returncode,
        "stderr": _trim(result.stderr),
        "stdout": _trim(result.stdout),
    }


def submit_and_wait_completed(
    *,
    check_id: str,
    user: str,
    workdir: str,
    partition: str,
    qos: str,
    args: tuple[str, ...],
) -> dict[str, Any]:
    submit = run_command(
        f"{check_id}_submit",
        compose_exec_args(
            "login-node-sim",
            "sbatch",
            "--parsable",
            "--partition",
            partition,
            "--qos",
            qos,
            *args,
            "--wrap",
            "hostname; echo sim-behavior-report-ok",
            user=user,
            workdir=workdir,
        ),
        timeout_seconds=30,
    )
    if submit.returncode != 0:
        return {
            "id": check_id,
            "expected": "completed",
            "status": "fail",
            "phase": "submit",
            "returncode": submit.returncode,
            "stderr": _trim(submit.stderr),
            "stdout": _trim(submit.stdout),
        }

    job_id = submit.stdout.strip().split(";", 1)[0]
    row = ""
    for _ in range(20):
        sacct = run_command(
            f"{check_id}_sacct",
            compose_exec_args(
                "login-node-sim",
                "sacct",
                "-nP",
                "-j",
                job_id,
                "-X",
                "-o",
                "JobIDRaw,User,Partition,QOS,State,ExitCode",
            ),
            timeout_seconds=15,
        )
        row = sacct.stdout.strip().splitlines()[0] if sacct.stdout.strip() else ""
        if row == f"{job_id}|{user}|{partition}|{qos}|COMPLETED|0:0":
            return {
                "id": check_id,
                "expected": "completed",
                "status": "pass",
                "job_id": job_id,
                "sacct": row,
            }
        time.sleep(1)

    return {
        "id": check_id,
        "expected": "completed",
        "status": "fail",
        "phase": "accounting",
        "job_id": job_id,
        "sacct": row,
    }


def build_report(
    *,
    profile: dict[str, Any],
    manifest: dict[str, Any],
    generated_at: str,
    observations: dict[str, Any],
) -> dict[str, Any]:
    commands = observations.get("commands", [])
    commands_by_name = {command.name: command for command in commands}
    behavior_checks = observations.get("behavior_checks", [])
    rest_probe = observations.get("rest_probe")
    observed_slurm_version = parse_slurm_version(
        commands_by_name.get("scontrol_version").stdout
        if commands_by_name.get("scontrol_version")
        else ""
    )
    version_status = classify_version(
        observed=observed_slurm_version,
        target=str(profile["slurm"]["target_version"]),
        fallback=str(profile["slurm"]["fallback_version"]),
    )
    if version_status == "target" and TARGET_MANIFEST_PATH.exists():
        manifest = json.loads(TARGET_MANIFEST_PATH.read_text(encoding="utf-8"))
    scheduler_fidelity = scheduler_fidelity_summary(commands_by_name, behavior_checks)
    rest_api = rest_api_summary(
        profile,
        rest_probe,
        commands_by_name.get("rest_auth_probe"),
        version_status=version_status,
    )
    runtime_fidelity = runtime_fidelity_summary(manifest)
    known_differences = known_differences_summary(version_status, rest_api, runtime_fidelity)
    status = overall_status(scheduler_fidelity, rest_api, known_differences)

    return {
        "schema": "pilot107.simulator_real_behavior_fidelity.v1",
        "generated_at": generated_at,
        "source_docs": [
            "docs-main",
            "training-107-competition.pdf",
            "demo (2).pdf",
            "107Pilot_真实107算力平台特征补充说明_v1.0.md",
        ],
        "profile": {
            "path": str(PROFILE_PATH.relative_to(ROOT)),
            "schema": profile.get("schema"),
            "profile_id": profile.get("profile_id"),
        },
        "image": {
            "family": manifest.get("image_family"),
            "target": manifest.get("target"),
            "fallback": manifest.get("fallback"),
        },
        "slurm": {
            "observed_version": observed_slurm_version,
            "target_version": profile["slurm"]["target_version"],
            "fallback_version": profile["slurm"]["fallback_version"],
            "version_status": version_status,
            "accounting_storage_enforce": profile["slurm"]["accounting_storage_enforce"],
        },
        "rest_api": rest_api,
        "scheduler_fidelity": scheduler_fidelity,
        "runtime_fidelity": runtime_fidelity,
        "behavior_checks": behavior_checks,
        "known_differences": known_differences,
        "command_results": [command.to_payload() for command in commands],
        "summary": {"status": status},
    }


def scheduler_fidelity_summary(
    commands_by_name: dict[str, CommandResult],
    behavior_checks: list[dict[str, Any]],
) -> dict[str, str]:
    assoc = commands_by_name.get("assoc_table")
    qos = commands_by_name.get("qos_table")
    partitions = commands_by_name.get("scontrol_show_part")
    rejected = [
        check
        for check in behavior_checks
        if check.get("expected") == "rejected" and check.get("status") == "pass"
    ]
    completed = [
        check
        for check in behavior_checks
        if check.get("expected") == "completed" and check.get("status") == "pass"
    ]
    partition_ok = bool(partitions and partitions.ok and "PartitionName=" in partitions.stdout)
    return {
        "partition": "pass" if partition_ok else "fail",
        "qos": "pass" if qos and qos.ok and "qos_stu_medium_2gpu" in qos.stdout else "fail",
        "association": "pass"
        if assoc and assoc.ok and "bob|students|normal,qos_stu_default" in assoc.stdout
        else "fail",
        "rejected_jobs": "pass" if len(rejected) >= 3 else "fail",
        "accepted_jobs": "pass" if len(completed) >= 2 else "fail",
        "accounting": "pass" if len(completed) >= 2 else "fail",
        "pending_reason": "not_covered_yet",
    }


def rest_api_summary(
    profile: dict[str, Any],
    rest_probe: dict[str, Any] | None,
    rest_command: CommandResult | None,
    *,
    version_status: str,
) -> dict[str, Any]:
    summary = rest_probe.get("summary", {}) if rest_probe else {}
    status = summary.get("status")
    known_difference = None
    if status != "supported":
        known_difference = "REST/JWT behavior is not fully supported by current simulator probe."
    elif version_status != "target":
        known_difference = "REST probe is running against fallback Slurm behavior."
    return {
        "declared_api_version": profile["slurm"]["api_version"],
        "probe_status": status or ("failed" if rest_command and not rest_command.ok else "unknown"),
        "probe_artifact": "artifacts/probes/sim_rest_auth.json" if rest_probe else None,
        "auth_modes": [
            profile["slurm"]["auth"]["rest_primary"],
            profile["slurm"]["auth"]["simulator_fallback"],
        ],
        "known_difference": known_difference,
    }


def runtime_fidelity_summary(manifest: dict[str, Any]) -> dict[str, str]:
    runtime = manifest.get("runtime_fidelity", {})
    return {
        "scheduler_gres": str(runtime.get("scheduler_gres", "unknown")),
        "cuda_driver": str(runtime.get("cuda_driver", "unknown")),
        "nvml": str(runtime.get("nvml", "unknown")),
        "real_gpu_devices": str(runtime.get("real_gpu_devices", "unknown")),
        "shared_public": "declared",
        "node_local_tmp": "declared",
    }


def known_differences_summary(
    version_status: str,
    rest_api: dict[str, Any],
    runtime_fidelity: dict[str, str],
) -> list[str]:
    differences: list[str] = []
    if version_status != "target":
        differences.append(
            "Slurm fallback version is active; 25.11 target image is not yet default."
        )
    if rest_api.get("probe_status") != "supported":
        differences.append("REST/JWT behavior is not fully supported by current simulator probe.")
    if runtime_fidelity.get("real_gpu_devices") != "available":
        differences.append("Runtime GPU devices, CUDA driver, and NVML are unavailable by default.")
    differences.append("Pending Reason fidelity is not covered by the current behavior report.")
    return differences


def overall_status(
    scheduler_fidelity: dict[str, str],
    rest_api: dict[str, Any],
    known_differences: list[str],
) -> str:
    required = ("partition", "qos", "association", "rejected_jobs", "accepted_jobs", "accounting")
    if any(scheduler_fidelity.get(key) != "pass" for key in required):
        return "fail"
    if rest_api.get("probe_status") == "supported" and not known_differences:
        return "pass"
    return "limited"


def classify_version(*, observed: str | None, target: str, fallback: str) -> str:
    if not observed:
        return "unknown"
    if _version_family_matches(observed, target):
        return "target"
    if _version_family_matches(observed, fallback):
        return "fallback"
    return "mismatch"


def _version_family_matches(observed: str, expected: str) -> bool:
    return observed.startswith(expected.removesuffix(".x"))


def parse_slurm_version(stdout: str) -> str | None:
    for token in stdout.replace("\n", " ").split():
        if token[0:1].isdigit() and "." in token:
            return token
    return None


def compose_exec_args(
    service: str,
    *command: str,
    user: str | None = None,
    workdir: str | None = None,
) -> tuple[str, ...]:
    args = [
        "docker",
        "compose",
        "--env-file",
        str(ROOT / "simulator/compose/.env.example"),
        "-f",
        str(ROOT / "simulator/compose/compose.yml"),
        "exec",
        "-T",
    ]
    if user:
        args.extend(["--user", user])
    if workdir:
        args.extend(["--workdir", workdir])
    args.append(service)
    args.extend(command)
    return tuple(args)


def run_command(name: str, argv: tuple[str, ...], *, timeout_seconds: int) -> CommandResult:
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(ROOT / "src"))
    try:
        result = subprocess.run(
            list(argv),
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            cwd=ROOT,
            env=env,
        )
        return CommandResult(
            name=name,
            argv=argv,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            name=name,
            argv=argv,
            returncode=124,
            stdout=exc.stdout if isinstance(exc.stdout, str) else "",
            stderr=exc.stderr if isinstance(exc.stderr, str) else "command timed out",
        )


def load_json_if_exists(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_simple_yaml(path: Path) -> dict[str, Any]:
    lines: list[tuple[int, str]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        lines.append((indent, raw_line.strip()))

    def parse_scalar(value: str) -> Any:
        value = value.strip()
        if value == "null":
            return None
        if value == "true":
            return True
        if value == "false":
            return False
        if value.startswith("["):
            return ast.literal_eval(value)
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            return ast.literal_eval(value)
        try:
            return int(value)
        except ValueError:
            return value

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(lines) or lines[index][0] < indent:
            return {}, index
        if lines[index][0] == indent and lines[index][1].startswith("- "):
            return parse_list(index, indent)
        return parse_dict(index, indent)

    def parse_list(index: int, indent: int) -> tuple[list[Any], int]:
        items: list[Any] = []
        while index < len(lines) and lines[index][0] == indent:
            text = lines[index][1]
            if not text.startswith("- "):
                break
            content = text[2:]
            if not content:
                item, index = parse_block(index + 1, indent + 2)
                items.append(item)
                continue
            if ":" in content:
                key, value = content.split(":", 1)
                item: dict[str, Any] = {}
                if value.strip():
                    item[key] = parse_scalar(value)
                    index += 1
                else:
                    item[key], index = parse_block(index + 1, indent + 2)
                while index < len(lines) and lines[index][0] > indent:
                    child, index = parse_block(index, indent + 2)
                    if not isinstance(child, dict):
                        raise ValueError(f"unexpected nested list under {key}")
                    item.update(child)
                items.append(item)
                continue
            items.append(parse_scalar(content))
            index += 1
        return items, index

    def parse_dict(index: int, indent: int) -> tuple[dict[str, Any], int]:
        data: dict[str, Any] = {}
        while index < len(lines) and lines[index][0] == indent:
            text = lines[index][1]
            if text.startswith("- "):
                break
            key, value = text.split(":", 1)
            if value.strip():
                data[key] = parse_scalar(value)
                index += 1
            else:
                data[key], index = parse_block(index + 1, indent + 2)
        return data, index

    profile, end = parse_block(0, 0)
    if end != len(lines) or not isinstance(profile, dict):
        raise ValueError(f"could not parse profile {path}")
    return profile


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _trim(value: str, *, limit: int = 4000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n<truncated>"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
