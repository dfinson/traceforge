"""Information Flow Control (IFC) source labeling and taint tracking."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracemill.governance.state import SessionState, TaintEntry
    from tracemill.governance.types import EnrichmentContext


class Clearance(StrEnum):
    """IFC clearance levels (ordered from least to most privileged)."""
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    SECRET = "secret"


_CLEARANCE_ORDER = {c: i for i, c in enumerate(Clearance)}

# Public constants for IFC label rules (referenced by spec)
SCOPE_TO_LABEL: dict[str, str] = {
    "host": "ifc:host_access",
    "network": "ifc:network_access",
    "cloud": "ifc:cloud_access",
    "sandbox": "ifc:sandboxed",
}

PATH_LABEL_RULES: dict[str, Clearance] = {
    ".env": Clearance.SECRET,
    ".env.local": Clearance.SECRET,
    ".env.production": Clearance.SECRET,
    "secrets.yaml": Clearance.SECRET,
    "credentials.json": Clearance.SECRET,
    ".npmrc": Clearance.CONFIDENTIAL,
    ".pypirc": Clearance.CONFIDENTIAL,
    "id_rsa": Clearance.SECRET,
    ".ssh/config": Clearance.SECRET,
    "kubeconfig": Clearance.SECRET,
}

# Source label assignment rules
_SENSITIVE_PATHS = frozenset(PATH_LABEL_RULES.keys())
_SENSITIVE_EXTENSIONS = frozenset({".pem", ".key", ".p12", ".pfx", ".jks"})


class IFCChecker:
    """Information Flow Control — assigns source labels and tracks taints."""

    def check(
        self,
        ctx: "EnrichmentContext",
        src_labels: set[str],
        session_state: "SessionState",
    ) -> None:
        """Assign IFC labels based on event content and taint history."""
        from tracemill.governance.types import ToolCallEvent, ToolResultEvent
        from tracemill.governance.state import TaintEntry

        event = ctx.event

        # Check taint propagation BEFORE adding new taint (prevent self-taint)
        if session_state.taint_ledger:
            # If tool is writing and there's prior taint, propagate
            if ctx.base_classification.effect in ("mutating", "destructive"):
                max_clearance = max(
                    (t.clearance for t in session_state.taint_ledger),
                    key=lambda c: _CLEARANCE_ORDER.get(c, 0),
                    default=None,
                )
                if max_clearance:
                    src_labels.add(f"ifc:tainted_write:{max_clearance}")

        # Determine clearance of data being accessed by current event
        clearance = self._infer_clearance(ctx)
        if clearance and _CLEARANCE_ORDER[clearance] >= _CLEARANCE_ORDER[Clearance.CONFIDENTIAL]:
            src_labels.add(f"ifc:{clearance}")
            # Record taint for future events (not current)
            session_state.add_taint(TaintEntry(
                event_id=event.event_id,
                source_event_key=event.source_event_key,
                clearance=clearance,
                source=self._classify_source(ctx),
                payload_pointer="",
            ))

    def _infer_clearance(self, ctx: "EnrichmentContext") -> Clearance | None:
        """Infer clearance level from event context."""
        from tracemill.governance.types import ToolCallEvent
        import json as json_mod

        if not isinstance(ctx.event, ToolCallEvent):
            return None

        # Try structured path extraction first
        try:
            args_dict = json_mod.loads(ctx.event.tool_args_json)
            file_path = args_dict.get("path") or args_dict.get("file") or args_dict.get("filename") or ""
            if isinstance(file_path, str):
                # Check path segments and basename for sensitive file matches
                path_lower = file_path.lower()
                basename = path_lower.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                for sensitive_path in _SENSITIVE_PATHS:
                    if basename == sensitive_path or path_lower.endswith(f"/{sensitive_path}") or path_lower.endswith(f"\\{sensitive_path}"):
                        return PATH_LABEL_RULES.get(sensitive_path, Clearance.SECRET)
                for ext in _SENSITIVE_EXTENSIONS:
                    if basename.endswith(ext):
                        return Clearance.CONFIDENTIAL
        except (json_mod.JSONDecodeError, TypeError, AttributeError):
            pass

        # Fallback: boundary-aware text search on raw args
        args = ctx.event.tool_args_json.lower()
        for path in _SENSITIVE_PATHS:
            # Require path separator or string boundary before the sensitive name
            import re
            pattern = r'(?:^|[/\\\s"\':])' + re.escape(path) + r'(?:$|[/\\\s"\',})\]])'
            if re.search(pattern, args):
                return PATH_LABEL_RULES.get(path, Clearance.SECRET)

        for ext in _SENSITIVE_EXTENSIONS:
            # Extension must be at word boundary
            import re
            if re.search(re.escape(ext) + r'(?:$|["\s,}\]\)])', args):
                return Clearance.CONFIDENTIAL

        # Check MCP profile clearance if available
        if ctx.mcp_profiles:
            key = ctx.mcp_profile_key
            if key and key in ctx.mcp_profiles:
                profile_clearance = ctx.mcp_profiles[key].get("clearance")
                if profile_clearance:
                    return Clearance(profile_clearance)

        return None

    def _classify_source(self, ctx: "EnrichmentContext") -> str:
        """Classify the source type of the data access."""
        from tracemill.governance.types import ToolCallEvent, ToolResultEvent

        if isinstance(ctx.event, ToolResultEvent):
            return "tool_output"
        if isinstance(ctx.event, ToolCallEvent):
            args = ctx.event.tool_args_json.lower()
            if "read" in args or "get" in args or "fetch" in args:
                return "file_read"
        return "tool_input"
