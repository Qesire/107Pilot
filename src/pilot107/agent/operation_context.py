"""In-process binding between Tool Gateway operation identity and domain side effects.

The durable operation id is created by the Tool Gateway. Domain services may read
it while the authorized handler is executing so their own durable receipts can
reference the same identity. The value is process-local execution context only;
it is never accepted from model/tool arguments.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

_OPERATION_KEY = re.compile(r"^operation-[a-f0-9]{64}$")
_CURRENT_OPERATION_KEY: ContextVar[str | None] = ContextVar(
    "pilot107_current_agent_operation_key",
    default=None,
)


def current_agent_operation_key() -> str | None:
    """Return the currently executing durable Agent operation, if any."""

    return _CURRENT_OPERATION_KEY.get()


@contextmanager
def bind_agent_operation_key(operation_key: str | None) -> Iterator[None]:
    """Bind one trusted Gateway operation id for the duration of a handler call."""

    if operation_key is not None and _OPERATION_KEY.fullmatch(operation_key) is None:
        raise ValueError("operation_key is invalid")
    token: Token[str | None] = _CURRENT_OPERATION_KEY.set(operation_key)
    try:
        yield
    finally:
        _CURRENT_OPERATION_KEY.reset(token)
