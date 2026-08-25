#!/usr/bin/env python3
"""Narrow command gateway for the 107Pilot Slurm simulator."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import posixpath
import pwd
import re
import shutil
import stat
import subprocess
import tarfile
import threading
import time
import uuid
import zipfile
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
    "sinfo",
    "squeue",
    "sstat",
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
        seed = token.encode("utf-8") if token else os.urandom(32)
        self._cursor_key = hashlib.sha256(b"pilot107-file-search\0" + seed).digest()


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
                elif self.path.rstrip("/") == "/write_bytes":
                    response = _write_bytes(payload, config)
                    status = 200
                elif self.path.rstrip("/") == "/read_bytes":
                    response = _read_bytes(payload, config)
                    status = 200
                elif self.path.rstrip("/") == "/sha256":
                    response = _file_sha256(payload, config)
                    status = 200
                elif self.path.rstrip("/") == "/list_dir":
                    response = _list_dir(payload, config)
                    status = 200
                elif self.path.rstrip("/") == "/search_files":
                    response = _search_files(payload, config)
                    status = 200
                elif self.path.rstrip("/") == "/mkdir":
                    _make_dir(payload, config)
                    status = 200
                    response = {"status": "ok"}
                elif self.path.rstrip("/") == "/remove":
                    _remove_path(payload, config)
                    status = 200
                    response = {"status": "ok"}
                elif self.path.rstrip("/") == "/rename":
                    _rename_path(payload, config)
                    status = 200
                    response = {"status": "ok"}
                elif self.path.rstrip("/") == "/copy":
                    response = _copy_entries(payload, config)
                    status = 200
                elif self.path.rstrip("/") == "/create-file":
                    response = _create_file(payload, config)
                    status = 200
                elif self.path.rstrip("/") == "/stat":
                    response = _file_stat(payload, config)
                    status = 200
                elif self.path.rstrip("/") == "/disk_usage":
                    response = _disk_usage(payload, config)
                    status = 200
                elif self.path.rstrip("/") == "/extract":
                    response = _extract_archive(payload, config)
                    status = 200
                elif self.path.rstrip("/") == "/archive":
                    response = _create_archive(payload, config)
                    status = 200
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
    if path == "/write_bytes":
        data = "" if payload.get("data_b64") is None else str(payload.get("data_b64"))
        return {
            "path": payload.get("path"),
            "owner": payload.get("owner"),
            "offset": payload.get("offset"),
            "data_bytes": (len(data) * 3) // 4,
        }
    if path in {
        "/read_bytes",
        "/sha256",
        "/list_dir",
        "/stat",
        "/disk_usage",
        "/remove",
        "/extract",
    }:
        return {
            "path": payload.get("path"),
            "dest_dir": payload.get("dest_dir"),
            "owner": payload.get("owner"),
        }
    if path == "/mkdir":
        return {"path": payload.get("path"), "owner": payload.get("owner")}
    if path == "/rename":
        return {
            "path": payload.get("path"),
            "new_path": payload.get("new_path"),
            "owner": payload.get("owner"),
        }
    if path == "/copy":
        return {
            "paths": payload.get("paths"),
            "dest_dir": payload.get("dest_dir"),
            "owner": payload.get("owner"),
        }
    if path == "/create-file":
        return {
            "dir": payload.get("dir"),
            "name": payload.get("name"),
            "owner": payload.get("owner"),
        }
    if path == "/archive":
        return {
            "paths": payload.get("paths"),
            "dest_dir": payload.get("dest_dir"),
            "owner": payload.get("owner"),
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
    user = payload.get("user")
    command = list(argv)
    username: str | None = None
    if user is not None:
        username = _safe_user(str(user))
        pwd.getpwnam(username)
        command = ["gosu", username, *command]
    _validate_command(argv, config, user=username)

    cwd = payload.get("cwd")
    safe_cwd = None
    if cwd is not None:
        safe_cwd = _authorize_path(str(cwd), config, user=username)

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


def _validate_command(
    argv: list[str],
    config: GatewayConfig,
    *,
    user: str | None,
) -> None:
    exact_commands = {
        "date": ["date", "-Is"],
        "env": ["env"],
        "pwd": ["pwd"],
        "python": ["python", "-V"],
        "which": ["which", "python"],
        "whoami": ["whoami"],
        "sinfo": ["sinfo", "-h", "-o", "%P|%c|%m|%G|%T"],
    }
    expected = exact_commands.get(argv[0])
    if expected is not None and argv != expected:
        raise GatewayError(f"command arguments not allowed: {argv[0]}")
    if argv[0] == "test":
        if len(argv) != 3 or argv[1] not in {"-e", "-d", "-r", "-x", "-w"}:
            raise GatewayError("command arguments not allowed: test")
        _authorize_path(argv[2], config, user=user)
    if argv[0] == "sstat":
        expected_fields = (
            "JobID,NTasks,AllocTRES,AveCPU,MaxRSS,"
            "TRESUsageInTot,TRESUsageOutTot"
        )
        if (
            len(argv) != 7
            or argv[1:4] != ["-nP", "--allsteps", "-j"]
            or argv[5:] != ["-o", expected_fields]
        ):
            raise GatewayError("command arguments not allowed: sstat")
        job_ids = argv[4].split(",")
        if not 1 <= len(job_ids) <= 1000 or any(
            re.fullmatch(r"[A-Za-z0-9_.+-]+", job_id) is None
            for job_id in job_ids
        ):
            raise GatewayError("command arguments not allowed: sstat")
    if argv[0] == "scontrol":
        show_config = argv == ["scontrol", "show", "config"]
        show_job = (
            len(argv) == 5
            and argv[1:4] == ["-o", "show", "job"]
            and re.fullmatch(r"[A-Za-z0-9_.+-]+", argv[4]) is not None
        )
        if not show_config and not show_job:
            raise GatewayError("command arguments not allowed: scontrol")


def _write_text(payload: dict[str, Any], config: GatewayConfig) -> None:
    owner = _safe_user(str(payload.get("owner", "")))
    path = _authorize_path(str(payload.get("path", "")), config, user=owner)
    content = str(payload.get("content", ""))
    user_info = pwd.getpwnam(owner)
    target = Path(path)
    if not target.parent.exists():
        raise GatewayError(f"parent directory does not exist: {target.parent}")
    target.write_text(content, encoding="utf-8")
    os.chown(target, user_info.pw_uid, user_info.pw_gid)


def _chown_to_owner(path: str, owner: str) -> None:
    user_info = pwd.getpwnam(owner)
    os.chown(path, user_info.pw_uid, user_info.pw_gid)


def _write_bytes(payload: dict[str, Any], config: GatewayConfig) -> dict[str, Any]:
    owner = _safe_user(str(payload.get("owner", "")))
    path = _authorize_path(str(payload.get("path", "")), config, user=owner)
    data_field = payload.get("data_b64")
    if not isinstance(data_field, str):
        raise GatewayError("data_b64 must be a base64 string")
    try:
        data = base64.b64decode(data_field, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise GatewayError(f"invalid base64 payload: {exc}") from exc
    raw_offset = payload.get("offset", -1)
    try:
        offset = int(raw_offset)
    except (TypeError, ValueError) as exc:
        raise GatewayError("offset must be an integer") from exc
    target = Path(path)
    if not target.parent.exists():
        raise GatewayError(f"parent directory does not exist: {target.parent}")
    if offset < 0:
        mode = "ab"
    elif offset == 0:
        mode = "wb"
    else:
        if not target.exists():
            raise GatewayError("cannot write at offset before file exists")
        current = target.stat().st_size
        if offset != current:
            raise GatewayError(
                f"write offset {offset} does not match file size {current}"
            )
        mode = "r+b"
    with open(target, mode) as handle:
        if offset > 0:
            handle.seek(offset)
        handle.write(data)
    _chown_to_owner(path, owner)
    return {"status": "ok", "size": target.stat().st_size}


def _read_bytes(payload: dict[str, Any], config: GatewayConfig) -> dict[str, Any]:
    owner = _safe_user(str(payload.get("owner", "")))
    path = _authorize_path(str(payload.get("path", "")), config, user=owner)
    target = Path(path)
    if not target.is_file():
        raise GatewayError(f"not a regular file: {path}", status=404)
    try:
        offset = int(payload.get("offset", 0))
        length = int(payload.get("length", 0))
    except (TypeError, ValueError) as exc:
        raise GatewayError("offset and length must be integers") from exc
    if offset < 0 or length <= 0:
        raise GatewayError("offset must be >= 0 and length must be positive")
    size = target.stat().st_size
    with open(target, "rb") as handle:
        handle.seek(offset)
        data = handle.read(length)
    return {
        "data_b64": base64.b64encode(data).decode("ascii"),
        "size": size,
        "offset": offset,
        "length": len(data),
    }


def _file_sha256(payload: dict[str, Any], config: GatewayConfig) -> dict[str, Any]:
    owner = _safe_user(str(payload.get("owner", "")))
    path = _authorize_path(str(payload.get("path", "")), config, user=owner)
    target = Path(path)
    if not target.is_file():
        raise GatewayError(f"not a regular file: {path}", status=404)
    digest = hashlib.sha256()
    with open(target, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {"sha256": digest.hexdigest(), "size": target.stat().st_size}


def _list_dir(payload: dict[str, Any], config: GatewayConfig) -> dict[str, Any]:
    owner = _safe_user(str(payload.get("owner", "")))
    path = _authorize_path(str(payload.get("path", "")), config, user=owner)
    target = Path(path)
    if not target.is_dir():
        raise GatewayError(f"not a directory: {path}", status=404)
    entries: list[dict[str, Any]] = []
    for entry in sorted(target.iterdir(), key=lambda item: item.name):
        try:
            info = entry.lstat()
        except OSError:
            continue
        if entry.is_symlink():
            kind = "symlink"
        elif entry.is_dir():
            kind = "dir"
        elif entry.is_file():
            kind = "file"
        else:
            kind = "other"
        entries.append(
            {
                "name": entry.name,
                "type": kind,
                "size": info.st_size,
                "mtime": int(info.st_mtime),
            }
        )
    return {"path": path, "entries": entries}


def _search_files(payload: dict[str, Any], config: GatewayConfig) -> dict[str, Any]:
    owner = _safe_user(str(payload.get("owner", "")))
    root = _authorize_path(str(payload.get("root", "")), config, user=owner)
    target = Path(root)
    if not target.is_dir():
        raise GatewayError(f"not a directory: {root}", status=404)

    query = str(payload.get("q", "")).strip().casefold()
    kind = str(payload.get("kind", "all"))
    if kind not in {"file", "directory", "all"}:
        raise GatewayError("kind must be file, directory, or all")
    limit = _bounded_integer(payload, "limit", default=100, minimum=1, maximum=100)
    scan_limit = _bounded_integer(
        payload, "scan_limit", default=10_000, minimum=1, maximum=100_000
    )
    time_limit_ms = _bounded_integer(
        payload, "time_limit_ms", default=750, minimum=1, maximum=10_000
    )
    size_min = _optional_nonnegative_integer(payload, "size_min")
    size_max = _optional_nonnegative_integer(payload, "size_max")
    mtime_from = _optional_nonnegative_integer(payload, "mtime_from")
    mtime_to = _optional_nonnegative_integer(payload, "mtime_to")
    if size_min is not None and size_max is not None and size_min > size_max:
        raise GatewayError("size_min cannot exceed size_max")
    if mtime_from is not None and mtime_to is not None and mtime_from > mtime_to:
        raise GatewayError("mtime_from cannot exceed mtime_to")

    binding = {
        "owner": owner,
        "root": root,
        "q": query,
        "kind": kind,
        "size_min": size_min,
        "size_max": size_max,
        "mtime_from": mtime_from,
        "mtime_to": mtime_to,
    }
    raw_cursor = payload.get("cursor")
    if raw_cursor is None or raw_cursor == "":
        stack: list[dict[str, Any]] = [{"relative_dir": "", "index": 0}]
    elif isinstance(raw_cursor, str):
        cursor_payload = _decode_search_cursor(raw_cursor, config._cursor_key)
        if cursor_payload.get("binding") != binding:
            raise GatewayError("search cursor does not match request")
        raw_stack = cursor_payload.get("stack")
        if not isinstance(raw_stack, list):
            raise GatewayError("invalid search cursor")
        stack = []
        for frame in raw_stack:
            if not isinstance(frame, dict):
                raise GatewayError("invalid search cursor")
            relative_dir = frame.get("relative_dir")
            index = frame.get("index")
            if (
                not isinstance(relative_dir, str)
                or relative_dir.startswith("/")
                or ".." in Path(relative_dir).parts
                or not isinstance(index, int)
                or index < 0
            ):
                raise GatewayError("invalid search cursor")
            stack.append({"relative_dir": relative_dir, "index": index})
    else:
        raise GatewayError("cursor must be a string")

    started = time.monotonic()
    scanned = 0
    items: list[dict[str, Any]] = []
    warnings: list[str] = []
    while stack and len(items) < limit:
        if (
            scanned >= scan_limit
            or (time.monotonic() - started) * 1000 >= time_limit_ms
        ):
            break
        frame = stack[-1]
        relative_dir = str(frame["relative_dir"])
        directory = target / relative_dir if relative_dir else target
        try:
            with os.scandir(directory) as handle:
                entries = sorted(handle, key=lambda entry: entry.name)
        except OSError:
            warnings.append(
                "unreadable directory: " + (relative_dir if relative_dir else ".")
            )
            stack.pop()
            continue
        index = int(frame["index"])
        if index >= len(entries):
            stack.pop()
            continue
        entry = entries[index]
        frame["index"] = index + 1
        scanned += 1
        try:
            info = entry.stat(follow_symlinks=False)
            is_directory = entry.is_dir(follow_symlinks=False)
            is_file = entry.is_file(follow_symlinks=False)
        except OSError:
            continue
        if not is_directory and not is_file:
            continue
        relative_path = (
            posixpath.join(relative_dir, entry.name) if relative_dir else entry.name
        )
        entry_kind = "directory" if is_directory else "file"
        if is_directory:
            stack.append({"relative_dir": relative_path, "index": 0})
        if query not in entry.name.casefold() and query not in relative_path.casefold():
            continue
        if kind != "all" and kind != entry_kind:
            continue
        if size_min is not None and info.st_size < size_min:
            continue
        if size_max is not None and info.st_size > size_max:
            continue
        mtime = int(info.st_mtime)
        if mtime_from is not None and mtime < mtime_from:
            continue
        if mtime_to is not None and mtime > mtime_to:
            continue
        items.append(
            {
                "path": str(target / relative_path),
                "relative_path": relative_path,
                "type": entry_kind,
                "size": info.st_size,
                "mtime": mtime,
            }
        )

    incomplete = bool(stack)
    next_cursor = (
        _encode_search_cursor({"binding": binding, "stack": stack}, config._cursor_key)
        if incomplete
        else None
    )
    return {
        "root": root,
        "items": items,
        "incomplete": incomplete,
        "next_cursor": next_cursor,
        "warnings": warnings,
    }


def _bounded_integer(
    payload: dict[str, Any],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = payload.get(name, default)
    if isinstance(raw, bool):
        raise GatewayError(f"{name} must be an integer")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise GatewayError(f"{name} must be an integer") from exc
    if value < minimum or value > maximum:
        raise GatewayError(f"{name} must be between {minimum} and {maximum}")
    return value


def _optional_nonnegative_integer(payload: dict[str, Any], name: str) -> int | None:
    raw = payload.get(name)
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise GatewayError(f"{name} must be a non-negative integer")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise GatewayError(f"{name} must be a non-negative integer") from exc
    if value < 0:
        raise GatewayError(f"{name} must be a non-negative integer")
    return value


def _encode_search_cursor(payload: dict[str, Any], key: bytes) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).rstrip(b"=")
    signature = hmac.new(key, body, hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return body.decode("ascii") + "." + encoded_signature


def _decode_search_cursor(cursor: str, key: bytes) -> dict[str, Any]:
    try:
        body_text, signature_text = cursor.split(".", 1)
        body = body_text.encode("ascii")
        signature = base64.urlsafe_b64decode(
            signature_text + "=" * (-len(signature_text) % 4)
        )
        expected = hmac.new(key, body, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature mismatch")
        raw = base64.urlsafe_b64decode(body + b"=" * (-len(body) % 4))
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise GatewayError("invalid search cursor") from exc
    if not isinstance(decoded, dict):
        raise GatewayError("invalid search cursor")
    return decoded


def _make_dir(payload: dict[str, Any], config: GatewayConfig) -> None:
    owner = _safe_user(str(payload.get("owner", "")))
    path = _authorize_path(str(payload.get("path", "")), config, user=owner)
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    _chown_to_owner(path, owner)


def _remove_path(payload: dict[str, Any], config: GatewayConfig) -> None:
    owner = _safe_user(str(payload.get("owner", "")))
    path = _authorize_path(str(payload.get("path", "")), config, user=owner)
    target = Path(path)
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    elif target.exists() or target.is_symlink():
        target.unlink()
    else:
        raise GatewayError(f"path does not exist: {path}", status=404)


def _rename_path(payload: dict[str, Any], config: GatewayConfig) -> None:
    owner = _safe_user(str(payload.get("owner", "")))
    path = _authorize_path(str(payload.get("path", "")), config, user=owner)
    new_path = _authorize_path(str(payload.get("new_path", "")), config, user=owner)
    overwrite = bool(payload.get("overwrite", False))
    source = Path(path)
    destination = Path(new_path)
    if not source.exists() and not source.is_symlink():
        raise GatewayError(f"path does not exist: {path}", status=404)
    if destination.exists():
        if not overwrite:
            raise GatewayError(f"target already exists: {new_path}", status=409)
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)
    _chown_to_owner(new_path, owner)


def _copy_entries(payload: dict[str, Any], config: GatewayConfig) -> dict[str, Any]:
    """Copy files/directories into dest_dir (overwrite, same as move)."""
    owner = _safe_user(str(payload.get("owner", "")))
    dest_dir = _authorize_path(str(payload.get("dest_dir", "")), config, user=owner)
    raw_paths = payload.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise GatewayError("paths must be a non-empty list")
    destination = Path(dest_dir)
    if not destination.is_dir():
        raise GatewayError(f"destination is not a directory: {dest_dir}", status=404)
    dest_resolved = destination.resolve()
    copied: list[str] = []
    for item in raw_paths:
        source = Path(_authorize_path(str(item), config, user=owner))
        if not source.exists() and not source.is_symlink():
            raise GatewayError(f"path does not exist: {item}", status=404)
        if source.is_dir() and not source.is_symlink():
            source_resolved = source.resolve()
            if dest_resolved == source_resolved or str(dest_resolved).startswith(
                f"{source_resolved}/"
            ):
                raise GatewayError(f"cannot copy a directory into itself: {item}")
        target = destination / source.name
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        if source.is_dir() and not source.is_symlink():
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target, follow_symlinks=False)
        _chown_to_owner(str(target), owner)
        copied.append(str(target))
    return {"status": "ok", "copied": copied}


def _create_file(payload: dict[str, Any], config: GatewayConfig) -> dict[str, Any]:
    """Create an empty file; 409 when the name is already taken."""
    owner = _safe_user(str(payload.get("owner", "")))
    dir_path = _authorize_path(str(payload.get("dir", "")), config, user=owner)
    name = str(payload.get("name", ""))
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise GatewayError(f"unsafe file name: {name}")
    parent = Path(dir_path)
    if not parent.is_dir():
        raise GatewayError(f"directory does not exist: {dir_path}", status=404)
    target = parent / name
    try:
        with target.open("xb"):
            pass
    except FileExistsError as exc:
        raise GatewayError(f"file already exists: {target}", status=409) from exc
    _chown_to_owner(str(target), owner)
    return {"status": "ok", "path": str(target)}


def _file_stat(payload: dict[str, Any], config: GatewayConfig) -> dict[str, Any]:
    owner = _safe_user(str(payload.get("owner", "")))
    path = _authorize_path(str(payload.get("path", "")), config, user=owner)
    target = Path(path)
    if not target.exists():
        raise GatewayError(f"path does not exist: {path}", status=404)
    info = target.lstat()
    if target.is_symlink():
        kind = "symlink"
    elif target.is_dir():
        kind = "dir"
    elif target.is_file():
        kind = "file"
    else:
        kind = "other"
    return {
        "path": path,
        "type": kind,
        "size": info.st_size,
        "mtime": int(info.st_mtime),
    }


def _disk_usage(payload: dict[str, Any], config: GatewayConfig) -> dict[str, Any]:
    owner = _safe_user(str(payload.get("owner", "")))
    path = _authorize_path(str(payload.get("path", "")), config, user=owner)
    target = Path(path)
    if not target.exists():
        raise GatewayError(f"path does not exist: {path}", status=404)
    if target.is_dir() and not target.is_symlink():
        used = 0
        stack = [target]
        while stack:
            current = stack.pop()
            try:
                handle = os.scandir(current)
            except OSError:
                continue
            with handle:
                for entry in handle:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            continue
                        used += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
    else:
        used = target.lstat().st_size
    try:
        total: int | None = shutil.disk_usage(target).total
    except OSError:
        total = None
    return {"path": path, "used_bytes": used, "total_bytes": total}


_TAR_SUFFIXES = {".tar", ".gz", ".tgz", ".bz2", ".xz"}
_SUPPORTED_ARCHIVE_HINT = (
    "supported formats: .tar, .tar.gz, .tgz, .tar.bz2, .tar.xz, .zip, .rar"
)


def _require_member_within(
    member_name: str, destination: Path, dest_resolved: Path
) -> None:
    member_dest = (destination / member_name).resolve()
    if member_dest != dest_resolved and not str(member_dest).startswith(
        f"{dest_resolved}/"
    ):
        raise GatewayError(f"archive member escapes destination: {member_name}")


def _extract_tar_members(archive: Path, destination: Path, dest_resolved: Path) -> int:
    count = 0
    with tarfile.open(archive, "r:*") as tar:
        for member in tar.getmembers():
            _require_member_within(member.name, destination, dest_resolved)
            if member.issym() or member.islnk():
                raise GatewayError(
                    f"archive link members are not permitted: {member.name}"
                )
            count += 1
        tar.extractall(destination)
    return count


def _extract_zip_members(archive: Path, destination: Path, dest_resolved: Path) -> int:
    count = 0
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            _require_member_within(info.filename, destination, dest_resolved)
            unix_mode = info.external_attr >> 16
            if unix_mode and stat.S_ISLNK(unix_mode):
                raise GatewayError(
                    f"archive link members are not permitted: {info.filename}"
                )
            count += 1
        zf.extractall(destination)
    return count


def _extract_rar_members(archive: Path, destination: Path) -> int:
    # rar is proprietary: shell out to unar (installed in the slurm image).
    # Fixed argument list, no shell; unar itself refuses ".." members.
    try:
        proc = subprocess.run(
            ["unar", "-f", "-o", str(destination), str(archive)],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GatewayError("unar is not available on this host", status=500) from exc
    except subprocess.TimeoutExpired as exc:
        raise GatewayError("rar extraction timed out") from exc
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = lines[-1] if lines else "unknown error"
        raise GatewayError(f"rar extraction failed: {detail}")
    # unar has no machine-readable member count; report what now exists under
    # the destination (informational only).
    return sum(1 for _ in destination.rglob("*"))


def _extract_archive(payload: dict[str, Any], config: GatewayConfig) -> dict[str, Any]:
    owner = _safe_user(str(payload.get("owner", "")))
    archive_path = _authorize_path(str(payload.get("path", "")), config, user=owner)
    dest_dir = _authorize_path(str(payload.get("dest_dir", "")), config, user=owner)
    archive = Path(archive_path)
    if not archive.is_file():
        raise GatewayError(f"archive not found: {archive_path}", status=404)
    destination = Path(dest_dir)
    destination.mkdir(parents=True, exist_ok=True)
    dest_resolved = destination.resolve()
    suffix = archive.suffix.lower()
    if suffix in _TAR_SUFFIXES:
        count = _extract_tar_members(archive, destination, dest_resolved)
    elif suffix == ".zip":
        count = _extract_zip_members(archive, destination, dest_resolved)
    elif suffix == ".rar":
        count = _extract_rar_members(archive, destination)
    else:
        raise GatewayError(
            f"unsupported archive format: {archive.name} ({_SUPPORTED_ARCHIVE_HINT})"
        )
    _chown_to_owner(dest_dir, owner)
    return {"status": "ok", "members": count, "dest_dir": dest_dir}


def _create_archive(payload: dict[str, Any], config: GatewayConfig) -> dict[str, Any]:
    owner = _safe_user(str(payload.get("owner", "")))
    dest_dir = _authorize_path(str(payload.get("dest_dir", "")), config, user=owner)
    archive_name = str(payload.get("archive_name", "archive.tar.gz"))
    if "/" in archive_name or "\\" in archive_name or ".." in archive_name:
        raise GatewayError(f"unsafe archive name: {archive_name}")
    raw_paths = payload.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise GatewayError("paths must be a non-empty list")
    sources: list[str] = []
    for item in raw_paths:
        sources.append(_authorize_path(str(item), config, user=owner))
    destination = Path(dest_dir)
    destination.mkdir(parents=True, exist_ok=True)
    archive_path = destination / archive_name
    count = 0
    with tarfile.open(archive_path, "w:gz") as tar:
        for source in sources:
            source_path = Path(source)
            if not source_path.exists():
                raise GatewayError(f"path does not exist: {source}", status=404)
            tar.add(source, arcname=source_path.name)
            count += 1
    _chown_to_owner(str(archive_path), owner)
    return {
        "status": "ok",
        "path": str(archive_path),
        "size": archive_path.stat().st_size,
        "members": count,
    }


def _authorize_path(path: str, config: GatewayConfig, *, user: str | None = None) -> str:
    resolved = _realpath(path)
    for root in config.allowed_roots:
        resolved_root = _realpath(_root_for_user(root, user)).rstrip("/")
        if resolved == resolved_root or resolved.startswith(f"{resolved_root}/"):
            return resolved
    raise GatewayError(f"path outside allowed roots: {path}")


def _root_for_user(root: str, user: str | None) -> str:
    if "{user}" not in root:
        return root
    if user is None:
        raise GatewayError("owner-scoped root requires a user")
    return root.replace("{user}", user)


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

    raw_allowed_roots = os.environ.get("PILOT107_GATEWAY_ALLOWED_ROOTS", "")
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
