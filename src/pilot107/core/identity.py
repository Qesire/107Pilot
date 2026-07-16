"""Identity primitives for user-scoped platform operations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class IdentityMode(StrEnum):
    TRUSTED_HEADER = "trusted_header"
    DEMO = "demo"
    SINGLE_USER_JWT = "single_user_jwt"


class IdentityResolutionError(StrEnum):
    MISSING = "AUTH.MISSING"
    FORBIDDEN = "AUTH.FORBIDDEN"


@dataclass(frozen=True)
class UserIdentity:
    username: str
    mode: IdentityMode = IdentityMode.TRUSTED_HEADER
    credential_ref: str | None = None


@dataclass(frozen=True)
class IdentityResolution:
    identity: UserIdentity | None
    error: IdentityResolutionError | None = None


def resolve_trusted_header_identity(
    headers: Mapping[str, str] | None,
    *,
    header_name: str,
    required: bool,
) -> IdentityResolution:
    username = header_value(headers, header_name)
    if username is None or not username.strip():
        if required:
            return IdentityResolution(identity=None, error=IdentityResolutionError.MISSING)
        return IdentityResolution(identity=None)
    username = username.strip()
    if not is_safe_username(username):
        return IdentityResolution(identity=None, error=IdentityResolutionError.FORBIDDEN)
    return IdentityResolution(identity=UserIdentity(username=username))


def header_value(headers: Mapping[str, str] | None, name: str) -> str | None:
    if headers is None:
        return None
    lower_name = name.lower()
    for key, value in headers.items():
        if key.lower() == lower_name:
            return str(value)
    return None


def is_safe_username(username: str) -> bool:
    return bool(username) and all(char.isalnum() or char in "_.-" for char in username)
