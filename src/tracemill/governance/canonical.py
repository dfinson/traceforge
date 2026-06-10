"""Canonical action identity computation via SHA-256."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracemill.classify.core import Classification


_CANONICAL_VERSION = "v1"


def compute_canonical_hash(
    classification: "Classification",
    command: str | None = None,
    reason_code: str | None = None,
) -> str:
    """Compute canonical action identity hash.

    Includes: mechanism, effect, scope, role, action, stable capability/structure, command, reason_code.
    Excludes: source_labels, budget_pressure, phase_anomaly, semantic_drift (dynamic labels).
    """
    # Stable capability = capability minus dynamic labels
    dynamic_caps = frozenset({"budget_pressure"})
    stable_cap = sorted(classification.capability - dynamic_caps)

    # Stable structure = structure minus dynamic labels
    dynamic_struct = frozenset({"phase_anomaly", "semantic_drift"})
    stable_struct = sorted(classification.structure - dynamic_struct)

    payload = {
        "mechanism": classification.mechanism,
        "effect": classification.effect,
        "scope": sorted(classification.scope),
        "role": sorted(classification.role),
        "action": sorted(classification.action) if hasattr(classification, "action") else [],
        "stable_capability": stable_cap,
        "stable_structure": stable_struct,
        "command": command or "",
        "reason_code": reason_code or "",
    }

    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode()).hexdigest()
    return f"sha256:{_CANONICAL_VERSION}:{digest}"
