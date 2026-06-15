"""MCP tool fingerprint drift detection with per-dimension semantic analysis."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from tracemill.classify.core import Classification
    from tracemill.governance.persistence import SystemStore
    from tracemill.governance.types import EnrichmentContext


@dataclass(frozen=True)
class MCPToolProfile:
    """Frozen fingerprint of an MCP tool at first-seen time."""

    tool_name: str
    server_namespace: str
    description_hash: str
    schema_hash: str
    registered_effect: str | None
    registered_role: frozenset[str]
    registered_capabilities: frozenset[str]
    registered_scope: frozenset[str]
    clearance: str | None
    first_seen: datetime
    last_seen: datetime


@dataclass(frozen=True)
class MCPIntegrityAlert:
    """Individual drift alert with severity."""

    tool_name: str
    server: str
    alert_type: Literal[
        "effect_escalation",
        "capability_gain",
        "scope_expansion",
        "description_change",
        "schema_change",
        "adversarial_pattern",
    ]
    previous: str
    current: str
    severity: Literal["info", "warning", "critical"]
    timestamp: datetime


# Effect precedence for escalation detection
_EFFECT_ORDER = {"read_only": 0, "informational": 1, "mutating": 2, "destructive": 3}
# Dangerous capabilities that trigger alerts when gained
_DANGEROUS_CAPS = frozenset(
    {"network_outbound", "elevated_privilege", "arbitrary_execution", "credential_exposure"}
)
# Scope escalation (higher = broader)
_SCOPE_ORDER = {"file": 0, "project": 1, "repository": 2, "host": 3, "network": 4}

# Adversarial patterns in tool descriptions
_ADVERSARIAL_PATTERNS = [
    re.compile(r"[\u200b-\u200f\u2028-\u202f\u2060-\u206f\ufeff]"),  # Invisible unicode
    re.compile(
        r"(?:ignore|disregard|forget)\s+(?:previous|prior|above)\s+(?:instructions?|rules?|constraints?)",
        re.IGNORECASE,
    ),  # Prompt injection
    re.compile(r"(?:you\s+are|act\s+as|pretend|role[-\s]?play)", re.IGNORECASE),  # Role override
    re.compile(
        r"(?<![A-Fa-f0-9])[A-Za-z0-9+/]{40,}(?:={1,2})(?![A-Za-z0-9+/=])"
    ),  # Base64 payload (requires padding, excludes hex-only)
    re.compile(r"<!--.*?-->", re.DOTALL),  # Hidden HTML comments
    re.compile(r"\{%.*?%\}", re.DOTALL),  # Template injection
]


@dataclass(frozen=True)
class MCPDeferredWrite:
    """Immutable deferred DB write — committed only after pipeline finalization."""

    kind: Literal["upsert", "last_seen"]
    server: str
    tool_name: str
    payload: str  # JSON for upsert, ISO timestamp for last_seen


@dataclass(frozen=True)
class MCPScanResult:
    """Complete scan output — alerts, novelty flag, and deferred writes."""

    alerts: tuple[MCPIntegrityAlert, ...]
    is_new: bool
    deferred_writes: tuple[MCPDeferredWrite, ...]


class MCPIntegrityScanner:
    """Full MCP integrity scanning: fingerprint drift + semantic analysis + adversarial detection."""

    def __init__(self, store: "SystemStore") -> None:
        self._store = store

    def scan(self, ctx: "EnrichmentContext", cap: set[str]) -> MCPScanResult:
        """Full scan: fingerprint comparison + semantic drift + adversarial patterns.

        Returns MCPScanResult with alerts, is_new flag, and deferred writes.
        Deferred writes MUST only be committed after pipeline finalization.
        """
        from tracemill.governance.types import ToolCallEvent

        if not isinstance(ctx.event, ToolCallEvent):
            return MCPScanResult(alerts=(), is_new=False, deferred_writes=())

        server = ctx.event.mcp_server_name or ""
        tool_name = ctx.event.tool_name or ""
        if not server or not tool_name:
            return MCPScanResult(alerts=(), is_new=False, deferred_writes=())

        alerts: list[MCPIntegrityAlert] = []
        deferred: list[MCPDeferredWrite] = []
        now = datetime.now(timezone.utc)

        # Compute current fingerprints
        desc_hash = self._hash(ctx.event.tool_description or "")
        schema_hash = self._hash(ctx.event.tool_schema_json or "")

        # Check stored profile
        stored = self._store.get_mcp_profile(server, tool_name)

        if stored is None:
            # First time — defer registration until pipeline finalization
            cls = ctx.base_classification
            deferred.append(
                MCPDeferredWrite(
                    kind="upsert",
                    server=server,
                    tool_name=tool_name,
                    payload=json.dumps(
                        {
                            "description_hash": desc_hash,
                            "schema_hash": schema_hash,
                            "registered_effect": cls.effect,
                            "registered_role": json.dumps(sorted(cls.role)) if cls.role else None,
                            "registered_capabilities": json.dumps(sorted(cls.capability))
                            if cls.capability
                            else None,
                            "registered_scope": json.dumps(sorted(cls.scope))
                            if cls.scope
                            else None,
                            "clearance": None,
                            "first_seen": now.isoformat(),
                            "last_seen": now.isoformat(),
                        }
                    ),
                )
            )
            # Still scan description for adversarial content on first-seen
            desc_alerts = self.scan_description(
                ctx.event.tool_description or "", tool_name, server, now
            )
            if desc_alerts:
                alerts.extend(desc_alerts)
                cap.add("mcp_drift")
            return MCPScanResult(
                alerts=tuple(alerts),
                is_new=True,
                deferred_writes=tuple(deferred),
            )

        # ── Fingerprint comparison ──
        if stored["description_hash"] != desc_hash:
            alerts.append(
                MCPIntegrityAlert(
                    tool_name=tool_name,
                    server=server,
                    alert_type="description_change",
                    previous=stored["description_hash"][:16],
                    current=desc_hash[:16],
                    severity="warning",
                    timestamp=now,
                )
            )

        if stored["schema_hash"] != schema_hash:
            alerts.append(
                MCPIntegrityAlert(
                    tool_name=tool_name,
                    server=server,
                    alert_type="schema_change",
                    previous=stored["schema_hash"][:16],
                    current=schema_hash[:16],
                    severity="critical",
                    timestamp=now,
                )
            )

        # ── Semantic drift: per-dimension comparison ──
        semantic_alerts = self.check_semantic_drift(
            tool_name, server, ctx.base_classification, stored, now
        )
        alerts.extend(semantic_alerts)

        # ── Adversarial pattern scanning ──
        desc_alerts = self.scan_description(
            ctx.event.tool_description or "", tool_name, server, now
        )
        alerts.extend(desc_alerts)

        # Apply labels based on alert severity
        if alerts:
            max_severity = max(a.severity for a in alerts)
            if max_severity in ("warning", "critical"):
                cap.add("mcp_drift")

        # Defer last_seen update until pipeline finalization
        deferred.append(
            MCPDeferredWrite(
                kind="last_seen",
                server=server,
                tool_name=tool_name,
                payload=now.isoformat(),
            )
        )

        return MCPScanResult(
            alerts=tuple(alerts),
            is_new=False,
            deferred_writes=tuple(deferred),
        )

    def check_semantic_drift(
        self,
        tool: str,
        server: str,
        current: "Classification",
        stored: dict,
        now: datetime,
    ) -> list[MCPIntegrityAlert]:
        """Per-dimension comparison against registered profile."""
        alerts: list[MCPIntegrityAlert] = []

        # Effect escalation
        reg_effect = stored.get("registered_effect")
        if reg_effect and current.effect:
            prev_order = _EFFECT_ORDER.get(reg_effect, 0)
            curr_order = _EFFECT_ORDER.get(current.effect, 0)
            if curr_order > prev_order:
                severity = "critical" if curr_order >= 3 else "warning"
                alerts.append(
                    MCPIntegrityAlert(
                        tool_name=tool,
                        server=server,
                        alert_type="effect_escalation",
                        previous=reg_effect,
                        current=current.effect,
                        severity=severity,
                        timestamp=now,
                    )
                )

        # Capability gain
        reg_caps_raw = stored.get("registered_capabilities")
        reg_caps = frozenset(json.loads(reg_caps_raw)) if reg_caps_raw else frozenset()
        new_caps = current.capability - reg_caps
        dangerous_new = new_caps & _DANGEROUS_CAPS
        if dangerous_new:
            alerts.append(
                MCPIntegrityAlert(
                    tool_name=tool,
                    server=server,
                    alert_type="capability_gain",
                    previous=",".join(sorted(reg_caps)),
                    current=",".join(sorted(dangerous_new)),
                    severity="critical",
                    timestamp=now,
                )
            )

        # Scope expansion
        reg_scope_raw = stored.get("registered_scope")
        reg_scope = frozenset(json.loads(reg_scope_raw)) if reg_scope_raw else frozenset()
        new_scope = current.scope - reg_scope
        if new_scope:
            max_prev = max((_SCOPE_ORDER.get(s, 0) for s in reg_scope), default=0)
            max_curr = max((_SCOPE_ORDER.get(s, 0) for s in new_scope), default=0)
            if max_curr > max_prev:
                alerts.append(
                    MCPIntegrityAlert(
                        tool_name=tool,
                        server=server,
                        alert_type="scope_expansion",
                        previous=",".join(sorted(reg_scope)),
                        current=",".join(sorted(new_scope)),
                        severity="warning" if max_curr < 3 else "critical",
                        timestamp=now,
                    )
                )

        return alerts

    def scan_description(
        self,
        description: str,
        tool_name: str,
        server: str,
        now: datetime,
    ) -> list[MCPIntegrityAlert]:
        """Detect adversarial patterns: invisible unicode, prompt injection, base64, hidden markup."""
        if not description:
            return []

        alerts: list[MCPIntegrityAlert] = []
        for pattern in _ADVERSARIAL_PATTERNS:
            match = pattern.search(description)
            if match:
                alerts.append(
                    MCPIntegrityAlert(
                        tool_name=tool_name,
                        server=server,
                        alert_type="adversarial_pattern",
                        previous="clean",
                        current=f"pattern:{pattern.pattern[:40]}",
                        severity="critical",
                        timestamp=now,
                    )
                )
                break  # One alert is enough

        return alerts

    def _hash(self, content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()
