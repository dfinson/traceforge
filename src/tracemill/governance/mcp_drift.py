"""MCP tool fingerprint drift detection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracemill.governance.persistence import SystemStore
    from tracemill.governance.types import EnrichmentContext


@dataclass(frozen=True)
class MCPDriftResult:
    """Result of comparing current MCP tool profile to stored fingerprint."""
    server: str
    tool_name: str
    description_changed: bool
    schema_changed: bool
    is_new: bool


class MCPIntegrityScanner:
    """Detects MCP tool fingerprint drift by comparing description/schema hashes."""

    def __init__(self, store: "SystemStore") -> None:
        self._store = store

    def scan(self, ctx: "EnrichmentContext", cap: set[str]) -> MCPDriftResult | None:
        """Check if tool's fingerprint has drifted from stored baseline."""
        from tracemill.governance.types import ToolCallEvent

        if not isinstance(ctx.event, ToolCallEvent):
            return None

        server = ctx.event.mcp_server_name or ""
        tool_name = ctx.event.tool_name or ""
        if not server or not tool_name:
            return None

        # Compute current fingerprints
        desc_hash = self._hash(ctx.event.tool_description or "")
        schema_hash = self._hash(ctx.event.tool_schema_json or "")

        # Check stored profile
        stored = self._store.get_mcp_profile(server, tool_name)
        now = datetime.now(timezone.utc).isoformat()

        if stored is None:
            # First time seeing this tool — register it
            self._store.upsert_mcp_profile(server, tool_name, {
                "description_hash": desc_hash,
                "schema_hash": schema_hash,
                "registered_effect": None,
                "registered_role": None,
                "registered_capabilities": None,
                "registered_scope": None,
                "clearance": None,
                "first_seen": now,
                "last_seen": now,
            })
            return MCPDriftResult(
                server=server, tool_name=tool_name,
                description_changed=False, schema_changed=False, is_new=True,
            )

        # Compare hashes
        desc_changed = stored["description_hash"] != desc_hash
        schema_changed = stored["schema_hash"] != schema_hash

        if desc_changed or schema_changed:
            cap.add("mcp_drift")
            # Update last_seen but keep original hashes for audit trail
            self._store.upsert_mcp_profile(server, tool_name, {
                "description_hash": desc_hash,
                "schema_hash": schema_hash,
                "registered_effect": stored.get("registered_effect"),
                "registered_role": stored.get("registered_role"),
                "registered_capabilities": stored.get("registered_capabilities"),
                "registered_scope": stored.get("registered_scope"),
                "clearance": stored.get("clearance"),
                "first_seen": stored["first_seen"],
                "last_seen": now,
            })

        return MCPDriftResult(
            server=server, tool_name=tool_name,
            description_changed=desc_changed, schema_changed=schema_changed, is_new=False,
        )

    def _hash(self, content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()
