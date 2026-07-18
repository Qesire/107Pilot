"""Small stdlib web server and same-origin API proxy for the Web MVP."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

_SAFE_USER = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_STATIC_ROOT = Path(__file__).resolve().parent / "static"
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


class WebIdentityMode(StrEnum):
    DEMO = "demo"
    FIXED_USER = "fixed_user"


@dataclass(frozen=True)
class WebConfig:
    api_base_url: str
    demo_user: str = "alice"
    fixed_user: str | None = None
    trusted_user_header: str = "X-Pilot107-User"
    identity_mode: WebIdentityMode = WebIdentityMode.DEMO
    terminal_deep_link: str | None = None

    def __post_init__(self) -> None:
        if not is_safe_demo_user(self.demo_user):
            raise ValueError("PILOT107_WEB_DEMO_USER must be a safe username")
        if self.identity_mode == WebIdentityMode.FIXED_USER and (
            self.fixed_user is None or not is_safe_demo_user(self.fixed_user)
        ):
            raise ValueError(
                "PILOT107_WEB_FIXED_USER is required and must be a safe username"
            )
        if self.terminal_deep_link is not None and not is_safe_terminal_deep_link(
            self.terminal_deep_link
        ):
            raise ValueError("PILOT107_WEB_TERMINAL_DEEP_LINK must be an absolute HTTP(S) URL")


def config_from_env(env: Mapping[str, str] | None = None) -> WebConfig:
    values = os.environ if env is None else env
    return WebConfig(
        api_base_url=values.get("PILOT107_WEB_API_BASE_URL", "http://127.0.0.1:8070").rstrip("/"),
        demo_user=values.get("PILOT107_WEB_DEMO_USER", "alice"),
        fixed_user=values.get("PILOT107_WEB_FIXED_USER") or None,
        trusted_user_header=values.get("PILOT107_TRUSTED_USER_HEADER", "X-Pilot107-User"),
        identity_mode=WebIdentityMode(
            values.get("PILOT107_WEB_IDENTITY_MODE", WebIdentityMode.DEMO.value)
        ),
        terminal_deep_link=values.get("PILOT107_WEB_TERMINAL_DEEP_LINK") or None,
    )


def make_handler(config: WebConfig) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "pilot107-web/0.1"

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self._send_bytes(200, b'{"status":"ok"}\n', "application/json; charset=utf-8")
                return
            if urlparse(self.path).path == "/api/v1/web/session":
                self._serve_web_session()
                return
            if self.path.startswith("/api/"):
                self._proxy()
                return
            self._serve_static()

        def do_HEAD(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self._send_bytes(
                    200,
                    b"",
                    "application/json; charset=utf-8",
                    send_body=False,
                )
                return
            if self.path.startswith("/api/"):
                self._send_bytes(405, b"", "text/plain; charset=utf-8", send_body=False)
                return
            self._serve_static(send_body=False)

        def do_POST(self) -> None:  # noqa: N802
            if self.path.startswith("/api/"):
                self._proxy()
                return
            self._send_bytes(404, b"not found\n", "text/plain; charset=utf-8")

        def do_PATCH(self) -> None:  # noqa: N802
            if self.path.startswith("/api/"):
                self._proxy()
                return
            self._send_bytes(404, b"not found\n", "text/plain; charset=utf-8")

        def log_message(self, format: str, *args: object) -> None:
            return

        def _serve_web_session(self) -> None:
            user = resolve_proxy_user(config, dict(self.headers.items()))
            if user is None:
                self._send_bytes(
                    403,
                    b'{"error":{"code":"AUTH.FORBIDDEN"}}\n',
                    "application/json; charset=utf-8",
                )
                return
            payload = json.dumps(
                {
                    "identity_mode": config.identity_mode.value,
                    "user": user,
                    "switchable": config.identity_mode == WebIdentityMode.DEMO,
                    "terminal_deep_link": config.terminal_deep_link,
                },
                ensure_ascii=False,
            ).encode("utf-8") + b"\n"
            self._send_bytes(200, payload, "application/json; charset=utf-8")

        def _serve_static(self, *, send_body: bool = True) -> None:
            parsed = urlparse(self.path)
            static_path = resolve_static_request(parsed.path)
            if static_path is None and _request_escapes_static_root(parsed.path):
                self._send_bytes(
                    403, b"forbidden\n", "text/plain; charset=utf-8", send_body=send_body
                )
                return
            if static_path is None:
                self._send_bytes(
                    404, b"not found\n", "text/plain; charset=utf-8", send_body=send_body
                )
                return
            content_type = _CONTENT_TYPES.get(static_path.suffix, "application/octet-stream")
            cache_control = "no-store" if static_path.name == "index.html" else "max-age=60"
            self._send_bytes(
                200,
                static_path.read_bytes(),
                content_type,
                cache_control=cache_control,
                send_body=send_body,
            )

        def _proxy(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length > 0 else None
            user = resolve_proxy_user(config, dict(self.headers.items()))
            if user is None:
                self._send_bytes(
                    403,
                    b'{"error":{"code":"AUTH.FORBIDDEN"}}\n',
                    "application/json; charset=utf-8",
                )
                return
            headers = {
                "Accept": self.headers.get("Accept", "application/json"),
                config.trusted_user_header: user,
            }
            content_type = self.headers.get("Content-Type")
            if content_type:
                headers["Content-Type"] = content_type
            request = urllib.request.Request(
                url=f"{config.api_base_url}{self.path}",
                data=body,
                headers=headers,
                method=self.command,
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    response_body = response.read()
                    self._send_bytes(
                        response.status,
                        response_body,
                        response.headers.get("Content-Type", "application/json; charset=utf-8"),
                    )
            except urllib.error.HTTPError as exc:
                self._send_bytes(
                    exc.code,
                    exc.read(),
                    exc.headers.get("Content-Type", "application/json; charset=utf-8"),
                )
            except OSError as exc:
                payload = json.dumps(
                    {
                        "error": {
                            "code": "WEB.UPSTREAM_UNAVAILABLE",
                            "message": str(exc),
                        }
                    },
                    ensure_ascii=False,
                ).encode("utf-8") + b"\n"
                self._send_bytes(502, payload, "application/json; charset=utf-8")

        def _send_bytes(
            self,
            status: int,
            body: bytes,
            content_type: str,
            *,
            cache_control: str = "no-store",
            send_body: bool = True,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache_control)
            self.end_headers()
            if send_body:
                self.wfile.write(body)

    return Handler


def is_safe_terminal_deep_link(value: str) -> bool:
    if any(ord(character) < 32 for character in value):
        return False
    parsed = urlparse(value)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


def run_web_server(*, config: WebConfig, host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(config))
    try:
        server.serve_forever()
    finally:
        server.server_close()


def is_safe_demo_user(value: str) -> bool:
    return bool(_SAFE_USER.fullmatch(value))


def resolve_proxy_user(config: WebConfig, headers: Mapping[str, str]) -> str | None:
    """Resolve the BFF identity without widening the configured trust boundary."""

    if config.identity_mode == WebIdentityMode.FIXED_USER:
        assert config.fixed_user is not None
        return config.fixed_user
    requested = next(
        (
            str(value)
            for key, value in headers.items()
            if key.lower() == config.trusted_user_header.lower()
        ),
        config.demo_user,
    )
    return requested if is_safe_demo_user(requested) else None


def resolve_static_request(request_path: str, *, root: Path = _STATIC_ROOT) -> Path | None:
    """Resolve assets exactly and product routes to the built SPA entrypoint."""

    normalized_root = root.resolve()
    selected = "/index.html" if request_path in {"", "/"} else request_path
    candidate = (normalized_root / selected.lstrip("/")).resolve()
    try:
        candidate.relative_to(normalized_root)
    except ValueError:
        return None
    if candidate.is_file():
        return candidate
    if Path(selected).suffix:
        return None
    entrypoint = normalized_root / "index.html"
    return entrypoint if entrypoint.is_file() else None


def _request_escapes_static_root(request_path: str, *, root: Path = _STATIC_ROOT) -> bool:
    candidate = (root.resolve() / request_path.lstrip("/")).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the 107Pilot Web MVP.")
    parser.add_argument("--host", default=os.environ.get("PILOT107_WEB_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("PILOT107_WEB_PORT", "3000"))
    )
    parser.add_argument("--api-base-url", default=None)
    parser.add_argument("--demo-user", default=None)
    args = parser.parse_args()

    env_config = config_from_env(os.environ)
    run_web_server(
        config=WebConfig(
            api_base_url=(args.api_base_url or env_config.api_base_url).rstrip("/"),
            demo_user=args.demo_user or env_config.demo_user,
            fixed_user=env_config.fixed_user,
            trusted_user_header=env_config.trusted_user_header,
            identity_mode=env_config.identity_mode,
        ),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
