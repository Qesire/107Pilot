"""Authenticated identity forwarding between the Web BFF and control API."""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

PROXY_SIGNATURE_HEADER = "X-Pilot107-Proxy-Signature"
PROXY_TIMESTAMP_HEADER = "X-Pilot107-Proxy-Timestamp"
REQUEST_ID_HEADER = "X-Request-ID"


class ProxyAuthConfigError(ValueError):
    """Raised when the shared proxy authentication secret is unsafe."""


def load_proxy_hmac_secret(
    *,
    secret: str | None,
    secret_file: str | Path | None,
) -> bytes | None:
    """Load one shared secret without silently choosing between two sources."""

    if secret and secret_file:
        raise ProxyAuthConfigError(
            "configure only one of PILOT107_PROXY_HMAC_SECRET or "
            "PILOT107_PROXY_HMAC_SECRET_FILE"
        )
    value = secret
    if secret_file:
        value = Path(secret_file).read_text(encoding="utf-8").strip()
    if not value:
        return None
    encoded = value.encode("utf-8")
    if len(encoded) < 32:
        raise ProxyAuthConfigError("proxy HMAC secret must contain at least 32 bytes")
    return encoded


def signed_proxy_headers(
    *,
    secret: bytes,
    method: str,
    target: str,
    user: str,
    body: bytes = b"",
    now: int | None = None,
    request_id: str | None = None,
    trusted_user_header: str = "X-Pilot107-User",
) -> dict[str, str]:
    timestamp = int(time.time()) if now is None else now
    selected_request_id = request_id or str(uuid4())
    signature = hmac.new(
        secret,
        _canonical_request(
            method=method,
            target=target,
            user=user,
            body=body,
            timestamp=timestamp,
            request_id=selected_request_id,
        ),
        hashlib.sha256,
    ).hexdigest()
    return {
        trusted_user_header: user,
        PROXY_TIMESTAMP_HEADER: str(timestamp),
        REQUEST_ID_HEADER: selected_request_id,
        PROXY_SIGNATURE_HEADER: f"v1={signature}",
    }


@dataclass
class ProxyRequestAuthenticator:
    """Verify signatures and reject request-id reuse inside the freshness window."""

    secret: bytes
    max_age_seconds: int = 30
    clock: Callable[[], float] = time.time
    _seen: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def verify(
        self,
        *,
        method: str,
        target: str,
        body: bytes,
        headers: Mapping[str, str],
        trusted_user_header: str = "X-Pilot107-User",
    ) -> bool:
        normalized = {key.lower(): value for key, value in headers.items()}
        user = normalized.get(trusted_user_header.lower(), "")
        request_id = normalized.get(REQUEST_ID_HEADER.lower(), "")
        signature = normalized.get(PROXY_SIGNATURE_HEADER.lower(), "")
        timestamp_text = normalized.get(PROXY_TIMESTAMP_HEADER.lower(), "")
        try:
            timestamp = int(timestamp_text)
        except ValueError:
            return False
        now = int(self.clock())
        if not user or not request_id or abs(now - timestamp) > self.max_age_seconds:
            return False
        expected = signed_proxy_headers(
            secret=self.secret,
            method=method,
            target=target,
            user=user,
            body=body,
            now=timestamp,
            request_id=request_id,
            trusted_user_header=trusted_user_header,
        )[PROXY_SIGNATURE_HEADER]
        if not hmac.compare_digest(signature, expected):
            return False
        with self._lock:
            cutoff = now - self.max_age_seconds
            self._seen = {
                seen_id: seen_at
                for seen_id, seen_at in self._seen.items()
                if seen_at >= cutoff
            }
            if request_id in self._seen:
                return False
            self._seen[request_id] = now
        return True


def _canonical_request(
    *,
    method: str,
    target: str,
    user: str,
    body: bytes,
    timestamp: int,
    request_id: str,
) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    fields = (
        "pilot107-proxy-v1",
        str(timestamp),
        method.upper(),
        target,
        user,
        body_hash,
        request_id,
    )
    return "\n".join(fields).encode("utf-8")
