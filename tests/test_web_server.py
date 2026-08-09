from __future__ import annotations

import base64
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from pilot107.adapters.slurm import LocalFileOpsExecutor
from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.file_routes import FileRoutes
from pilot107.api.http_app import Pilot107HttpApi, build_api
from pilot107.api.http_app import make_handler as make_api_handler
from pilot107.core.file_uploads import FileUploadService
from pilot107.core.run_store import RunStore
from pilot107.web.server import (
    WebConfig,
    WebIdentityMode,
    _tus_data_plane,
    config_from_env,
    is_safe_demo_user,
    is_safe_terminal_deep_link,
    make_handler,
    mutating_request_error,
    normalize_origin,
    resolve_proxy_user,
    resolve_static_request,
)
from pilot107.worker.evidence import EvidenceStore


class WebServerTests(unittest.TestCase):
    def test_config_from_env_reads_api_base_url(self) -> None:
        config = config_from_env(
            {
                "PILOT107_WEB_API_BASE_URL": "http://api:8080/",
                "PILOT107_PROXY_HMAC_SECRET": "x" * 32,
            }
        )

        self.assertEqual(config.api_base_url, "http://api:8080")
        self.assertEqual(config.proxy_hmac_secret, b"x" * 32)

    def test_web_config_defaults_to_alice(self) -> None:
        self.assertEqual(WebConfig(api_base_url="http://api:8080").demo_user, "alice")

    def test_demo_user_validation(self) -> None:
        self.assertTrue(is_safe_demo_user("alice"))
        self.assertTrue(is_safe_demo_user("bob.1-test"))
        self.assertFalse(is_safe_demo_user("../bob"))
        self.assertFalse(is_safe_demo_user(""))

    def test_config_rejects_unsafe_fixed_user(self) -> None:
        with self.assertRaisesRegex(ValueError, "safe username"):
            WebConfig(
                api_base_url="http://api:8080",
                fixed_user="../bob",
                identity_mode=WebIdentityMode.FIXED_USER,
            )

    def test_demo_mode_accepts_safe_client_selected_user(self) -> None:
        config = WebConfig(api_base_url="http://api:8080")

        self.assertEqual(resolve_proxy_user(config, {"x-pilot107-user": "bob"}), "bob")
        self.assertIsNone(resolve_proxy_user(config, {"X-Pilot107-User": "../bob"}))

    def test_fixed_user_mode_ignores_spoofed_client_identity(self) -> None:
        config = WebConfig(
            api_base_url="http://api:8080",
            fixed_user="alice",
            identity_mode=WebIdentityMode.FIXED_USER,
        )

        self.assertEqual(
            resolve_proxy_user(config, {"X-Pilot107-User": "bob"}),
            "alice",
        )

    def test_tus_data_plane_matches_chunk_endpoints_only(self) -> None:
        # High-frequency data plane: PATCH/HEAD on a specific upload id.
        self.assertTrue(_tus_data_plane("/api/v1/files/tus/abc123"))
        self.assertTrue(_tus_data_plane("/api/v1/files/tus/abc123?x=1"))
        # Control plane stays rate-limited: create/concat, capability, other APIs.
        self.assertFalse(_tus_data_plane("/api/v1/files/tus"))
        self.assertFalse(_tus_data_plane("/api/v1/files/tus/"))
        self.assertFalse(_tus_data_plane("/api/v1/files/uploads/abc/complete"))
        self.assertFalse(_tus_data_plane("/api/v1/runs"))

    def test_config_from_env_reads_fixed_identity_mode(self) -> None:
        config = config_from_env(
            {
                "PILOT107_WEB_API_BASE_URL": "http://api:8080",
                "PILOT107_WEB_IDENTITY_MODE": "fixed_user",
                "PILOT107_WEB_FIXED_USER": "alice",
            }
        )

        self.assertEqual(config.identity_mode, WebIdentityMode.FIXED_USER)
        self.assertEqual(config.fixed_user, "alice")

    def test_fixed_identity_mode_requires_explicit_user(self) -> None:
        with self.assertRaisesRegex(ValueError, "PILOT107_WEB_FIXED_USER is required"):
            config_from_env({"PILOT107_WEB_IDENTITY_MODE": "fixed_user"})

    def test_terminal_deep_link_is_configured_and_scheme_restricted(self) -> None:
        config = config_from_env(
            {
                "PILOT107_WEB_API_BASE_URL": "http://api:8080",
                "PILOT107_WEB_TERMINAL_DEEP_LINK": "https://terminal.example.edu/session",
            }
        )

        self.assertEqual(
            config.terminal_deep_link,
            "https://terminal.example.edu/session",
        )
        self.assertTrue(is_safe_terminal_deep_link("http://127.0.0.1:7681/"))
        self.assertFalse(is_safe_terminal_deep_link("javascript:alert(1)"))
        credential_url = "https://user:pass@example.edu"  # secret-scan: allow
        self.assertFalse(is_safe_terminal_deep_link(credential_url))
        with self.assertRaisesRegex(ValueError, "absolute HTTP"):
            config_from_env({"PILOT107_WEB_TERMINAL_DEEP_LINK": "javascript:alert(1)"})

    def test_mutating_requests_require_json_and_same_origin_without_cookies(self) -> None:
        config = WebConfig(
            api_base_url="http://api:8080",
            public_origin="https://pilot.example.edu",
        )
        valid = {
            "Content-Type": "application/json; charset=utf-8",
            "Origin": "https://pilot.example.edu",
            "Sec-Fetch-Site": "same-origin",
        }

        self.assertIsNone(mutating_request_error(config, valid))
        self.assertEqual(
            mutating_request_error(config, {**valid, "Cookie": "session=unsafe"}),
            "CSRF.COOKIE_AUTH_UNSUPPORTED",
        )
        self.assertEqual(
            mutating_request_error(config, {**valid, "Origin": "https://evil.example"}),
            "CSRF.ORIGIN_DENIED",
        )
        self.assertEqual(
            mutating_request_error(config, {"Content-Type": "text/plain"}),
            "CSRF.JSON_REQUIRED",
        )

    def test_mutating_request_allows_tus_content_types(self) -> None:
        config = WebConfig(api_base_url="http://api:8080")
        # tus PATCH streams ``application/offset+octet-stream``; DELETE/OPTIONS
        # carry no body, so an absent Content-Type must pass the CSRF gate.
        self.assertIsNone(
            mutating_request_error(
                config, {"Content-Type": "application/offset+octet-stream"}
            )
        )
        self.assertIsNone(mutating_request_error(config, {}))
        # Browser-auto-submittable form encodings stay rejected.
        self.assertEqual(
            mutating_request_error(
                config, {"Content-Type": "application/x-www-form-urlencoded"}
            ),
            "CSRF.JSON_REQUIRED",
        )

    def test_origin_fallback_honours_forwarded_proto_and_host(self) -> None:
        # No public_origin configured: the BFF sits behind an HTTPS reverse
        # proxy that strips Host and sets X-Forwarded-Proto/Host.
        config = WebConfig(api_base_url="http://api:8080", public_origin=None)
        base = {
            "Content-Type": "application/json",
            "Sec-Fetch-Site": "same-origin",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "pilot.example.edu:8443",
            "Host": "pilot107-web:3000",
        }
        self.assertIsNone(
            mutating_request_error(
                config, {**base, "Origin": "https://pilot.example.edu:8443"}
            )
        )
        self.assertEqual(
            mutating_request_error(
                config, {**base, "Origin": "http://pilot.example.edu:8443"}
            ),
            "CSRF.ORIGIN_DENIED",
        )

    def test_public_origin_is_canonical_and_hsts_is_explicit(self) -> None:
        config = config_from_env(
            {
                "PILOT107_WEB_PUBLIC_ORIGIN": "https://pilot.example.edu/",
                "PILOT107_WEB_ENABLE_HSTS": "true",
            }
        )

        self.assertEqual(normalize_origin(config.public_origin or ""), "https://pilot.example.edu")
        self.assertTrue(config.enable_hsts)
        self.assertIsNone(normalize_origin("https://pilot.example.edu/path"))
        self.assertIsNone(normalize_origin("https://pilot.example.edu:invalid"))
        self.assertEqual(normalize_origin("http://[::1]:3000"), "http://[::1]:3000")

    def test_static_assets_exist(self) -> None:
        static_root = Path(__file__).resolve().parents[1] / "src" / "pilot107" / "web" / "static"

        self.assertTrue((static_root / "index.html").exists())
        self.assertTrue((static_root / "assets" / "app.js").exists())
        self.assertTrue((static_root / "assets" / "styles.css").exists())

    def test_product_routes_fall_back_to_spa_but_missing_assets_do_not(self) -> None:
        static_root = Path(__file__).resolve().parents[1] / "src" / "pilot107" / "web" / "static"

        self.assertEqual(
            resolve_static_request("/runs/run_123", root=static_root),
            static_root / "index.html",
        )
        self.assertIsNone(resolve_static_request("/assets/missing.js", root=static_root))
        self.assertIsNone(resolve_static_request("/../../etc/passwd", root=static_root))

    def test_origin_probe_post_exercises_bff_csrf_origin_check(self) -> None:
        # Spin up the real API (http_app) and BFF (web.server) stack so a POST
        # to /api/v1/security/origin-probe traverses do_POST's
        # _allow_mutating_request() Origin check before reaching the route.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = build_api(
                db_path=root / "pilot107.db",
                evidence_root=root / "evidence",
                auth_required=False,
            )
            api_server = ThreadingHTTPServer(("127.0.0.1", 0), make_api_handler(api))
            api_thread = threading.Thread(target=api_server.serve_forever, daemon=True)
            api_thread.start()
            api_base = f"http://127.0.0.1:{api_server.server_port}"
            try:
                config = WebConfig(
                    api_base_url=api_base,
                    public_origin=None,  # fall back to Host header
                )
                bff_server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(config))
                bff_thread = threading.Thread(target=bff_server.serve_forever, daemon=True)
                bff_thread.start()
                base = f"http://127.0.0.1:{bff_server.server_port}"
                probe = base + "/api/v1/security/origin-probe"
                try:
                    # Matching Origin (derived from Host) -> 200 from the route.
                    req_ok = urllib.request.Request(
                        probe,
                        data=b"",
                        method="POST",
                        headers={
                            "Origin": base,
                            "Host": f"127.0.0.1:{bff_server.server_port}",
                            "Content-Type": "application/json",
                        },
                    )
                    with urllib.request.urlopen(req_ok, timeout=5) as response:
                        self.assertEqual(response.status, 200)

                    # Mismatched Origin -> 403 CSRF.ORIGIN_DENIED at the BFF.
                    req_bad = urllib.request.Request(
                        probe,
                        data=b"",
                        method="POST",
                        headers={
                            "Origin": "https://evil.example",
                            "Host": f"127.0.0.1:{bff_server.server_port}",
                            "Content-Type": "application/json",
                        },
                    )
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(req_bad, timeout=5)
                    self.assertEqual(ctx.exception.code, 403)
                    self.assertIn(
                        "CSRF.ORIGIN_DENIED",
                        ctx.exception.read().decode("utf-8", "replace"),
                    )

                    # Absent Origin -> BFF skips the check -> 200 from the route.
                    req_none = urllib.request.Request(
                        probe,
                        data=b"",
                        method="POST",
                        headers={
                            "Host": f"127.0.0.1:{bff_server.server_port}",
                            "Content-Type": "application/json",
                        },
                    )
                    with urllib.request.urlopen(req_none, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                finally:
                    bff_server.shutdown()
                    bff_server.server_close()
                    bff_thread.join(timeout=5)
            finally:
                api_server.shutdown()
                api_server.server_close()
                api_thread.join(timeout=5)


class TusProxyTests(unittest.TestCase):
    """The BFF forwards tus methods and protocol headers to the API verbatim.

    Spins up the real API (http_app with file routes) behind the real BFF
    (web.server) and drives a full tus lifecycle over HTTP, asserting that the
    tus request headers reach the API and the tus response headers are relayed
    back to the client.
    """

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self.cluster_root = root / "cluster" / "alice"
        self.cluster_root.mkdir(parents=True)
        executor = LocalFileOpsExecutor(allowed_roots=[str(self.cluster_root)])
        upload_service = FileUploadService(
            executor=executor,
            owner_roots=(str(self.cluster_root),),
            staging_root=root / "staging",
        )
        run_store = RunStore(root / "pilot107.db")
        api = Pilot107HttpApi(
            store=run_store,
            evidence_query=EvidenceQueryService(
                store=run_store,
                evidence_store=EvidenceStore(root / "evidence"),
            ),
            file_routes=FileRoutes(upload_service=upload_service, executor=executor),
            auth_required=True,
        )
        self._api_server = ThreadingHTTPServer(("127.0.0.1", 0), make_api_handler(api))
        self._api_thread = threading.Thread(
            target=self._api_server.serve_forever, daemon=True
        )
        self._api_thread.start()
        api_base = f"http://127.0.0.1:{self._api_server.server_port}"

        config = WebConfig(api_base_url=api_base, public_origin=None)
        self._bff_server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(config))
        self._bff_thread = threading.Thread(
            target=self._bff_server.serve_forever, daemon=True
        )
        self._bff_thread.start()
        self.base = f"http://127.0.0.1:{self._bff_server.server_port}"
        self.tus = f"{self.base}/api/v1/files/tus"
        self.target = str(self.cluster_root)

    def tearDown(self) -> None:
        self._bff_server.shutdown()
        self._bff_server.server_close()
        self._bff_thread.join(timeout=5)
        self._api_server.shutdown()
        self._api_server.server_close()
        self._api_thread.join(timeout=5)
        self._temporary.cleanup()

    @staticmethod
    def _request(method: str, url: str, *, headers=None, body=None):
        request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            return urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            return exc

    @staticmethod
    def _metadata(**fields: str) -> str:
        return ",".join(
            f"{key} {base64.b64encode(value.encode('utf-8')).decode('ascii')}"
            for key, value in fields.items()
        )

    def test_options_capabilities_relayed(self) -> None:
        response = self._request("OPTIONS", self.tus)
        self.assertEqual(response.status, 204)
        self.assertEqual(response.headers.get("Tus-Resumable"), "1.0.0")
        self.assertEqual(response.headers.get("Tus-Version"), "1.0.0")
        self.assertEqual(
            response.headers.get("Tus-Extension"), "creation,termination,concatenation"
        )
        self.assertIsNotNone(response.headers.get("Tus-Max-Size"))

    def test_create_patch_head_delete_headers_relayed(self) -> None:
        payload = b"hello tus through the bff" * 4
        user = {"X-Pilot107-User": "alice", "Tus-Resumable": "1.0.0"}

        # creation: Upload-Length/Upload-Metadata forwarded, Location relayed.
        # A tus create is a bodyless POST (no Content-Type), so pass body=None;
        # urllib would otherwise inject application/x-www-form-urlencoded.
        created = self._request(
            "POST",
            self.tus,
            headers={
                **user,
                "Upload-Length": str(len(payload)),
                "Upload-Metadata": self._metadata(
                    filename="proxied.bin", target_path=self.target
                ),
            },
        )
        self.assertEqual(created.status, 201)
        self.assertEqual(created.headers.get("Tus-Resumable"), "1.0.0")
        location = created.headers.get("Location")
        self.assertIsNotNone(location)
        upload_url = f"{self.base}{location}"

        # PATCH: offset+octet-stream body forwarded, new Upload-Offset relayed.
        patched = self._request(
            "PATCH",
            upload_url,
            headers={
                **user,
                "Content-Type": "application/offset+octet-stream",
                "Upload-Offset": "0",
            },
            body=payload,
        )
        self.assertEqual(patched.status, 204)
        self.assertEqual(patched.headers.get("Upload-Offset"), str(len(payload)))

        # HEAD resume probe: offset/length relayed, no body.
        head = self._request("HEAD", upload_url, headers=user)
        self.assertEqual(head.status, 200)
        self.assertEqual(head.headers.get("Upload-Offset"), str(len(payload)))
        self.assertEqual(head.headers.get("Upload-Length"), str(len(payload)))
        self.assertEqual(head.read(), b"")

        # DELETE termination.
        deleted = self._request("DELETE", upload_url, headers=user)
        self.assertEqual(deleted.status, 204)
        self.assertEqual(deleted.headers.get("Tus-Resumable"), "1.0.0")


if __name__ == "__main__":
    unittest.main()
