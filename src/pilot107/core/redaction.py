"""Conservative secret redaction for logs, health, metrics, and audit payloads."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_SENSITIVE_KEY = re.compile(
    r"(?i)(?:^|[_-])(?:api[_-]?key|access[_-]?key|authorization|cookie|password|passwd|"
    r"private[_-]?key|secret|token)(?:$|[_-])"
)
_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)(?P<credentials>[^/@\s]+)@",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)(?P<prefix>\bbearer\s+)[A-Za-z0-9._~+/=-]+")
_ASSIGNMENT = re.compile(
    r"(?i)(?P<prefix>\b(?:api[_-]?key|access[_-]?key|authorization|cookie|password|"
    r"passwd|private[_-]?key|secret|token)\s*[:=]\s*)(?P<value>[^\s,;]+)"
)
_NON_SECRET_KEYS = frozenset(
    {
        "execution_fencing_token",
        "fencing_token",
        "llm_tokens",
        "max_llm_tokens",
        "max_tokens",
        "submission_fencing_token",
    }
)


def redact_sensitive_text(value: str, *, secrets: Sequence[str] = ()) -> str:
    """Redact common credential forms plus caller-supplied exact secrets."""

    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "<redacted>")
    redacted = _URL_CREDENTIALS.sub(r"\g<scheme><redacted>@", redacted)
    redacted = _BEARER.sub(r"\g<prefix><redacted>", redacted)
    return _ASSIGNMENT.sub(r"\g<prefix><redacted>", redacted)


def redact_sensitive_structure(value: Any) -> Any:
    """Recursively redact secret-bearing keys and credential-shaped strings."""

    if isinstance(value, Mapping):
        return {
            str(key): (
                "<redacted>"
                if _is_sensitive_key(str(key))
                else redact_sensitive_structure(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_structure(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_structure(item) for item in value)
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


def _is_sensitive_key(key: str) -> bool:
    return key.lower() not in _NON_SECRET_KEYS and _SENSITIVE_KEY.search(key) is not None
