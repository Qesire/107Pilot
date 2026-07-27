#!/usr/bin/env python3
"""Collect a bounded, read-only real107 environment inventory.

This probe intentionally captures platform metadata rather than user data.  It
does not enumerate directory contents, read project files, mint credentials,
or accept arbitrary commands/paths.  Its output is suitable for comparing the
Docker simulator with a normal user's login environment.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "pilot107.real107_environment_inventory.v1"
MAX_OUTPUT_CHARS = 80_000
SAFE_CONFIG_KEYS = frozenset(
    {
        "AccountingStorageEnforce",
        "DefMemPerCPU",
        "JobAcctGatherFrequency",
        "JobAcctGatherType",
        "MaxMemPerCPU",
        "MinJobAge",
        "PreemptMode",
        "PreemptType",
        "PriorityType",
        "ProctrackType",
        "SchedulerParameters",
        "SchedulerType",
        "SelectType",
        "SelectTypeParameters",
        "SlurmctldTimeout",
        "SlurmdTimeout",
        "TaskPlugin",
    }
)
FIXED_COMMANDS: dict[str, tuple[str, ...]] = {
    "slurm_versions": ("scontrol", "--version"),
    "scheduler_config": ("scontrol", "show", "config"),
    "qos": (
        "sacctmgr",
        "-nP",
        "show",
        "qos",
        "format=Name,MaxWall,MaxTRESPerUser,MaxTRESPerNode,MaxTRESPerJob,MaxJobsPU,MaxSubmitJobsPU,Flags",
    ),
    "partitions": ("scontrol", "show", "part"),
}
RUNTIME_PROGRAMS = (
    "python",
    "python3",
    "sbatch",
    "srun",
    "sacct",
    "scontrol",
    "apptainer",
    "singularity",
    "enroot",
    "docker",
    "podman",
    "modulecmd",
    "nvcc",
    "nvidia-smi",
)
DIRECTORY_LABELS = (
    ("root", "/"),
    ("public", "/public"),
    ("public_home", "/public/home"),
    ("home", "/home"),
    ("tmp", "/tmp"),
    ("dev_shm", "/dev/shm"),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect a fixed, read-only real107 environment inventory."
    )
    parser.add_argument("--out-dir", default="real107-environment-inventory")
    args = parser.parse_args(argv)

    output = Path(args.out_dir)
    if output.exists():
        raise SystemExit(f"refusing to overwrite existing output directory: {output}")
    output.mkdir(parents=True, mode=0o700)

    home = Path(os.environ.get("HOME", "/nonexistent"))
    inventory = build_inventory(home=home)
    (output / "environment_inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output / "redaction-report.json").write_text(
        json.dumps(
            {
                "redactions": [
                    "user home paths are represented as <home>",
                    "scheduler configuration is allowlisted by key; raw config is not stored",
                    "directory contents, credentials, environment variables, and project files "
                    "are not collected",
                ]
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print("environment_inventory=" + str(output / "environment_inventory.json"))
    return 0


def build_inventory(*, home: Path) -> dict[str, Any]:
    commands = {name: _run(argv) for name, argv in FIXED_COMMANDS.items()}
    return {
        "schema": SCHEMA,
        "captured_at": datetime.now(UTC).isoformat(),
        "scope": "login_node",
        "directory_layout": _directory_layout(home=home),
        "scheduler": {
            "version": commands["slurm_versions"],
            "config": _extract_safe_config(commands["scheduler_config"]),
            "qos": commands["qos"],
            "partitions": commands["partitions"],
        },
        "runtime": _runtime_inventory(),
        "resource_limits": _resource_limits(),
        "collection_limits": {
            "max_command_output_chars": MAX_OUTPUT_CHARS,
            "directory_contents_enumerated": False,
            "credentials_collected": False,
        },
    }


def _run(argv: tuple[str, ...]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
            env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C", "LANG": "C"},
        )
    except FileNotFoundError:
        return {"argv": list(argv), "returncode": 127, "stdout": "", "stderr": "unavailable"}
    except subprocess.TimeoutExpired:
        return {"argv": list(argv), "returncode": 124, "stdout": "", "stderr": "timed out"}
    return {
        "argv": list(argv),
        "returncode": completed.returncode,
        "stdout": _bounded(completed.stdout),
        "stderr": _bounded(completed.stderr),
    }


def _bounded(value: str) -> str:
    return value if len(value) <= MAX_OUTPUT_CHARS else value[:MAX_OUTPUT_CHARS] + "\n<TRUNCATED>\n"


def _extract_safe_config(command: dict[str, Any]) -> dict[str, Any]:
    result = {"argv": command["argv"], "returncode": command["returncode"], "fields": {}}
    if command["returncode"] != 0:
        result["stderr"] = command["stderr"]
        return result
    fields: dict[str, str] = {}
    for line in str(command["stdout"]).splitlines():
        if "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key in SAFE_CONFIG_KEYS:
            fields[key] = value
    result["fields"] = fields
    return result


def _directory_layout(*, home: Path) -> dict[str, Any]:
    username = os.environ.get("USER", "")
    public_home = Path("/public/home") / username if _safe_username(username) else None
    result: list[dict[str, Any]] = []
    for label, fixed_path in DIRECTORY_LABELS:
        path = home if fixed_path is None else Path(fixed_path)
        result.append(
            _directory_metadata(label=label, path=path, home=home, public_home=public_home)
        )
    result.append(
        _directory_metadata(
            label="user_home",
            path=home,
            home=home,
            public_home=public_home,
        )
    )
    if public_home is not None:
        result.append(
            _directory_metadata(
                label="public_user_home",
                path=public_home,
                home=home,
                public_home=public_home,
            )
        )
    return {
        "directories": result,
        "home_and_public_home_same_inode": _same_inode(home, public_home),
    }


def _directory_metadata(
    *, label: str, path: Path, home: Path, public_home: Path | None
) -> dict[str, Any]:
    try:
        info = path.stat()
        usage = os.statvfs(path)
    except OSError as exc:
        return {"label": label, "exists": False, "error_type": type(exc).__name__}
    return {
        "label": label,
        "path": _redacted_path(path, home=home, public_home=public_home),
        "exists": True,
        "is_directory": stat.S_ISDIR(info.st_mode),
        "mode": stat.filemode(info.st_mode),
        "is_mount_point": os.path.ismount(path),
        "device": int(info.st_dev),
        "filesystem_bytes": usage.f_frsize * usage.f_blocks,
        "filesystem_available_bytes": usage.f_frsize * usage.f_bavail,
    }


def _redacted_path(path: Path, *, home: Path, public_home: Path | None) -> str:
    try:
        resolved = path.resolve()
        resolved_home = home.resolve()
    except OSError:
        resolved = path
        resolved_home = home
    if resolved == resolved_home:
        return "<home>"
    if public_home is not None:
        try:
            if resolved == public_home.resolve():
                return "<public-home>"
        except OSError:
            pass
    try:
        relative = resolved.relative_to(resolved_home)
    except ValueError:
        return str(resolved)
    return "<home>/" + str(relative)


def _same_inode(first: Path, second: Path | None) -> bool | None:
    if second is None:
        return None
    try:
        return os.path.samefile(first, second)
    except OSError:
        return None


def _safe_username(value: str) -> bool:
    return bool(value) and all(character.isalnum() or character in "_.-" for character in value)


def _runtime_inventory() -> dict[str, Any]:
    programs: dict[str, dict[str, Any]] = {}
    for program in RUNTIME_PROGRAMS:
        found = shutil.which(program)
        programs[program] = {"available": found is not None}
    return {"programs": programs, "python_version": _run(("python", "-V"))}


def _resource_limits() -> dict[str, int | str | None]:
    names = ("RLIMIT_NOFILE", "RLIMIT_NPROC", "RLIMIT_CORE", "RLIMIT_STACK")
    limits: dict[str, int | str | None] = {}
    for name in names:
        limit = getattr(resource, name, None)
        if limit is None:
            limits[name] = None
            continue
        soft, hard = resource.getrlimit(limit)
        limits[name] = f"{_limit_value(soft)}:{_limit_value(hard)}"
    return limits


def _limit_value(value: int) -> int | str:
    return "infinity" if value == resource.RLIM_INFINITY else value


if __name__ == "__main__":
    raise SystemExit(main())
