"""Phase derivation logic — shared across tool, MCP, and shell classifiers.

Extracted to its own module to avoid circular imports between tools.py and mcp.py.
"""

from __future__ import annotations

from tracemill.classify.core import Classification, PhaseSegment
from tracemill.classify.workflow import Phase

# Rule table: (predicate, phase) — evaluated in order, first match wins for single-phase
_DERIVE_RULES: list[tuple[str, str, str]] = [
    # (kind, value, phase)
    ("action", "validate", Phase.VERIFICATION),
    ("action", "deliver", Phase.REVIEW),
    ("action", "retrieve", Phase.EXPLORATION),
    ("action", "analyze", Phase.EXPLORATION),
    ("action", "configure", Phase.IMPLEMENTATION),
    ("action", "execute", Phase.IMPLEMENTATION),
    ("action", "modify", Phase.IMPLEMENTATION),
    ("action", "persist", Phase.IMPLEMENTATION),
]


def derive_phase(cls: Classification) -> str:
    """Derive a single phase for a tool classification.

    Uses a rule table consistent with _phases_from_classification in the enricher.
    Returns the first matching phase; VCS persist/deliver is special-cased to REVIEW.
    """
    # Special case: VCS persist/deliver → review
    if cls.has_role("persistence.version_control") and (
        cls.has_action("persist") or cls.has_action("deliver")
    ):
        return Phase.REVIEW

    for kind, value, phase in _DERIVE_RULES:
        if kind == "action" and cls.has_action(value):
            return phase

    # Mechanism-based fallbacks
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
