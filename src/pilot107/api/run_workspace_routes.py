"""Compatibility alias for the former product-layer Run Workspace route.

New code uses :class:`RunContextRoutes`. The canonical URL is
``/api/v1/runs/{run_id}/context``; ``/workspace`` remains a temporary read-only
alias for existing clients. Agent Workspace terminology is otherwise reserved.
"""

from pilot107.api.run_context_routes import RunContextRoutes

RunWorkspaceRoutes = RunContextRoutes

__all__ = ["RunWorkspaceRoutes"]
