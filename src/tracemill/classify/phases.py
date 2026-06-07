"""Phase derivation logic — shared across tool, MCP, and shell classifiers.

Extracted to its own module to avoid circular imports between tools.py and mcp.py.
"""

from __future__ import annotations

from tracemill.classify.core import Classification, PhaseSegment
from tracemill.classify.workflow import Phase


def derive_phase(cls: Classification) -> str:
    """Derive a single phase for a tool classification.

    Same logic as _phases_from_classification fallback but returns one phase
    for building the phase_map of single-action tools.
    """
    if cls.has_action("validate"):
        return Phase.VERIFICATION
    if cls.has_role("persistence.version_control") and (
        cls.has_action("persist") or cls.has_action("deliver")
    ):
        return Phase.REVIEW
    if cls.has_action("deliver"):
        return Phase.REVIEW
    if cls.has_action("retrieve") or cls.has_action("analyze"):
        return Phase.EXPLORATION
    if cls.has_action("modify") or cls.has_action("persist"):
        return Phase.IMPLEMENTATION
    if cls.has_action("configure") or cls.has_action("execute"):
        return Phase.IMPLEMENTATION
    if cls.mechanism.startswith("communication"):
        return Phase.PLANNING
    if cls.mechanism.startswith("delegation"):
        return Phase.IMPLEMENTATION
    if cls.mechanism == "filesystem" and cls.effect == "read_only":
        return Phase.EXPLORATION
    return Phase.IMPLEMENTATION


def with_phase_map(cls: Classification) -> Classification:
    """Add a single-segment phase_map to a Classification.

    Reuses derive_phase() to keep phase assignment consistent across all
    tool types (native, MCP, shell single-command).
    """
    phase = derive_phase(cls)
    seg = PhaseSegment(
        phase=phase,
        actions=cls.action,
        scopes=cls.scope,
        roles=cls.role,
    )
    return Classification(
        mechanism=cls.mechanism,
        effect=cls.effect,
        scope=cls.scope,
        role=cls.role,
        action=cls.action,
        capability=cls.capability,
        structure=cls.structure,
        shell_dialect=cls.shell_dialect,
        binaries=cls.binaries,
        phase_map=(seg,),
    )
