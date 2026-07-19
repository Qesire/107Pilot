#!/usr/bin/env python3
"""Narrow command gateway for the 107Pilot Slurm simulator."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ALLOWED_COMMANDS = {
    "date",
    "env",
    "find",
    "hostname",
    "id",
    "pwd",
    "python",
    "realpath",
    "sacct",
    "sbatch",
    "scancel",
    "scontrol",
    "sha256sum",
    "squeue",
    "stat",
    "tail",
    "test",
    "which",
    "whoami",
}
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class GatewayError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class GatewayConfig:
    def __init__(
        self,
        *,
        token: str | None,
        allowed_roots: list[str],
        audit_log_path: str | None = None,
        rate_limit_max_requests: int = 1200,
        rate_limit_window_seconds: float = 60.0,
    ) -> None:
        self.token = token
        self.allowed_roots = [root.rstrip("/") or "/" for root in allowed_roots]
        self.audit_log_path = audit_log_path
        self.rate_limit_max_requests = rate_limit_max_requests
        self.rate_limit_window_seconds = rate_limit_window_seconds
        self._rate_lock = threading.Lock()
        self._rate_buckets: dict[str, tuple[float, int]] = {}
        self._audit_lock = threading.Lock()


def make_handler(config: GatewayConfig) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "pilot107-command-gateway/0.1"

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.rstrip("/")
            if path == "/healthz":
                self._send_json(200, {"status": "ok"})
                return
            if path == "/health/ready":
                self._send_json(*_slurm_readiness())
                return
            self._send_json(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            request_id = _request_id(self.headers.get("X-Request-Id"))
            started = time.time()
            payload: dict[str, Any] = {}
            status = 500
            response: dict[str, Any] = {"error": "internal_error"}
            error: str | None = None
            try:
                _check_rate_limit(config, _client_key(self))
                _check_auth(config, self.headers.get("Authorization"))
                payload = self._read_json()
                if self.path.rstrip("/") == "/run":
                    status = 200
                    response = _run(payload, config)
                elif self.path.rstrip("/") == "/realpath":
                    status = 200
                    response = {"path": _realpath(str(payload.get("path", "")))}
                elif self.path.rstrip("/") == "/write_text":
                    _write_text(payload, config)
                    status = 200
                    response = {"status": "ok"}
                else:
                    status = 404
                    response = {"error": "not_found"}
            except GatewayError as exc:
                status = exc.status
                error = str(exc)
                response = {"error": str(exc)}
            except Exception as exc:
                status = 500
                error = str(exc)
                response = {"error": str(exc)}
            response = {**response, "request_id": request_id}
            self._send_json(status, response, request_id=request_id)
            _audit_request(
                config,
                request_id=request_id,
                remote_addr=_client_key(self),
                method="POST",
                path=self.path.rstrip("/") or "/",
                status=status,
                payload=payload,
                error=error,
                duration_ms=(time.time() - started) * 1000,
            )

        def log_message(self, format: str, *args: object) -> None:
            return

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise GatewayError(f"invalid JSON: {exc}") from exc
            if not isinstance(payload, dict):
                raise GatewayError("request body must be a JSON object")
            return payload

        def _send_json(
            self,
            status: int,
            payload: dict[str, Any],
            *,
            request_id: str | None = None,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if request_id is not None:
                self.send_header("X-Request-Id", request_id)
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _slurm_readiness() -> tuple[int, dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    ping = _run_readonly_probe(["scontrol", "ping"])
    checks.append(_probe_check("scontrol_ping", ping))

    sinfo = _run_readonly_probe(["sinfo", "-h", "-o", "%P"])
    sinfo_check = _probe_check("sinfo_partitions", sinfo)
    if sinfo_check["status"] == "ok" and not sinfo["stdout"].strip():
        sinfo_check = {
            "name": "sinfo_partitions",
            "status": "fail",
            "detail": "no partitions reported",
        }
    checks.append(sinfo_check)

    ready = all(check.get("status") == "ok" for check in checks)
    payload = {
        "status": "ready" if ready else "not_ready",
        "checks": checks,
    }
    return (200 if ready else 503, payload)


def _probe_check(name: str, result: dict[str, Any]) -> dict[str, Any]:
    if result["status"] == "ok":
        return {"name": name, "status": "ok"}
    detail = result.get("detail") or ""
    return {"name": name, "status": "fail", "detail": detail[:200]}


def _run_readonly_probe(argv: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            argv,
            timeout=3,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        return {"status": "fail", "detail": f"binary missing: {exc}", "stdout": ""}
    except subprocess.TimeoutExpired as exc:
        return {"status": "fail", "detail": f"timeout after {exc.timeout}s", "stdout": ""}
    except OSError as exc:
        return {"status": "fail", "detail": f"os error: {exc}", "stdout": ""}
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        detail = stderr or f"exit code {completed.returncode}"
        return {
            "status": "fail",
            "detail": detail,
            "stdout": completed.stdout or "",
        }
    return {"status": "ok", "stdout": completed.stdout or ""}


def _request_id(header: str | None) -> str:
    value = "" if header is None else header.strip()
    if _REQUEST_ID.fullmatch(value):
        return value
    return f"gw-{uuid.uuid4().hex}"


def _client_key(handler: BaseHTTPRequestHandler) -> str:
    host, _port = handler.client_address
    return host


def _check_rate_limit(config: GatewayConfig, key: str) -> None:
    if config.rate_limit_max_requests <= 0:
        return
    now = time.monotonic()
    with config._rate_lock:
        window_started, count = config._rate_buckets.get(key, (now, 0))
        if now - window_started >= config.rate_limit_window_seconds:
            window_started = now
            count = 0
        count += 1
        config._rate_buckets[key] = (window_started, count)
    if count > config.rate_limit_max_requests:
        raise GatewayError("rate limit exceeded", status=429)


def _audit_request(
    config: GatewayConfig,
    *,
    request_id: str,
    remote_addr: str,
    method: str,
    path: str,
    status: int,
    payload: dict[str, Any],
    error: str | None,
    duration_ms: float,
) -> None:
    if not config.audit_log_path:
        return
    audit_path = Path(config.audit_log_path)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "request_id": request_id,
        "remote_addr": remote_addr,
        "method": method,
        "path": path,
        "status": status,
        "ok": 200 <= status < 400,
        "duration_ms": round(duration_ms, 3),
        "request": _audit_request_summary(path=path, payload=payload),
        "error": error,
    }
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    with config._audit_lock, audit_path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _audit_request_summary(*, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    if path == "/run":
        argv = payload.get("argv")
        argv0 = argv[0] if isinstance(argv, list) and argv and isinstance(argv[0], str) else None
        return {
            "argv0": argv0,
            "argc": len(argv) if isinstance(argv, list) else None,
            "cwd": payload.get("cwd"),
            "user": payload.get("user"),
            "timeout_seconds": payload.get("timeout_seconds"),
            "stdin_bytes": None
            if payload.get("stdin") is None
            else len(str(payload.get("stdin")).encode("utf-8")),
        }
    if path == "/realpath":
        return {"path": payload.get("path")}
    if path == "/write_text":
        content = "" if payload.get("content") is None else str(payload.get("content"))
        return {
            "path": payload.get("path"),
            "owner": payload.get("owner"),
            "content_bytes": len(content.encode("utf-8")),
        }
    return {}


def _check_auth(config: GatewayConfig, header: str | None) -> None:
    if not config.token:
        return
    if header != f"Bearer {config.token}":
        raise GatewayError("unauthorized", status=401)


def _run(payload: dict[str, Any], config: GatewayConfig) -> dict[str, Any]:
    argv = payload.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        raise GatewayError("argv must be a non-empty string array")
    if argv[0] not in ALLOWED_COMMANDS:
        raise GatewayError(f"command not allowed: {argv[0]}")
    _validate_command(argv, config)

    cwd = payload.get("cwd")
    safe_cwd = None
    if cwd is not None:
        safe_cwd = _authorize_path(str(cwd), config)

    user = payload.get("user")
    command = list(argv)
    if user is not None:
        username = _safe_user(str(user))
        pwd.getpwnam(username)
        command = ["gosu", username, *command]

    timeout = _timeout(payload)
    completed = subprocess.run(
        command,
        cwd=safe_cwd,
        input=None if payload.get("stdin") is None else str(payload.get("stdin")),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _validate_command(argv: list[str], config: GatewayConfig) -> None:
    exact_commands = {
        "date": ["date", "-Is"],
        "env": ["env"],
        "pwd": ["pwd"],
        "python": ["python", "-V"],
        "which": ["which", "python"],
        "whoami": ["whoami"],
    }
    expected = exact_commands.get(argv[0])
    if expected is not None and argv != expected:
        raise GatewayError(f"command arguments not allowed: {argv[0]}")
    if argv[0] == "test":
        if len(argv) != 3 or argv[1] not in {"-e", "-d", "-r", "-x", "-w"}:
            raise GatewayError("command arguments not allowed: test")
        _authorize_path(argv[2], config)


def _write_text(payload: dict[str, Any], config: GatewayConfig) -> None:
    path = _authorize_path(str(payload.get("path", "")), config)
    content = str(payload.get("content", ""))
    owner = _safe_user(str(payload.get("owner", "")))
    user_info = pwd.getpwnam(owner)
    target = Path(path)
    if not target.parent.exists():
        raise GatewayError(f"parent directory does not exist: {target.parent}")
    target.write_text(content, encoding="utf-8")
    os.chown(target, user_info.pw_uid, user_info.pw_gid)


def _authorize_path(path: str, config: GatewayConfig) -> str:
    resolved = _realpath(path)
    for root in config.allowed_roots:
        resolved_root = _realpath(root).rstrip("/")
        if resolved == resolved_root or resolved.startswith(f"{resolved_root}/"):
            return resolved
    raise GatewayError(f"path outside allowed roots: {path}")


def _realpath(path: str) -> str:
    if not path.startswith("/") or "\x00" in path:
        raise GatewayError("path must be absolute and must not contain NUL")
    completed = subprocess.run(
        ["realpath", "-m", path],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    if completed.returncode != 0:
        raise GatewayError(completed.stderr.strip() or "realpath failed")
    return completed.stdout.strip()


def _safe_user(user: str) -> str:
    if not user or any(not (char.isalnum() or char in "_.-") for char in user):
        raise GatewayError("invalid user")
    return user


def _timeout(payload: dict[str, Any]) -> float:
    try:
        timeout = float(payload.get("timeout_seconds", 10))
    except (TypeError, ValueError) as exc:
        raise GatewayError("invalid timeout_seconds") from exc
    return min(max(timeout, 1.0), 120.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the 107Pilot command gateway.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()

    raw_allowed_roots = os.environ.get("PILOT107_GATEWAY_ALLOWED_ROOTS", "/public/home/alice")
    allowed_roots = [
        item.strip()
        for item in raw_allowed_roots.split(",")
        if item.strip()
    ]
    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(
            GatewayConfig(
                token=os.environ.get("PILOT107_COMMAND_GATEWAY_TOKEN") or None,
                allowed_roots=allowed_roots,
                audit_log_path=os.environ.get("PILOT107_GATEWAY_AUDIT_LOG") or None,
                rate_limit_max_requests=int(
                    os.environ.get("PILOT107_GATEWAY_RATE_LIMIT_MAX", "1200")
                ),
                rate_limit_window_seconds=float(
                    os.environ.get("PILOT107_GATEWAY_RATE_LIMIT_WINDOW_SECONDS", "60")
                ),
            )
        ),
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
