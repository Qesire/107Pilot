"""Opaque, filter-bound keyset cursors for read-model pagination."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any

_CURSOR_VERSION = 1
_MAX_CURSOR_LENGTH = 2048


class CursorError(ValueError):
    pass


@dataclass(frozen=True)
class CursorPosition:
    primary: str
    secondary: str


def cursor_scope(kind: str, filters: dict[str, Any]) -> str:
    material = json.dumps(
        {"kind": kind, "filters": filters},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:24]


def encode_cursor(*, kind: str, scope: str, position: CursorPosition) -> str:
    payload = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "kind": kind,
            "scope": scope,
            "primary": position.primary,
            "secondary": position.secondary,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(*, value: str, kind: str, scope: str) -> CursorPosition:
    if not value or len(value) > _MAX_CURSOR_LENGTH:
        raise CursorError("cursor is empty or too long")
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("ascii"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise CursorError("cursor is malformed") from exc
    if not isinstance(payload, dict):
        raise CursorError("cursor payload must be an object")
    if (
        payload.get("v") != _CURSOR_VERSION
        or payload.get("kind") != kind
        or payload.get("scope") != scope
    ):
        raise CursorError("cursor does not match this query")
    primary = payload.get("primary")
    secondary = payload.get("secondary")
    if not isinstance(primary, str) or not primary or not isinstance(secondary, str):
        raise CursorError("cursor position is invalid")
    return CursorPosition(primary=primary, secondary=secondary)
