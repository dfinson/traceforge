"""Information Flow Control (IFC) source labeling and taint tracking.

IFCChecker is the Phase-1 taint engine. It runs once per event (from
``phase1.py`` via ``labeler.check_ifc``) and its durable effect is the taint
ledger it accumulates on the mutable :class:`SessionState`. Phase-2 labeling
(``labeler.py``) reads the resulting ledger snapshot to emit the ``ifc_violation``
structure label and ``ifc:{clearance}`` source labels on egress.

The ``src_labels`` set populated by :meth:`IFCChecker.check` is part of the
method's contract (and is asserted directly in unit tests); the live pipeline
discards it, so the ledger is the channel that reaches downstream consumers.

Model (deterministic, CPU-only):

* **Clearance lattice** ``PUBLIC < INTERNAL < CONFIDENTIAL < SECRET``.
* **Data clearance** — sensitivity of the data an event reads or carries,
  inferred from sensitive file paths / extensions in tool args (call) or the
  result payload (result). Recorded as taint at ``CONFIDENTIAL`` or above.
* **Tool clearance** — the registered ceiling of a tool, taken from its MCP
  profile. When data the tool handles (accumulated ledger taint or the event's
  own data) strictly dominates that ceiling, the flow violates the partial
  order and is flagged as a clearance violation.
* **span_id propagation** — taint follows a span through the pre/post
  (ToolCallEvent -> ToolResultEvent) chain: a result inherits the clearance of
  a prior same-span taint (matched by span id, or by ``pre_call_event_id`` back
  to the call's event id). The originating span id is carried on each taint via
  ``TaintEntry.payload_pointer`` so lineage survives serialization.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from traceforge.governance.state import SessionState, TaintEntry
    from traceforge.governance.types import EnrichmentContext, SessionEvent


class Clearance(StrEnum):
    """IFC clearance levels (ordered from least to most privileged)."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    SECRET = "secret"


_CLEARANCE_ORDER = {c: i for i, c in enumerate(Clearance)}

# Clearance at or above which accessed/carried data is recorded as taint.
_TAINT_THRESHOLD = Clearance.CONFIDENTIAL

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

# Precompiled, boundary-aware patterns. Sensitive names must sit on a path
# separator or string boundary so substrings (e.g. "prevent") never match.
_PATH_PATTERNS: tuple[tuple[re.Pattern[str], Clearance], ...] = tuple(
    (
        re.compile(r'(?:^|[/\\\s"\':])' + re.escape(path) + r'(?:$|[/\\\s"\',})\]])'),
        PATH_LABEL_RULES.get(path, Clearance.SECRET),
    )
    for path in _SENSITIVE_PATHS
)
_EXT_PATTERNS: tuple[tuple[re.Pattern[str], Clearance], ...] = tuple(
    (re.compile(re.escape(ext) + r'(?:$|["\s,}\]\)])'), Clearance.CONFIDENTIAL)
    for ext in _SENSITIVE_EXTENSIONS
)


def _rank(clearance: object) -> int:
    """Lattice rank of a clearance value (accepts Clearance or its str value)."""
    return _CLEARANCE_ORDER.get(clearance, -1)  # type: ignore[arg-type]


def _higher(a: Clearance | None, b: Clearance | None) -> Clearance | None:
    """Return the higher-ranked of two optional clearances."""
    if a is None:
        return b
    if b is None:
        return a
    return a if _rank(a) >= _rank(b) else b


def _max_clearance(values) -> Clearance | str | None:
    """Return the highest-ranked clearance in ``values`` (skipping ``None``).

    Accepts a mix of :class:`Clearance` members and their raw string values
    (ledger entries store the value as ``str``); the original object is returned
    so callers can render it verbatim.
    """
    best: Clearance | str | None = None
    best_rank = -1
    for value in values:
        if value is None:
            continue
        rank = _rank(value)
        if rank > best_rank:
            best_rank = rank
            best = value
    return best


def _dominates(higher: object, lower: object) -> bool:
    """True when ``higher`` sits strictly above ``lower`` in the lattice."""
    return _rank(higher) > _rank(lower)


class IFCChecker:
    """Information Flow Control — assigns source labels and tracks taints."""

    def check(
        self,
        ctx: "EnrichmentContext",
        src_labels: set[str],
        session_state: "SessionState",
    ) -> None:
        """Assign IFC labels and accumulate taint for a single event.

        Populates ``src_labels`` (the method's contract) and records taint on
        ``session_state`` via the bounded :meth:`SessionState.add_taint`, which
        FIFO-evicts oldest entries past the ledger cap.
        """
        event = ctx.event
        ledger = session_state.taint_ledger
        span_id = self._span_id(event)

        # 1) Inbound span propagation — inherit taint from a prior event on the
        #    same span (pre/post chain), so a result carries its call's taint.
        inherited = self._inherited_span_clearance(event, ledger)
        if inherited is not None:
            src_labels.add(f"ifc:tainted_span:{inherited}")

        # 2) Egress of accumulated taint: a mutating/destructive action while
        #    the session already carries taint is an outbound flow.
        if ledger and ctx.base_classification.effect in ("mutating", "destructive"):
            accumulated = _max_clearance(t.clearance for t in ledger)
            if accumulated is not None:
                src_labels.add(f"ifc:tainted_write:{accumulated}")

        # 3) Clearance of the data this event reads or carries, combined with
        #    any clearance inherited along its span.
        data_clearance = self._infer_data_clearance(ctx)
        effective = _max_clearance((data_clearance, inherited))

        # 4) Record taint when the effective clearance is CONFIDENTIAL or above.
        recorded = False
        if effective is not None and _rank(effective) >= _rank(_TAINT_THRESHOLD):
            src_labels.add(f"ifc:{effective}")
            self._record_taint(
                session_state,
                event,
                clearance=effective,
                source=self._classify_source(ctx),
                span_id=span_id,
            )
            recorded = True

        # 5) Tool-clearance partial-order violation: the tool's registered
        #    ceiling is dominated by data it is handling (accumulated or own).
        self._check_tool_clearance(
            ctx, src_labels, session_state, effective=effective, recorded=recorded
        )

    # ── clearance inference ────────────────────────────────────────────────

    def _infer_data_clearance(self, ctx: "EnrichmentContext") -> Clearance | None:
        """Clearance of the data an event reads (call args) or carries (result)."""
        from traceforge.governance.types import ToolCallEvent, ToolResultEvent

        event = ctx.event
        if isinstance(event, ToolCallEvent):
            return self._clearance_from_args(event.tool_args_json)
        if isinstance(event, ToolResultEvent):
            return self._scan_sensitive_text((event.result_payload_json or "").lower())
        return None

    def _clearance_from_args(self, tool_args_json: str) -> Clearance | None:
        """Infer clearance from tool-call args (structured path + text scan)."""
        import json as json_mod

        structured: Clearance | None = None
        try:
            args_dict = json_mod.loads(tool_args_json)
            file_path = (
                args_dict.get("path") or args_dict.get("file") or args_dict.get("filename") or ""
            )
            if isinstance(file_path, str) and file_path:
                structured = self._clearance_from_path(file_path.lower())
        except (json_mod.JSONDecodeError, TypeError, AttributeError):
            structured = None

        return _higher(structured, self._scan_sensitive_text(tool_args_json.lower()))

    def _clearance_from_path(self, path_lower: str) -> Clearance | None:
        """Exact basename / suffix match against known sensitive files."""
        basename = path_lower.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        best: Clearance | None = None
        for sensitive_path in _SENSITIVE_PATHS:
            if (
                basename == sensitive_path
                or path_lower.endswith(f"/{sensitive_path}")
                or path_lower.endswith(f"\\{sensitive_path}")
            ):
                best = _higher(best, PATH_LABEL_RULES.get(sensitive_path, Clearance.SECRET))
        if best is not None:
            return best
        for ext in _SENSITIVE_EXTENSIONS:
            if basename.endswith(ext):
                return Clearance.CONFIDENTIAL
        return None

    def _scan_sensitive_text(self, text_lower: str) -> Clearance | None:
        """Boundary-aware scan for sensitive names/extensions in raw text.

        Returns the highest-ranked clearance across all matches so the result is
        independent of pattern iteration order.
        """
        if not text_lower:
            return None
        best: Clearance | None = None
        for pattern, clearance in _PATH_PATTERNS:
            if pattern.search(text_lower):
                best = _higher(best, clearance)
        if best is not None:
            return best
        for pattern, clearance in _EXT_PATTERNS:
            if pattern.search(text_lower):
                return clearance
        return None

    def _tool_clearance(self, ctx: "EnrichmentContext") -> Clearance | None:
        """The current tool's registered clearance ceiling from its MCP profile."""
        if not ctx.mcp_profiles:
            return None
        key = ctx.mcp_profile_key
        if not key or key not in ctx.mcp_profiles:
            return None
        raw = ctx.mcp_profiles[key].get("clearance")
        if not raw:
            return None
        try:
            return Clearance(raw)
        except ValueError:
            # Unknown clearance value in MCP profile — treat as unbounded (no ceiling).
            return None

    # ── span lineage ───────────────────────────────────────────────────────

    def _span_id(self, event: "SessionEvent") -> str | None:
        """Span identifier for an event, when the event type carries one."""
        span = getattr(event, "span_id", None)
        return span if isinstance(span, str) and span else None

    def _inherited_span_clearance(
        self, event: "SessionEvent", ledger: list["TaintEntry"]
    ) -> Clearance | str | None:
        """Highest clearance already tainted on this event's span.

        Matches prior taint by span id (carried in ``payload_pointer``) or, for a
        result event, by ``pre_call_event_id`` back to the call's ``event_id``.
        """
        if not ledger:
            return None
        span_id = self._span_id(event)
        pre_call_id = getattr(event, "pre_call_event_id", None)
        matches = [
            t.clearance
            for t in ledger
            if (span_id and t.payload_pointer == span_id)
            or (pre_call_id and t.event_id == pre_call_id)
        ]
        return _max_clearance(matches)

    # ── taint recording ────────────────────────────────────────────────────

    def _check_tool_clearance(
        self,
        ctx: "EnrichmentContext",
        src_labels: set[str],
        session_state: "SessionState",
        *,
        effective: Clearance | str | None,
        recorded: bool,
    ) -> None:
        """Flag a partial-order violation when data dominates the tool ceiling."""
        ceiling = self._tool_clearance(ctx)
        if ceiling is None:
            return
        candidates = [t.clearance for t in session_state.taint_ledger]
        if effective is not None:
            candidates.append(effective)
        offending = _max_clearance(candidates)
        if offending is None or not _dominates(offending, ceiling):
            return
        src_labels.add(f"ifc:ifc_violation:{offending}>{ceiling}")
        # Persist the violation so a downstream egress sees a prior taint, unless
        # this event already left an equivalent taint above.
        if not recorded:
            self._record_taint(
                session_state,
                ctx.event,
                clearance=offending,
                source="ifc_violation",
                span_id=self._span_id(ctx.event),
            )

    def _record_taint(
        self,
        session_state: "SessionState",
        event: "SessionEvent",
        *,
        clearance: Clearance | str,
        source: str,
        span_id: str | None,
    ) -> None:
        """Append a taint entry through the bounded, FIFO-evicting ledger."""
        from traceforge.governance.state import TaintEntry

        session_state.add_taint(
            TaintEntry(
                event_id=event.event_id,
                source_event_key=event.source_event_key,
                clearance=str(clearance),
                source=source,
                payload_pointer=span_id or "",
            )
        )

    def _classify_source(self, ctx: "EnrichmentContext") -> str:
        """Classify the source type of the data access."""
        from traceforge.governance.types import ToolCallEvent, ToolResultEvent

        if isinstance(ctx.event, ToolResultEvent):
            return "tool_output"
        if isinstance(ctx.event, ToolCallEvent):
            args = ctx.event.tool_args_json.lower()
            if "read" in args or "get" in args or "fetch" in args:
                return "file_read"
        return "tool_input"
