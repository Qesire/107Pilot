"""Small stdlib web server and same-origin API proxy for the Web MVP."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.message import Message
from enum import StrEnum
from http.client import HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from pilot107.api.security import FixedWindowRateLimiter
from pilot107.core.proxy_auth import load_proxy_hmac_secret, signed_proxy_headers

_SAFE_USER = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_STATIC_ROOT = Path(__file__).resolve().parent / "static"
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}
# tus resumable-upload request headers forwarded verbatim to the API.
_TUS_PROXY_REQUEST_HEADERS = (
    "Tus-Resumable",
    "Upload-Offset",
    "Upload-Length",
    "Upload-Metadata",
    "Upload-Concat",
)
# tus response headers relayed back to the browser.
_TUS_PROXY_RESPONSE_HEADERS = (
    "Upload-Offset",
    "Upload-Length",
    "Location",
    "Tus-Resumable",
    "Tus-Version",
    "Tus-Extension",
    "Tus-Max-Size",
)


def _tus_data_plane(path: str) -> bool:
    """Return True for high-frequency tus data-plane requests.

    PATCH/HEAD ``/api/v1/files/tus/{id}`` fire once per chunk (a multi-GiB
    upload issues hundreds of them) and are already bounded by owner quota and
    auth, so they are exempt from the per-IP API rate limit. Control-plane tus
    requests (create/concat/complete/DELETE/OPTIONS/GET) stay rate-limited.
    """
    parsed_path = urlparse(path).path
    prefix = "/api/v1/files/tus/"
    return parsed_path.startswith(prefix) and len(parsed_path) > len(prefix)


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
    proxy_hmac_secret: bytes | None = field(default=None, repr=False)
    public_origin: str | None = None
    enable_hsts: bool = False
    max_request_body_bytes: int = 16 * 1024 * 1024
    max_response_body_bytes: int = 8 * 1024 * 1024
    # Upstream (API) socket timeout. Large-file upload ``complete`` performs a
    # whole-file sha256 + cluster write before responding, which can take
    # minutes for multi-GiB uploads, so this must be generous.
    upstream_timeout_seconds: int = 600
    rate_limit_requests: int = 300
    rate_limit_window_seconds: int = 60

    def __post_init__(self) -> None:
        if not is_safe_demo_user(self.demo_user):
            raise ValueError("PILOT107_WEB_DEMO_USER must be a safe username")
        if self.identity_mode == WebIdentityMode.FIXED_USER and (
            self.fixed_user is None or not is_safe_demo_user(self.fixed_user)
        ):
            raise ValueError("PILOT107_WEB_FIXED_USER is required and must be a safe username")
        if self.terminal_deep_link is not None and not is_safe_terminal_deep_link(
            self.terminal_deep_link
        ):
            raise ValueError("PILOT107_WEB_TERMINAL_DEEP_LINK must be an absolute HTTP(S) URL")
        if self.public_origin is not None and normalize_origin(self.public_origin) is None:
            raise ValueError("PILOT107_WEB_PUBLIC_ORIGIN must be an HTTP(S) origin")
        if (
            min(
                self.max_request_body_bytes,
                self.max_response_body_bytes,
                self.upstream_timeout_seconds,
                self.rate_limit_requests,
                self.rate_limit_window_seconds,
            )
            <= 0
        ):
            raise ValueError("Web size and rate limits must be positive")


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
        proxy_hmac_secret=load_proxy_hmac_secret(
            secret=values.get("PILOT107_PROXY_HMAC_SECRET"),
            secret_file=values.get("PILOT107_PROXY_HMAC_SECRET_FILE"),
        ),
        public_origin=values.get("PILOT107_WEB_PUBLIC_ORIGIN") or None,
        enable_hsts=_env_bool(values.get("PILOT107_WEB_ENABLE_HSTS"), False),
        max_request_body_bytes=int(
            values.get("PILOT107_WEB_MAX_REQUEST_BODY_BYTES", str(16 * 1024 * 1024))
        ),
        max_response_body_bytes=int(
            values.get("PILOT107_WEB_MAX_RESPONSE_BODY_BYTES", str(8 * 1024 * 1024))
        ),
        upstream_timeout_seconds=int(values.get("PILOT107_WEB_UPSTREAM_TIMEOUT_SECONDS", "600")),
        rate_limit_requests=int(values.get("PILOT107_WEB_RATE_LIMIT_REQUESTS", "300")),
        rate_limit_window_seconds=int(values.get("PILOT107_WEB_RATE_LIMIT_WINDOW_SECONDS", "60")),
    )


def make_handler(config: WebConfig) -> type[BaseHTTPRequestHandler]:
    rate_limiter = FixedWindowRateLimiter(
        limit=config.rate_limit_requests,
        window_seconds=config.rate_limit_window_seconds,
    )

    class Handler(BaseHTTPRequestHandler):
        server_version = "pilot107-web/0.1"

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self._send_bytes(200, b'{"status":"ok"}\n', "application/json; charset=utf-8")
                return
            if urlparse(self.path).path == "/api/v1/web/session":
                if not self._allow_api_request():
                    return
                self._serve_web_session()
                return
            if self.path.startswith("/api/"):
                if not self._allow_api_request():
                    return
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
                # tus resume probes (HEAD /api/v1/files/tus/{id}) must reach the
                # API; data-plane probes are exempt from the per-IP rate limit.
                if not _tus_data_plane(self.path) and not self._allow_api_request():
                    return
                self._proxy()
                return
            self._serve_static(send_body=False)

        def do_POST(self) -> None:  # noqa: N802
            if self.path.startswith("/api/"):
                if not self._allow_api_request() or not self._allow_mutating_request():
                    return
                self._proxy()
                return
            self._send_bytes(404, b"not found\n", "text/plain; charset=utf-8")

        def do_PATCH(self) -> None:  # noqa: N802
            if self.path.startswith("/api/"):
                # tus chunk PATCHes are high-frequency data plane: exempt from
                # the per-IP rate limit but still subject to CSRF checks.
                if not _tus_data_plane(self.path) and not self._allow_api_request():
                    return
                if not self._allow_mutating_request():
                    return
                self._proxy()
                return
            self._send_bytes(404, b"not found\n", "text/plain; charset=utf-8")

        def do_DELETE(self) -> None:  # noqa: N802
            if self.path.startswith("/api/"):
                if not self._allow_api_request() or not self._allow_mutating_request():
                    return
                self._proxy()
                return
            self._send_bytes(404, b"not found\n", "text/plain; charset=utf-8")

        def do_OPTIONS(self) -> None:  # noqa: N802
            if self.path.startswith("/api/"):
                # tus capability discovery (OPTIONS /api/v1/files/tus).
                if not self._allow_api_request():
                    return
                self._proxy()
                return
            self._send_error(405, "HTTP.METHOD_NOT_ALLOWED")

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
            payload = (
                json.dumps(
                    {
                        "identity_mode": config.identity_mode.value,
                        "user": user,
                        "switchable": config.identity_mode == WebIdentityMode.DEMO,
                        "terminal_deep_link": config.terminal_deep_link,
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                + b"\n"
            )
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
            body, error = self._read_request_body()
            if error is not None:
                self._send_error(error[0], error[1])
                return
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
            # tus resumable-upload headers pass through untouched.
            for name in _TUS_PROXY_REQUEST_HEADERS:
                value = self.headers.get(name)
                if value is not None:
                    headers[name] = value
            if config.proxy_hmac_secret is not None:
                headers.update(
                    signed_proxy_headers(
                        secret=config.proxy_hmac_secret,
                        method=self.command,
                        target=self.path,
                        user=user,
                        body=body or b"",
                        trusted_user_header=config.trusted_user_header,
                    )
                )
            request = urllib.request.Request(
                url=f"{config.api_base_url}{self.path}",
                data=body,
                headers=headers,
                method=self.command,
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=config.upstream_timeout_seconds
                ) as response:
                    if self._streamable_download():
                        self._stream_upstream(response)
                        return
                    response_body = response.read(config.max_response_body_bytes + 1)
                    if len(response_body) > config.max_response_body_bytes:
                        self._send_error(502, "WEB.UPSTREAM_RESPONSE_TOO_LARGE")
                        return
                    self._send_bytes(
                        response.status,
                        response_body,
                        response.headers.get("Content-Type", "application/json; charset=utf-8"),
                        send_body=self.command != "HEAD",
                        extra_headers=_tus_response_headers(response.headers),
                    )
            except urllib.error.HTTPError as exc:
                response_body = exc.read(config.max_response_body_bytes + 1)
                if len(response_body) > config.max_response_body_bytes:
                    self._send_error(502, "WEB.UPSTREAM_RESPONSE_TOO_LARGE")
                    return
                self._send_bytes(
                    exc.code,
                    response_body,
                    exc.headers.get("Content-Type", "application/json; charset=utf-8"),
                    send_body=self.command != "HEAD",
                    extra_headers=_tus_response_headers(exc.headers),
                )
            except OSError as exc:
                payload = (
                    json.dumps(
                        {
                            "error": {
                                "code": "WEB.UPSTREAM_UNAVAILABLE",
                                "message": str(exc),
                            }
                        },
                        ensure_ascii=False,
                    ).encode("utf-8")
                    + b"\n"
                )
                self._send_bytes(502, payload, "application/json; charset=utf-8")

        def _streamable_download(self) -> bool:
            """Large file reads bypass whole-response buffering.

            ``/api/v1/files/content`` returns one base64 slice per request; the
            client assembles the file.  Piping the upstream response avoids
            holding the slice (and any future raw download) entirely in memory
            and lifts the ``max_response_body_bytes`` cap for these paths.
            """
            return self.command == "GET" and urlparse(self.path).path.startswith(
                "/api/v1/files/content"
            )

        def _stream_upstream(self, response: HTTPResponse) -> None:
            status = response.status
            headers = response.headers
            self.send_response(status)
            self.send_header(
                "Content-Type",
                headers.get("Content-Type", "application/octet-stream"),
            )
            length = headers.get("Content-Length")
            if length is not None:
                self.send_header("Content-Length", length)
            disposition = headers.get("Content-Disposition")
            if disposition:
                self.send_header("Content-Disposition", disposition)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command == "HEAD":
                return
            remaining = int(length) if length is not None else None
            while True:
                if remaining is not None:
                    if remaining <= 0:
                        break
                    block = response.read(min(65536, remaining))
                else:
                    block = response.read(65536)
                if not block:
                    break
                self.wfile.write(block)
                if remaining is not None:
                    remaining -= len(block)

        def _send_bytes(
            self,
            status: int,
            body: bytes,
            content_type: str,
            *,
            cache_control: str = "no-store",
            send_body: bool = True,
            extra_headers: Mapping[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache_control)
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                "base-uri 'self'; frame-ancestors 'none'",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            if config.enable_hsts:
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def _allow_api_request(self) -> bool:
            allowed, retry_after = rate_limiter.check(self.client_address[0])
            if allowed:
                return True
            self._send_bytes(
                429,
                b'{"error":{"code":"HTTP.RATE_LIMITED"}}\n',
                "application/json; charset=utf-8",
                extra_headers={"Retry-After": str(retry_after)},
            )
            return False

        def _allow_mutating_request(self) -> bool:
            error = mutating_request_error(config, dict(self.headers.items()))
            if error is None:
                return True
            self._send_error(403, error)
            return False

        def _read_request_body(self) -> tuple[bytes | None, tuple[int, str] | None]:
            if self.headers.get("Transfer-Encoding"):
                return None, (400, "HTTP.TRANSFER_ENCODING_UNSUPPORTED")
            value = self.headers.get("Content-Length", "0") or "0"
            try:
                length = int(value)
            except ValueError:
                return None, (400, "HTTP.CONTENT_LENGTH_INVALID")
            if length < 0:
                return None, (400, "HTTP.CONTENT_LENGTH_INVALID")
            if length > config.max_request_body_bytes:
                return None, (413, "HTTP.REQUEST_TOO_LARGE")
            return self.rfile.read(length) if length else None, None

        def _send_error(self, status: int, code: str) -> None:
            payload = (
                json.dumps({"error": {"code": code}}, separators=(",", ":")).encode("utf-8") + b"\n"
            )
            self._send_bytes(status, payload, "application/json; charset=utf-8")

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


def normalize_origin(value: str) -> str | None:
    if any(ord(character) < 32 for character in value):
        return None
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return None
    default_port = 80 if parsed.scheme == "http" else 443
    try:
        port = parsed.port
    except ValueError:
        return None
    hostname = parsed.hostname
    assert hostname is not None
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    authority = rendered_host if port in {None, default_port} else f"{rendered_host}:{port}"
    return f"{parsed.scheme}://{authority}"


def mutating_request_error(config: WebConfig, headers: Mapping[str, str]) -> str | None:
    normalized = {key.lower(): value for key, value in headers.items()}
    if normalized.get("cookie"):
        return "CSRF.COOKIE_AUTH_UNSUPPORTED"
    fetch_site = normalized.get("sec-fetch-site", "").lower()
    if fetch_site in {"cross-site", "same-site"}:
        return "CSRF.CROSS_SITE_DENIED"
    content_type = normalized.get("content-type", "").split(";", 1)[0].strip().lower()
    # tus PATCH streams ``application/offset+octet-stream`` and DELETE/OPTIONS
    # carry no body; only JSON-equivalent or bodyless requests pass, while the
    # browser-auto-submittable form encodings stay rejected.
    if content_type not in {"", "application/json", "application/offset+octet-stream"}:
        return "CSRF.JSON_REQUIRED"
    origin = normalized.get("origin")
    if origin:
        expected = normalize_origin(config.public_origin) if config.public_origin else None
        if expected is None:
            host = normalized.get("x-forwarded-host") or normalized.get("host", "")
            scheme = normalized.get("x-forwarded-proto", "http").strip().lower()
            if scheme not in {"http", "https"}:
                scheme = "http"
            expected = normalize_origin(f"{scheme}://{host}") if host else None
        if normalize_origin(origin) != expected:
            return "CSRF.ORIGIN_DENIED"
    return None


def _tus_response_headers(upstream: Message) -> dict[str, str]:
    """Relay tus protocol headers from the upstream API response."""
    forwarded: dict[str, str] = {}
    for name in _TUS_PROXY_RESPONSE_HEADERS:
        value = upstream.get(name)
        if value is not None:
            forwarded[name] = str(value)
    return forwarded


def _env_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
            terminal_deep_link=env_config.terminal_deep_link,
            proxy_hmac_secret=env_config.proxy_hmac_secret,
            public_origin=env_config.public_origin,
            enable_hsts=env_config.enable_hsts,
            max_request_body_bytes=env_config.max_request_body_bytes,
            max_response_body_bytes=env_config.max_response_body_bytes,
            rate_limit_requests=env_config.rate_limit_requests,
            rate_limit_window_seconds=env_config.rate_limit_window_seconds,
        ),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
