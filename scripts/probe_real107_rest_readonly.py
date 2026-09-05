"""Read-only REST smoke probe against the REAL 107 platform.

Formalizes the one-off ConfigurationSnapshot probe (job 21039) into a
repeatable, non-blocking script. Performs ONLY HTTP GET calls; never
submits, cancels, or reads user files. The token is read from the
``PILOT107_REAL107_TOKEN`` env var and used only in the Authorization /
X-SLURM-USER-* headers; it is never printed, logged, or written to the
output artifact.

If ``PILOT107_REAL107_TOKEN`` is unset or empty, the probe exits 0 with
``status=skipped`` (matches the project's non-blocking smoke convention).

Output: ``artifacts/probes/real107_rest_readonly.json``
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_URL = "http://107.ustc.edu.cn:6820"
TIMEOUT_SECONDS = 10.0

# Read-only GET endpoints (see docs/phase-0/real_platform_compatibility_plan.md).
# Only HTTP GET; no submit/cancel/file-read.
ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("ping", "/slurm/v0.0.41/ping"),
    ("nodes", "/slurm/v0.0.41/nodes"),
    ("jobs", "/slurm/v0.0.41/jobs"),
    ("partitions", "/slurm/v0.0.41/partitions"),
)
OPENAPI_PATHS: tuple[str, ...] = ("/openapi/v3", "/openapi")


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    output_path = root / "artifacts" / "probes" / "real107_rest_readonly.json"

    url = os.environ.get("PILOT107_REAL107_REST_URL", DEFAULT_URL).rstrip("/")
    token = os.environ.get("PILOT107_REAL107_TOKEN", "").strip()

    if not token:
        return _write_skipped(output_path, url)

    endpoints: dict[str, Any] = {}
    openapi_digest: str | None = None
    openapi_status: int | None = None
    openapi_errors = False

    for name, path in ENDPOINTS:
        endpoints[name] = _probe_get(url, path, token)

    # OpenAPI: try /openapi/v3 first, fall back to /openapi.
    for path in OPENAPI_PATHS:
        result = _probe_get(url, path, token, capture_body=True)
        if result["http_status"] is not None and result["http_status"] < 400:
            openapi_status = result["http_status"]
            openapi_errors = bool(result.get("has_errors"))
            openapi_digest = result.get("body_digest")
            break
        openapi_status = result["http_status"]
        openapi_errors = bool(result.get("has_errors"))
        # keep trying fallback paths

    status = _aggregate_status(endpoints, openapi_digest, openapi_status)
    payload = {
        "url": url,
        "status": status,
        "endpoints": endpoints,
        "openapi": {
            "status": openapi_status,
            "has_errors": openapi_errors,
            "digest": openapi_digest,
        },
        "openapi_digest": openapi_digest,
        "captured_at": datetime.now(UTC).isoformat(),
        "summary": _summary(status, endpoints, openapi_digest),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print("real107 rest readonly probe " + status)
    print("artifact=" + str(output_path))
    return 0


def _write_skipped(output_path: Path, url: str) -> int:
    payload = {
        "url": url,
        "status": "skipped",
        "endpoints": {},
        "openapi": {"status": None, "has_errors": False, "digest": None},
        "openapi_digest": None,
        "captured_at": datetime.now(UTC).isoformat(),
        "summary": {"reason": "PILOT107_REAL107_TOKEN not set; probe skipped"},
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print("real107 rest readonly probe skipped")
    print("artifact=" + str(output_path))
    return 0


def _probe_get(
    base_url: str,
    path: str,
    token: str,
    *,
    capture_body: bool = False,
) -> dict[str, Any]:
    """Issue a single GET. Token goes ONLY into auth headers; never returned."""
    request = urllib.request.Request(
        url=f"{base_url}{path}",
        headers={
            "Accept": "application/json",
            # Slurm JWT confirmed style: X-SLURM-USER-* headers.
            "X-SLURM-USER-TOKEN": token,
            # Bearer fallback included; harmless if server ignores it.
            "Authorization": f"Bearer {token}",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read()
            status = response.status
            payload = _safe_json(raw)
            body_digest = hashlib.sha256(raw).hexdigest()[:16] if capture_body else None
            return _result(
                status=status,
                payload=payload,
                body_digest=body_digest,
                capture_body=capture_body,
            )
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        payload = _safe_json(raw)
        body_digest = hashlib.sha256(raw).hexdigest()[:16] if capture_body else None
        return _result(
            status=exc.code,
            payload=payload,
            body_digest=body_digest,
            capture_body=capture_body,
            error=None,
        )
    except OSError as exc:
        # Connection-level failure: do NOT include token (it isn't in exc).
        return {
            "http_status": None,
            "has_errors": False,
            "shape": None,
            "error": f"{type(exc).__name__}: {exc}",
            "body_digest": None,
        }


def _result(
    *,
    status: int,
    payload: Any,
    body_digest: str | None,
    capture_body: bool,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "http_status": status,
        "has_errors": bool(isinstance(payload, dict) and payload.get("errors")),
        "shape": _redacted_shape(payload),
        "error": error,
        "body_digest": body_digest if capture_body else None,
    }


def _safe_json(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return None


def _redacted_shape(payload: Any) -> dict[str, Any]:
    """Return a redacted summary: keys + counts, NEVER full payload.

    Real responses may include user/job data, so we keep only structural
    fingerprints (top-level keys, list lengths, scalar presence).
    """
    if payload is None:
        return {"type": "empty"}
    if isinstance(payload, dict):
        shape: dict[str, Any] = {}
        for key in sorted(payload):
            value = payload[key]
            if isinstance(value, list):
                shape[key] = {"type": "list", "length": len(value)}
            elif isinstance(value, dict):
                shape[key] = {"type": "object", "keys": sorted(value)[:16]}
            else:
                shape[key] = {"type": type(value).__name__}
        return shape
    if isinstance(payload, list):
        return {"type": "list", "length": len(payload)}
    return {"type": type(payload).__name__}


def _aggregate_status(
    endpoints: dict[str, Any],
    openapi_digest: str | None,
    openapi_status: int | None,
) -> str:
    if not endpoints:
        return "failed"
    statuses = [e["http_status"] for e in endpoints.values() if e["http_status"] is not None]
    if not statuses and openapi_status is None:
        return "failed"
    all_ok = all(s is not None and s < 400 for s in statuses)
    openapi_ok = openapi_status is not None and openapi_status < 400
    if all_ok and openapi_ok and openapi_digest is not None:
        return "ok"
    if any(s is not None and s < 400 for s in statuses) or openapi_ok:
        return "partial"
    return "failed"


def _summary(status: str, endpoints: dict[str, Any], openapi_digest: str | None) -> dict[str, Any]:
    return {
        "status": status,
        "endpoint_statuses": {name: e.get("http_status") for name, e in endpoints.items()},
        "openapi_digest": openapi_digest,
        "read_only": True,
        "methods": ["GET"],
        "token_persisted": False,
    }


if __name__ == "__main__":
    raise SystemExit(main())
