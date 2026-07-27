#!/usr/bin/env python3
"""Emit one bounded compute-job runtime observation as JSON.

The script is intentionally fixed: it does not receive a path, command, token,
or user payload. It neither enumerates directories nor reads project files.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import socket
import stat
import subprocess
from pathlib import Path
from typing import Any

PREFIX = "PILOT107_COMPUTE_PROBE_JSON="
NVIDIA_ARGV = (
    "nvidia-smi",
    "--query-gpu=name,driver_version,memory.total",
    "--format=csv,noheader,nounits",
)
SLURM_ENV_NAMES = (
    "SLURM_JOB_ID",
    "SLURM_JOB_PARTITION",
    "SLURM_JOB_QOS",
    "SLURM_CPUS_PER_TASK",
    "SLURM_JOB_GPUS",
    "CUDA_VISIBLE_DEVICES",
)


def main() -> int:
    payload = {
        "schema": "pilot107.compute_runtime_probe.v1",
        "captured_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "hostname": socket.gethostname(),
        "nvidia_smi": _nvidia_smi(),
        "torch": _torch(),
        "slurm_environment": {name: os.environ.get(name) for name in SLURM_ENV_NAMES},
        "filesystem": [
            _filesystem(label, path)
            for label, path in (("public", "/public"), ("home", "/home"), ("tmp", "/tmp"))
        ],
    }
    print(PREFIX + json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


def _nvidia_smi() -> dict[str, Any]:
    if not shutil.which("nvidia-smi"):
        return {"argv": list(NVIDIA_ARGV), "returncode": 127, "stdout": "", "stderr": "unavailable"}
    try:
        completed = subprocess.run(
            NVIDIA_ARGV,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return {"argv": list(NVIDIA_ARGV), "returncode": 124, "stdout": "", "stderr": "timed out"}
    return {
        "argv": list(NVIDIA_ARGV),
        "returncode": completed.returncode,
        "stdout": completed.stdout[:200_000],
        "stderr": completed.stderr[:20_000],
    }


def _torch() -> dict[str, Any]:
    try:
        import torch

        return {
            "available": True,
            "version": str(torch.__version__),
            "cuda_available": bool(torch.cuda.is_available()),
            "cuda_version": None if torch.version.cuda is None else str(torch.version.cuda),
            "device_count": int(torch.cuda.device_count()),
        }
    except Exception as exc:  # noqa: BLE001 - probe records an optional runtime
        return {"available": False, "error_type": type(exc).__name__}


def _filesystem(label: str, raw_path: str) -> dict[str, Any]:
    path = Path(raw_path)
    try:
        details = path.stat()
        usage = os.statvfs(path)
    except OSError as exc:
        return {"label": label, "exists": False, "error_type": type(exc).__name__}
    return {
        "label": label,
        "exists": True,
        "is_directory": stat.S_ISDIR(details.st_mode),
        "is_mount_point": os.path.ismount(path),
        "filesystem_bytes": usage.f_frsize * usage.f_blocks,
        "filesystem_available_bytes": usage.f_frsize * usage.f_bavail,
    }


if __name__ == "__main__":
    raise SystemExit(main())
