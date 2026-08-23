"""Short-lived, opaque HMAC capabilities for the private Agent Tool Gateway."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

_SIGNING_CONTEXT = b"pilot107-agent-capability-v1."
_TOKEN_PART = re.compile(r"^[A-Za-z0-9_-]+$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_A1_TOOLS = frozenset(
    {
        "platform_get_snapshot",
        "platform_observation_get",
        "account_observation_get",
        "workspace_list",
        "workspace_search",
        "workspace_read",
        "run_get",
        "run_log_read",
        "evidence_read",
        "run_resources_get",
    }
)
_A2_TOOLS = frozenset(
    {
        "project_get",
        "workspace_list",
        "workspace_read",
        "workspace_patch",
        "workspace_diff",
        "sandbox_exec",
        "validation_schedule",
    }
)
_A2_OPERATIONS = frozenset({"read", "write", "validate"})
_MAX_LIFETIME_SECONDS = 120
_CLOCK_SKEW_SECONDS = 5


class AgentCapabilityError(RuntimeError):
    """Stable, non-secret capability verification failure."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AgentCapabilityClaims:
    owner: str
    session_id: str
    turn_id: str
    state_version: int
    fencing_token: int
    profile_id: str
    tools: frozenset[str]
    max_invocations: int
    max_bytes: int
    expires_at: int
    issued_at: int | None = None
    project_id: str | None = None
    workspace_id: str | None = None
    operations: frozenset[str] = frozenset()
    max_commands: int = 0


class AgentCapabilitySigner:
    """Issue and verify canonical, unpadded base64url capability tokens."""

    def __init__(self, secret: bytes, *, clock: Callable[[], int] | None = None) -> None:
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("Agent capability secret must contain at least 32 bytes")
        self._secret = secret
        self._clock = clock or (lambda: int(time.time()))

    def __repr__(self) -> str:
        return "AgentCapabilitySigner(secret=<redacted>)"

    def sign(self, claims: AgentCapabilityClaims) -> str:
        now = int(self._clock())
        issued = replace(claims, issued_at=now if claims.issued_at is None else claims.issued_at)
        try:
            _validate_claims(issued, now=now, signing=True)
        except (TypeError, ValueError):
            raise AgentCapabilityError(
                "Agent capability claims are invalid",
                code="AGENT.CAPABILITY.INVALID",
            ) from None
        payload = _canonical_payload(issued)
        encoded = _encode(payload)
        signature = hmac.new(
            self._secret,
            _SIGNING_CONTEXT + encoded.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return f"{encoded}.{_encode(signature)}"

    def verify(self, token: str) -> AgentCapabilityClaims:
        try:
            if not isinstance(token, str) or len(token) > 8_192 or token.count(".") != 1:
                raise ValueError
            encoded, encoded_signature = token.split(".", 1)
            payload = _decode(encoded)
            signature = _decode(encoded_signature)
            expected = hmac.new(
                self._secret,
                _SIGNING_CONTEXT + encoded.encode("ascii"),
                hashlib.sha256,
            ).digest()
            if len(signature) != hashlib.sha256().digest_size or not hmac.compare_digest(
                signature, expected
            ):
                raise ValueError
            claims = _parse_claims(payload)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeError):
            raise AgentCapabilityError(
                "Agent capability is invalid", code="AGENT.CAPABILITY.INVALID"
            ) from None
        now = int(self._clock())
        try:
            _validate_claims(claims, now=now, signing=False)
        except AgentCapabilityError:
            raise
        except (ValueError, TypeError):
            raise AgentCapabilityError(
                "Agent capability is invalid", code="AGENT.CAPABILITY.INVALID"
            ) from None
        return claims


def _canonical_payload(claims: AgentCapabilityClaims) -> bytes:
    value = {
        "exp": claims.expires_at,
        "fence": claims.fencing_token,
        "iat": claims.issued_at,
        "max_bytes": claims.max_bytes,
        "max_invocations": claims.max_invocations,
        "max_commands": claims.max_commands,
        "operations": sorted(claims.operations),
        "owner": claims.owner,
        "profile": claims.profile_id,
        "project": claims.project_id,
        "session": claims.session_id,
        "state_version": claims.state_version,
        "tools": sorted(claims.tools),
        "turn": claims.turn_id,
        "version": 1,
        "workspace": claims.workspace_id,
    }
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _parse_claims(payload: bytes) -> AgentCapabilityClaims:
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict) or set(value) != {
        "exp",
        "fence",
        "iat",
        "max_bytes",
        "max_invocations",
        "max_commands",
        "operations",
        "owner",
        "profile",
        "project",
        "session",
        "state_version",
        "tools",
        "turn",
        "version",
        "workspace",
    }:
        raise ValueError
    if (
        value["version"] != 1
        or not isinstance(value["tools"], list)
        or not isinstance(value["operations"], list)
    ):
        raise ValueError
    return AgentCapabilityClaims(
        owner=_string(value["owner"]),
        session_id=_string(value["session"]),
        turn_id=_string(value["turn"]),
        state_version=_integer(value["state_version"]),
        fencing_token=_integer(value["fence"]),
        profile_id=_string(value["profile"]),
        tools=frozenset(_string(item) for item in value["tools"]),
        max_invocations=_integer(value["max_invocations"]),
        max_bytes=_integer(value["max_bytes"]),
        expires_at=_integer(value["exp"]),
        issued_at=_integer(value["iat"]),
        project_id=_optional_string(value["project"]),
        workspace_id=_optional_string(value["workspace"]),
        operations=frozenset(_string(item) for item in value["operations"]),
        max_commands=_integer(value["max_commands"]),
    )


def _validate_claims(
    claims: AgentCapabilityClaims, *, now: int, signing: bool
) -> None:
    for value in (
        claims.owner,
        claims.session_id,
        claims.turn_id,
        claims.profile_id,
    ):
        if _IDENTIFIER.fullmatch(value) is None:
            raise ValueError("invalid capability binding")
    if claims.profile_id in {"hpc-readonly-v1", "platform_coach"}:
        if (
            not claims.tools
            or not claims.tools.issubset(_A1_TOOLS)
            or claims.project_id is not None
            or claims.workspace_id is not None
            or claims.operations
            or claims.max_commands != 0
        ):
            raise ValueError("invalid read-only capability scope")
    elif claims.profile_id == "experiment_builder":
        for scoped_id in (claims.project_id, claims.workspace_id):
            if scoped_id is None or _IDENTIFIER.fullmatch(scoped_id) is None:
                raise ValueError("invalid builder capability binding")
        if (
            not claims.tools
            or not claims.tools.issubset(_A2_TOOLS)
            or not claims.operations
            or not claims.operations.issubset(_A2_OPERATIONS)
            or not 0 <= claims.max_commands <= 64
        ):
            raise ValueError("invalid builder capability scope")
        if "workspace_patch" in claims.tools and "write" not in claims.operations:
            raise ValueError("builder write tool lacks write operation")
        if "sandbox_exec" in claims.tools and (
            "validate" not in claims.operations or claims.max_commands < 1
        ):
            raise ValueError("builder sandbox tool lacks command budget")
        if "validation_schedule" in claims.tools and "validate" not in claims.operations:
            raise ValueError("builder validation tool lacks validate operation")
    else:
        raise ValueError("invalid capability profile")
    if claims.state_version <= 0 or claims.fencing_token <= 0:
        raise ValueError("invalid capability version")
    if not 1 <= claims.max_invocations <= 1_000:
        raise ValueError("invalid capability invocation budget")
    if not 1 <= claims.max_bytes <= 16 * 1024 * 1024:
        raise ValueError("invalid capability byte budget")
    if claims.issued_at is None:
        raise ValueError("capability issue time is missing")
    if claims.expires_at <= claims.issued_at:
        raise ValueError("invalid capability lifetime")
    if claims.expires_at - claims.issued_at > _MAX_LIFETIME_SECONDS:
        raise ValueError("capability lifetime exceeds maximum")
    if claims.issued_at > now + _CLOCK_SKEW_SECONDS:
        raise ValueError("capability issue time is in the future")
    if claims.expires_at < now - _CLOCK_SKEW_SECONDS:
        if signing:
            raise ValueError("cannot sign an expired capability")
        raise AgentCapabilityError(
            "Agent capability has expired", code="AGENT.CAPABILITY.EXPIRED"
        )


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    if not value or _TOKEN_PART.fullmatch(value) is None:
        raise ValueError
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError from exc


def _string(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError
    return value


def _integer(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError
    return value


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return _string(value)
