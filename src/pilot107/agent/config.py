"""Bounded, secret-safe configuration for the Python Agentd client."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass(frozen=True)
class AgentdClientConfig:
    base_url: str
    token: str = field(repr=False)
    model_profile_id: str
    timeout_seconds: float = 60.0
    max_output_tokens: int = 1_200

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _validated_url(self.base_url))
        if not self.token or len(self.token) > 4_096 or any(char.isspace() for char in self.token):
            raise ValueError("PILOT107_AGENTD_TOKEN must be a non-empty bearer token")
        if _ID_PATTERN.fullmatch(self.model_profile_id) is None:
            raise ValueError("PILOT107_AGENTD_MODEL_PROFILE must be a valid protocol ID")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0.1 <= float(self.timeout_seconds) <= 660.0
        ):
            raise ValueError("timeout_seconds is outside the supported range")
        if (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or not 1 <= self.max_output_tokens <= 32_000
        ):
            raise ValueError("max_output_tokens is outside the supported range")

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        prefix: str = "PILOT107_AGENTD_",
    ) -> AgentdClientConfig:
        return config_from_env(env, prefix=prefix)


def config_from_env(
    env: Mapping[str, str] | None = None,
    *,
    prefix: str = "PILOT107_AGENTD_",
) -> AgentdClientConfig:
    values = os.environ if env is None else env
    names = {
        "url": f"{prefix}URL",
        "token": f"{prefix}TOKEN",
        "profile": f"{prefix}MODEL_PROFILE",
        "timeout": f"{prefix}TIMEOUT_SECONDS",
        "max_output_tokens": f"{prefix}MAX_OUTPUT_TOKENS",
    }
    url = values.get(names["url"])
    token = values.get(names["token"])
    profile = values.get(names["profile"])
    raw_timeout = values.get(names["timeout"])
    raw_max_output_tokens = values.get(names["max_output_tokens"])
    configured_values = (
        (names["url"], url),
        (names["token"], token),
        (names["profile"], profile),
    )
    missing = [name for name, value in configured_values if value is None or value.strip() == ""]
    if missing:
        raise ValueError(f"missing required Agentd configuration: {', '.join(missing)}")
    assert url is not None and token is not None and profile is not None
    try:
        timeout_seconds = (
            60.0 if raw_timeout is None or not raw_timeout.strip() else float(raw_timeout)
        )
    except ValueError:
        raise ValueError(f"{names['timeout']} must be numeric") from None
    try:
        max_output_tokens = (
            1_200
            if raw_max_output_tokens is None or not raw_max_output_tokens.strip()
            else int(raw_max_output_tokens)
        )
    except ValueError:
        raise ValueError(f"{names['max_output_tokens']} must be an integer") from None
    return AgentdClientConfig(
        base_url=url,
        token=token,
        model_profile_id=profile,
        timeout_seconds=timeout_seconds,
        max_output_tokens=max_output_tokens,
    )


def _validated_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("PILOT107_AGENTD_URL must be a valid URL") from None
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("PILOT107_AGENTD_URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("PILOT107_AGENTD_URL must not contain userinfo")
    if parsed.query or parsed.fragment:
        raise ValueError("PILOT107_AGENTD_URL must not contain a query or fragment")
    if port is not None and not 1 <= port <= 65_535:
        raise ValueError("PILOT107_AGENTD_URL contains an unsupported port")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
