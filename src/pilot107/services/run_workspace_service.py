"""Compatibility alias for the former product-layer Run Workspace read model.

``Workspace`` is now reserved for the Agent writable/versioned filesystem
context. New product code must use :class:`RunContextService`. This module exists
only so existing callers can migrate without changing read-model semantics.
"""

from pilot107.services.run_context_service import RunContextService

RunWorkspaceService = RunContextService

__all__ = ["RunWorkspaceService"]
