"""Versioned compute-job runtime probe and PlatformSnapshot parser."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from pilot107.core.platform_snapshot import (
    CommandObservation,
    ObservationSourceType,
    ObservedAvailability,
    PlatformSnapshot,
    PlatformSnapshotScope,
    RuntimeLimitation,
    RuntimeLimitationName,
)
from pilot107.core.platform_snapshot_store import PlatformSnapshotRecord, PlatformSnapshotStore

PROBE_SCHEMA = "pilot107.compute_runtime_probe.v1"
_OUTPUT_PREFIX = "PILOT107_COMPUTE_PROBE_JSON="


def compute_runtime_probe_script() -> str:
    return r"""#!/bin/bash
set -eu
python - <<'PY'
import datetime
import json
import shutil
import socket
import subprocess

argv = [
    "nvidia-smi",
    "--query-gpu=name,driver_version,memory.total",
    "--format=csv,noheader,nounits",
]
if shutil.which("nvidia-smi"):
    completed = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=15)
    nvidia = {
        "argv": argv,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
else:
    nvidia = {"argv": argv, "returncode": 127, "stdout": "", "stderr": "command unavailable"}

try:
    import torch
    torch_info = {
        "available": True,
        "version": str(torch.__version__),
        "cuda_available": bool(torch.cuda.is_available()),
        "cuda_version": None if torch.version.cuda is None else str(torch.version.cuda),
        "device_count": int(torch.cuda.device_count()),
    }
except Exception as exc:
    torch_info = {"available": False, "error_type": type(exc).__name__}

payload = {
    "schema": "pilot107.compute_runtime_probe.v1",
    "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "hostname": socket.gethostname(),
    "nvidia_smi": nvidia,
    "torch": torch_info,
}
print("PILOT107_COMPUTE_PROBE_JSON=" + json.dumps(payload, sort_keys=True, separators=(",", ":")))
PY
"""


def parse_compute_runtime_probe_output(
    output: str,
    *,
    job_id: str,
    collector_version: str = "pilot107.compute_runtime_probe.v1",
) -> PlatformSnapshot:
    if len(output) > 500_000:
        raise ValueError("compute probe output exceeds limit")
    lines = [line for line in output.splitlines() if line.startswith(_OUTPUT_PREFIX)]
    if len(lines) != 1:
        raise ValueError("compute probe output must contain exactly one JSON record")
    try:
        payload = json.loads(lines[0][len(_OUTPUT_PREFIX) :])
    except json.JSONDecodeError as exc:
        raise ValueError("compute probe JSON is invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema") != PROBE_SCHEMA:
        raise ValueError("compute probe schema is invalid")
    captured_at = _timestamp(payload.get("captured_at"))
    nvidia = payload.get("nvidia_smi")
    if not isinstance(nvidia, dict):
        raise ValueError("compute probe nvidia_smi result is invalid")
    expected_argv = (
        "nvidia-smi",
        "--query-gpu=name,driver_version,memory.total",
        "--format=csv,noheader,nounits",
    )
    argv = nvidia.get("argv")
    if not isinstance(argv, list) or tuple(argv) != expected_argv:
        raise ValueError("compute probe nvidia_smi argv is invalid")
    returncode = nvidia.get("returncode")
    if not isinstance(returncode, int):
        raise ValueError("compute probe nvidia_smi returncode is invalid")
    command = CommandObservation(
        name="nvidia_smi_query",
        argv=expected_argv,
        returncode=returncode,
        stdout=_bounded_text(nvidia.get("stdout")),
        stderr=_bounded_text(nvidia.get("stderr")),
    )
    torch_payload = payload.get("torch")
    if not isinstance(torch_payload, dict):
        raise ValueError("compute probe torch result is invalid")
    runtime_available = returncode == 0 or torch_payload.get("cuda_available") is True
    warning = None
    limitations: tuple[str, ...] = ()
    if not runtime_available:
        warning = "GPU runtime was not available inside the allocated compute job."
        limitations = ("allocated compute job GPU runtime unavailable",)
    digest = hashlib.sha256(lines[0].encode("utf-8")).hexdigest()[:16]
    return PlatformSnapshot(
        snapshot_id=f"compute-{job_id}-{digest}",
        scope=PlatformSnapshotScope.COMPUTE_JOB,
        captured_at=captured_at,
        collector_version=collector_version,
        command_results=(command,),
        runtime_limitations=(
            RuntimeLimitation(
                name=RuntimeLimitationName.GPU_RUNTIME,
                availability=(
                    ObservedAvailability.KNOWN
                    if runtime_available
                    else ObservedAvailability.UNAVAILABLE
                ),
                source_type=ObservationSourceType.CLI,
                source_name=f"allocated-job:{job_id}",
                captured_at=captured_at,
                raw_artifact=f"runs/{job_id}/stdout",
                warning=warning,
            ),
        ),
        limitations=limitations,
    )


def store_compute_runtime_probe_output(
    *,
    store: PlatformSnapshotStore,
    owner: str,
    job_id: str,
    output: str,
    source_type: ObservationSourceType,
    source_name: str,
    ttl_seconds: int = 24 * 60 * 60,
) -> PlatformSnapshotRecord:
    if ttl_seconds <= 0 or ttl_seconds > 7 * 24 * 60 * 60:
        raise ValueError("snapshot TTL must be between 1 second and 7 days")
    snapshot = parse_compute_runtime_probe_output(output, job_id=job_id)
    captured = datetime.fromisoformat(snapshot.captured_at).astimezone(UTC)
    return store.create(
        owner=owner,
        snapshot=snapshot,
        source_type=source_type,
        source_name=source_name,
        expires_at=(captured + timedelta(seconds=ttl_seconds)).isoformat(),
    )


def _timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("compute probe captured_at is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("compute probe captured_at is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("compute probe captured_at must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _bounded_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("compute probe command output is invalid")
    if len(value) > 200_000:
        raise ValueError("compute probe command output exceeds limit")
    return value
