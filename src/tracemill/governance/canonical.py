"""Canonical action identity computation via SHA-256."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracemill.classify.core import Classification


_CANONICAL_VERSION = "v1"

# Session-contextual labels excluded from canonical hash (runtime-dependent)
_DYNAMIC_CAPABILITIES = frozenset({"budget_pressure", "mcp_drift"})
_DYNAMIC_STRUCTURES = frozenset({"phase_anomaly", "semantic_drift", "ifc_violation"})


def compute_canonical_hash(
    classification: "Classification",
    command: str | None = None,
    reason_code: str | None = None,
) -> str:
    """Compute canonical action identity hash.

    Includes: mechanism, effect, scope, role, action, stable capability/structure, command, reason_code.
    Excludes: source_labels, budget_pressure, phase_anomaly, semantic_drift (dynamic labels).
    Command is normalized (whitespace-collapsed) so formatting doesn't affect hash.
    """
    # Stable capability = capability minus dynamic labels
    stable_cap = sorted(classification.capability - _DYNAMIC_CAPABILITIES)

    # Stable structure = structure minus dynamic labels
    stable_struct = sorted(classification.structure - _DYNAMIC_STRUCTURES)

    payload: dict = {
        "v": _CANONICAL_VERSION,
        "mechanism": classification.mechanism,
        "effect": classification.effect,
        "scope": sorted(classification.scope),
        "role": sorted(classification.role),
        "action": sorted(classification.action) if hasattr(classification, "action") else [],
        "capability": stable_cap,
        "structure": stable_struct,
    }

    # Normalize command: strip + collapse internal whitespace
    if command:
        payload["command"] = " ".join(command.split())
    if reason_code:
        payload["reason"] = reason_code

    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode()).hexdigest()
    return f"sha256:{digest}"
