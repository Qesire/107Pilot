"""REST response semantic checks for Slurm adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RestSemanticLevel(StrEnum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class RestSemanticResult:
    level: RestSemanticLevel
    errors: list[Any] = field(default_factory=list)
    warnings: list[Any] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)

    @property
    def is_success(self) -> bool:
        return self.level in {RestSemanticLevel.OK, RestSemanticLevel.WARNING}


def check_slurm_rest_semantics(
    payload: dict[str, Any],
    *,
    required_fields: list[str] | None = None,
    partial_fields: list[str] | None = None,
) -> RestSemanticResult:
    """Check Slurm REST payload semantics beyond HTTP status."""

    required_fields = required_fields or []
    partial_fields = partial_fields or []
    errors = payload.get("errors") or []
    warnings = payload.get("warnings") or []
    missing = [field for field in required_fields if field not in payload]

    has_partial_payload = bool(errors) and any(field in payload for field in partial_fields)
    if has_partial_payload and not missing:
        return RestSemanticResult(
            level=RestSemanticLevel.WARNING,
            errors=list(errors),
            warnings=list(warnings),
        )

    if errors or missing:
        return RestSemanticResult(
            level=RestSemanticLevel.ERROR,
            errors=list(errors),
            warnings=list(warnings),
            missing_fields=missing,
        )
    if warnings:
        return RestSemanticResult(
            level=RestSemanticLevel.WARNING,
            warnings=list(warnings),
        )
    return RestSemanticResult(level=RestSemanticLevel.OK)
