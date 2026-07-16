"""Small HTTPS reverse proxy for local competition deployments."""

from __future__ import annotations

import argparse
import ssl
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class ProxyConfig:
    def __init__(self, *, target: str, https_port: int) -> None:
        self.target = target.rstrip("/")
        self.https_port = https_port


def make_redirect_handler(config: ProxyConfig) -> type[BaseHTTPRequestHandler]:
    class RedirectHandler(BaseHTTPRequestHandler):
        server_version = "pilot107-redirect/0.1"

        def do_GET(self) -> None:  # noqa: N802
            self._redirect()

        def do_POST(self) -> None:  # noqa: N802
            self._redirect()

        def log_message(self, format: str, *args: object) -> None:
            return

        def _redirect(self) -> None:
            host = self.headers.get("Host", "127.0.0.1").split(":", 1)[0]
            self.send_response(308)
            self.send_header("Location", f"https://{host}:{config.https_port}{self.path}")
            self.end_headers()

    return RedirectHandler


def make_proxy_handler(config: ProxyConfig) -> type[BaseHTTPRequestHandler]:
    class ProxyHandler(BaseHTTPRequestHandler):
        server_version = "pilot107-https-proxy/0.1"

        def do_GET(self) -> None:  # noqa: N802
            self._proxy()

        def do_POST(self) -> None:  # noqa: N802
            self._proxy()

        def log_message(self, format: str, *args: object) -> None:
            return

        def _proxy(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length > 0 else None
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in {"host", "connection", "content-length"}
            }
            headers["X-Forwarded-Proto"] = "https"
            headers["X-Forwarded-For"] = self.client_address[0]
            request = urllib.request.Request(
                url=f"{config.target}{self.path}",
                data=body,
                headers=headers,
                method=self.command,
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    response_body = response.read()
                    self.send_response(response.status)
                    for key, value in response.headers.items():
                        if key.lower() in {"connection", "transfer-encoding", "content-length"}:
                            continue
                        self.send_header(key, value)
                    self.send_header("Content-Length", str(len(response_body)))
                    self.end_headers()
                    self.wfile.write(response_body)
            except urllib.error.HTTPError as exc:
                response_body = exc.read()
                self.send_response(exc.code)
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)

    return ProxyHandler


def serve_http(*, host: str, port: int, handler: type[BaseHTTPRequestHandler]) -> None:
    server = ThreadingHTTPServer((host, port), handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the 107Pilot HTTPS reverse proxy.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--https-port", type=int, default=8443)
    parser.add_argument("--target", default="http://pilot107-web:3000")
    parser.add_argument("--cert", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    args = parser.parse_args(argv)

    config = ProxyConfig(target=args.target, https_port=args.https_port)
    https_server = ThreadingHTTPServer((args.host, args.https_port), make_proxy_handler(config))
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
    ssl_context.load_cert_chain(args.cert, args.key)
    https_server.socket = ssl_context.wrap_socket(https_server.socket, server_side=True)

    http_thread = threading.Thread(
        target=serve_http,
        kwargs={
            "host": args.host,
            "port": args.http_port,
            "handler": make_redirect_handler(config),
        },
        daemon=True,
    )
    http_thread.start()
    try:
        https_server.serve_forever()
    finally:
        https_server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
