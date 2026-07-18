from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pilot107.core.proxy_auth import (
    PROXY_SIGNATURE_HEADER,
    ProxyAuthConfigError,
    ProxyRequestAuthenticator,
    load_proxy_hmac_secret,
    signed_proxy_headers,
)


class ProxyAuthTests(unittest.TestCase):
    secret = b"0123456789abcdef0123456789abcdef"

    def test_signature_binds_identity_target_method_and_body(self) -> None:
        headers = signed_proxy_headers(
            secret=self.secret,
            method="POST",
            target="/api/v1/runs?owner=alice",
            user="alice",
            body=b'{"name":"demo"}',
            now=1_000,
            request_id="request-1",
        )
        authenticator = ProxyRequestAuthenticator(
            self.secret,
            max_age_seconds=30,
            clock=lambda: 1_010,
        )

        self.assertTrue(
            authenticator.verify(
                method="POST",
                target="/api/v1/runs?owner=alice",
                body=b'{"name":"demo"}',
                headers=headers,
            )
        )
        changed = dict(headers)
        changed["X-Pilot107-User"] = "bob"
        self.assertFalse(
            ProxyRequestAuthenticator(self.secret, clock=lambda: 1_010).verify(
                method="POST",
                target="/api/v1/runs?owner=alice",
                body=b'{"name":"demo"}',
                headers=changed,
            )
        )

    def test_expired_and_replayed_signatures_are_rejected(self) -> None:
        headers = signed_proxy_headers(
            secret=self.secret,
            method="GET",
            target="/api/v1/runs",
            user="alice",
            now=1_000,
            request_id="request-1",
        )
        authenticator = ProxyRequestAuthenticator(self.secret, clock=lambda: 1_020)
        self.assertTrue(
            authenticator.verify(
                method="GET", target="/api/v1/runs", body=b"", headers=headers
            )
        )
        self.assertFalse(
            authenticator.verify(
                method="GET", target="/api/v1/runs", body=b"", headers=headers
            )
        )
        self.assertFalse(
            ProxyRequestAuthenticator(self.secret, clock=lambda: 1_031).verify(
                method="GET", target="/api/v1/runs", body=b"", headers=headers
            )
        )

    def test_secret_loader_requires_one_strong_source(self) -> None:
        self.assertIsNone(load_proxy_hmac_secret(secret=None, secret_file=None))
        with self.assertRaisesRegex(ProxyAuthConfigError, "at least 32"):
            load_proxy_hmac_secret(secret="short", secret_file=None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret"
            path.write_text("a" * 32 + "\n", encoding="utf-8")
            self.assertEqual(
                load_proxy_hmac_secret(secret=None, secret_file=path),
                b"a" * 32,
            )
            with self.assertRaisesRegex(ProxyAuthConfigError, "only one"):
                load_proxy_hmac_secret(secret="b" * 32, secret_file=path)

    def test_signature_format_is_versioned(self) -> None:
        headers = signed_proxy_headers(
            secret=self.secret,
            method="GET",
            target="/api/v1/runs",
            user="alice",
            now=1_000,
            request_id="request-1",
        )
        self.assertRegex(headers[PROXY_SIGNATURE_HEADER], r"^v1=[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
