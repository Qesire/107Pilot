#!/usr/bin/env python3
# ruff: noqa: UP006,UP017,UP035,UP045
"""Read-only ConfigurationSnapshot probe for the real 107 Slurm REST API.

This script is intentionally self-contained so it can be copied to the real
cluster and run from an sbatch job without installing 107Pilot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

JsonObject = Dict[str, Any]
RequestJson = Callable[[str], Tuple[int, JsonObject]]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Probe real 107 Slurm REST read-only capabilities."
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("PILOT107_REAL107_BASE_URL", "http://107.ustc.edu.cn:6820"),
    )
    parser.add_argument(
        "--api-version",
        default=os.environ.get("PILOT107_REAL107_API_VERSION", "v0.0.41"),
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("PILOT107_REAL107_USERNAME") or _default_user(),
    )
    parser.add_argument(
        "--out-dir",
        default=os.environ.get("PILOT107_REAL107_OUT_DIR", "real107-probe-output"),
    )
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--token-env", default="PILOT107_REAL107_TOKEN")
    parser.add_argument(
        "--token-command",
        default=os.environ.get("PILOT107_REAL107_TOKEN_COMMAND", ""),
        help="Optional argv command such as: scontrol token lifespan=600",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    token, token_source, token_error = _load_token(
        env_name=args.token_env,
        token_command=args.token_command,
    )

    captured_at = _utc_now()
    if token is None:
        snapshot = _fallback_snapshot(
            base_url=args.base_url,
            api_version=args.api_version,
            username=args.username,
            captured_at=captured_at,
        )
        report = {
            "observed_at": captured_at,
            "collector": "pilot107.real107_configuration_snapshot_probe.v1",
            "redacted": True,
            "target": _endpoint(args.base_url),
            "api_version": args.api_version,
            "summary": {
                "status": "auth_required",
                "reason": token_error or "missing token",
            },
            "token_source": token_source,
            "probes": [],
            "warnings": ["No token was available; wrote fallback snapshot only."],
            "errors": [{"code": "AUTH_REQUIRED", "message": token_error or "missing token"}],
        }
    else:
        requester = _urllib_requester(
            base_url=args.base_url,
            api_version=args.api_version,
            token=token,
            timeout_seconds=args.timeout_seconds,
        )
        snapshot, report = build_snapshot_from_probe(
            base_url=args.base_url,
            api_version=args.api_version,
            username=args.username,
            request_json=requester,
            captured_at=captured_at,
            token_source=token_source,
        )

    _write_json(out_dir / "configuration_snapshot.json", snapshot)
    _write_json(out_dir / "probe_report.json", report)
    print("configuration_snapshot=" + str(out_dir / "configuration_snapshot.json"))
    print("probe_report=" + str(out_dir / "probe_report.json"))
    print("summary_status=" + str(report.get("summary", {}).get("status", "unknown")))
    return 0


def build_snapshot_from_probe(
    *,
    base_url: str,
    api_version: str,
    username: str,
    request_json: RequestJson,
    captured_at: str,
    token_source: str = "provided",
) -> Tuple[JsonObject, JsonObject]:
    probes: List[JsonObject] = []
    warnings: List[str] = []
    errors: List[JsonObject] = []

    ping = _probe_endpoint("ping", "/ping", request_json)
    probes.append(ping)

    partitions_probe = _probe_endpoint("partitions", "/partitions", request_json)
    probes.append(partitions_probe)

    nodes_probe = _probe_endpoint("nodes", "/nodes", request_json)
    probes.append(nodes_probe)

    jobs_probe = _probe_endpoint("jobs", "/jobs", request_json)
    probes.append(jobs_probe)

    openapi_probe, openapi_digest = _probe_openapi(request_json)
    probes.append(openapi_probe)

    for probe in probes:
        if probe["status"] != "ok":
            warnings.append(f"{probe['name']} probe {probe['status']}")
            if probe["status"] in {"auth_required", "auth_expired", "forbidden"}:
                errors.append({"code": probe["status"].upper(), "message": probe.get("message")})

    partitions = _extract_partitions(partitions_probe.get("payload_summary", {}))
    qos = _extract_qos(partitions_probe.get("payload_summary", {}))
    slurm_version = _extract_slurm_version(ping.get("payload_summary", {}))
    default_partition = partitions[0] if partitions else "unknown"
    default_qos = qos[0] if qos else None

    snapshot = {
        "cluster": {
            "name": "real-107",
            "slurm_version": slurm_version,
            "api_version": api_version,
            "shared_roots": ["/public"],
            "local_roots": ["/tmp", "/usr", "/var", "/opt"],
            "partitions": partitions,
            "qos": qos,
            "source_authority": "real_cluster_probe",
        },
        "users": [
            {
                "username": username,
                "home": f"/public/home/{username}",
                "allowed_roots": [f"/public/home/{username}"],
                "default_partition": default_partition,
                "default_qos": default_qos,
                "source_authority": "real_cluster_probe",
            }
        ],
        "endpoints": {
            "slurm_rest_url": _endpoint(base_url),
            "command_gateway_url": None,
            "evidence_transport_url": None,
        },
        "auth_strategy": "single_user_jwt_bearer",
        "openapi_digest": openapi_digest,
        "captured_at": captured_at,
        "freshness_seconds": 300,
    }

    report = {
        "observed_at": captured_at,
        "collector": "pilot107.real107_configuration_snapshot_probe.v1",
        "redacted": True,
        "target": _endpoint(base_url),
        "api_version": api_version,
        "token_source": token_source,
        "summary": {
            "status": _summary_status(probes),
            "ok_probes": [probe["name"] for probe in probes if probe["status"] == "ok"],
            "failed_probes": [probe["name"] for probe in probes if probe["status"] != "ok"],
        },
        "probes": probes,
        "warnings": warnings,
        "errors": errors,
    }
    return snapshot, report


def _probe_endpoint(name: str, path: str, request_json: RequestJson) -> JsonObject:
    try:
        http_status, payload = request_json(path)
    except ProbeHttpError as exc:
        return {
            "name": name,
            "path": path,
            "status": _classify_http_error(exc.status, exc.payload, exc.message),
            "http_status": exc.status,
            "message": exc.message,
            "payload_summary": _summarize_payload(exc.payload),
        }
    except Exception as exc:
        return {
            "name": name,
            "path": path,
            "status": "transport_error",
            "http_status": None,
            "message": str(exc),
            "payload_summary": {},
        }
    return {
        "name": name,
        "path": path,
        "status": "ok" if http_status < 400 else "failed",
        "http_status": http_status,
        "message": None,
        "payload_summary": _summarize_payload(payload),
    }


def _probe_openapi(request_json: RequestJson) -> Tuple[JsonObject, Optional[str]]:
    for path in ("/openapi.json", "/../openapi.json"):
        probe = _probe_endpoint("openapi", path, request_json)
        if probe["status"] == "ok":
            digest_source = json.dumps(probe["payload_summary"], sort_keys=True).encode()
            return probe, hashlib.sha256(digest_source).hexdigest()
    return probe, None


class ProbeHttpError(RuntimeError):
    def __init__(self, *, status: int, message: str, payload: JsonObject) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.payload = payload


def _urllib_requester(
    *,
    base_url: str,
    api_version: str,
    token: str,
    timeout_seconds: float,
) -> RequestJson:
    endpoint = _endpoint(base_url)

    def request_json(path: str) -> Tuple[int, JsonObject]:
        if path.startswith("/../"):
            url = endpoint + path[3:]
        else:
            url = f"{endpoint}/slurm/{api_version}{path}"
        request = urllib.request.Request(
            url=url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read()
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                return int(response.status), _object_payload(payload)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            payload = _parse_error_payload(raw)
            raise ProbeHttpError(
                status=exc.code,
                message=_error_message(payload),
                payload=payload,
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(str(exc)) from exc

    return request_json


def _load_token(*, env_name: str, token_command: str) -> Tuple[Optional[str], str, Optional[str]]:
    token = os.environ.get(env_name)
    if token and token.strip():
        return token.strip(), f"env:{env_name}", None
    if token_command.strip():
        try:
            completed = subprocess.run(
                shlex.split(token_command),
                check=False,
                text=True,
                capture_output=True,
                timeout=20,
            )
        except Exception as exc:
            return None, "command", str(exc)
        if completed.returncode != 0:
            return None, "command", completed.stderr.strip() or "token command failed"
        parsed = _parse_token_text(completed.stdout)
        if parsed:
            return parsed, "command", None
        return None, "command", "token command did not return a token"
    return None, "none", f"set {env_name} or PILOT107_REAL107_TOKEN_COMMAND"


def _parse_token_text(value: str) -> Optional[str]:
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        if "=" in line:
            _name, token = line.split("=", 1)
            token = token.strip().strip("'\"")
            if token:
                return token
        return line.strip("'\"")
    return None


def _fallback_snapshot(
    *,
    base_url: str,
    api_version: str,
    username: str,
    captured_at: str,
) -> JsonObject:
    return {
        "cluster": {
            "name": "real-107",
            "slurm_version": "unknown",
            "api_version": api_version,
            "shared_roots": ["/public"],
            "local_roots": ["/tmp", "/usr", "/var", "/opt"],
            "partitions": [],
            "qos": [],
            "source_authority": "real_cluster_probe",
        },
        "users": [
            {
                "username": username,
                "home": f"/public/home/{username}",
                "allowed_roots": [f"/public/home/{username}"],
                "default_partition": "unknown",
                "default_qos": None,
                "source_authority": "real_cluster_probe",
            }
        ],
        "endpoints": {
            "slurm_rest_url": _endpoint(base_url),
            "command_gateway_url": None,
            "evidence_transport_url": None,
        },
        "auth_strategy": "single_user_jwt_bearer",
        "openapi_digest": None,
        "captured_at": captured_at,
        "freshness_seconds": 300,
    }


def _summarize_payload(payload: Any) -> JsonObject:
    payload = _object_payload(payload)
    summary: JsonObject = {}
    for key, value in payload.items():
        if key in {"warnings", "errors", "meta"}:
            summary[key] = value
        elif isinstance(value, list):
            summary[key] = [_summarize_item(item) for item in value[:200]]
            summary[f"{key}_count"] = len(value)
        elif isinstance(value, dict):
            summary[key] = _summarize_item(value)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            summary[key] = value
    return _redact(summary)


def _summarize_item(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    keep = {}
    for key in (
        "name",
        "partition",
        "state",
        "qos",
        "qos_allowed",
        "default",
        "nodes",
        "job_id",
        "job_state",
        "user_name",
        "user",
        "version",
        "slurm_version",
    ):
        if key in item:
            keep[key] = item[key]
    return keep or {key: item[key] for key in sorted(item)[:10]}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if any(secret in key.lower() for secret in ("token", "authorization", "jwt", "cookie")):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str) and len(value) > 4096:
        return value[:4096] + "...<truncated>"
    return value


def _extract_partitions(summary: JsonObject) -> List[str]:
    names: List[str] = []
    for item in summary.get("partitions", []):
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("partition")
        if name is not None:
            names.append(str(name))
    return sorted(set(names))


def _extract_qos(summary: JsonObject) -> List[str]:
    values: List[str] = []
    for item in summary.get("partitions", []):
        if not isinstance(item, dict):
            continue
        raw = item.get("qos") or item.get("qos_allowed")
        if isinstance(raw, list):
            values.extend(str(entry) for entry in raw if entry)
        elif isinstance(raw, str):
            values.extend(entry for entry in raw.replace(",", " ").split() if entry)
    return sorted(set(values))


def _extract_slurm_version(summary: JsonObject) -> str:
    for source in (summary, summary.get("meta", {})):
        if isinstance(source, dict):
            for key in ("slurm_version", "version"):
                if source.get(key):
                    return str(source[key])
    return "unknown"


def _summary_status(probes: List[JsonObject]) -> str:
    ok = [probe for probe in probes if probe["status"] == "ok"]
    auth_failures = [
        probe for probe in probes if probe["status"] in {"auth_required", "auth_expired"}
    ]
    if ok:
        return "ok" if len(ok) == len(probes) else "partial"
    if auth_failures:
        return "auth_required"
    return "failed"


def _classify_http_error(status: int, payload: JsonObject, message: str) -> str:
    lowered = json.dumps(payload, sort_keys=True).lower() + " " + message.lower()
    if status == 401 and "expired" in lowered:
        return "auth_expired"
    if status == 401:
        return "auth_required"
    if status == 403:
        return "forbidden"
    return "failed"


def _parse_error_payload(raw: bytes) -> JsonObject:
    try:
        return _object_payload(json.loads(raw.decode("utf-8")) if raw else {})
    except Exception:
        return {"message": raw.decode("utf-8", errors="replace")}


def _error_message(payload: JsonObject) -> str:
    if payload.get("message"):
        return str(payload["message"])
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            return str(first.get("description") or first.get("message") or first)
        return str(first)
    return "HTTP error"


def _object_payload(payload: Any) -> JsonObject:
    return payload if isinstance(payload, dict) else {"value": payload}


def _endpoint(base_url: str) -> str:
    return base_url.rstrip("/")


def _default_user() -> str:
    return os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: JsonObject) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
