"""Shared HTTP transport types used by API route modules."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ApiResponse:
    status: int
    payload: dict[str, Any]
    headers: dict[str, str] | None = None
