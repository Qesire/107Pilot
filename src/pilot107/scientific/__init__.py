"""Deterministic application-side audits for scientific workflow outputs."""

from pilot107.scientific.heat_diffusion_validation import (
    HeatDiffusionAudit,
    audit_heat_diffusion_outputs,
)

__all__ = ["HeatDiffusionAudit", "audit_heat_diffusion_outputs"]
