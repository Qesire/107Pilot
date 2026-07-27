"""Owner-scoped expansion for deployment-provided workspace roots.

``PILOT107_ALLOWED_ROOTS`` is intentionally deployment configuration rather
than a cluster fact.  A root may include ``{user}`` to express a per-user
workspace, for example ``/public/home/{user}``.  Expanding it at the point of
submission prevents the API's demo users from becoming a global filesystem
allow-list in a multi-user deployment.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from pilot107.core.identity import is_safe_username

USER_ROOT_TOKEN = "{user}"


class OwnerRootPolicyError(ValueError):
    """Raised when a root template cannot safely be expanded for an owner."""


def resolve_owner_roots(
    roots: Iterable[str | Path],
    *,
    user: str,
) -> tuple[str, ...]:
    """Expand owner-root templates without widening non-template roots.

    Plain roots remain useful for an administrator-managed shared project
    directory.  Only the documented ``{user}`` token is interpolated, and
    interpolation is refused for an unsafe Slurm user name.
    """

    if not is_safe_username(user):
        raise OwnerRootPolicyError(f"unsafe owner for allowed-root expansion: {user!r}")
    return tuple(str(root).replace(USER_ROOT_TOKEN, user) for root in roots)
