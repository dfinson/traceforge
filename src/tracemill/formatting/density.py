"""Density-based event formatting for terminal output and reports."""

from __future__ import annotations

from enum import StrEnum

from tracemill.trace import EventTrace
from tracemill.types import SessionEvent


class Density(StrEnum):
    """Output verbosity level."""

    MINIMAL = "minimal"  # one-line: "[risk_band] tool_name → effect"
    STANDARD = "standard"  # two-line: identity + classification summary
    VERBOSE = "verbose"  # full: all fields, raw_event excerpt


def format_event(event: SessionEvent, density: Density = Density.STANDARD) -> str:
    """Format a single SessionEvent at the given density level."""
    if density == Density.MINIMAL:
        return _format_event_minimal(event)
    elif density == Density.STANDARD:
        return _format_event_standard(event)
    else:
        return _format_event_verbose(event)


def format_trace(trace: EventTrace, density: Density = Density.STANDARD) -> str:
    """Format an EventTrace at the given density level."""
    if density == Density.MINIMAL:
        return _format_trace_minimal(trace)
    elif density == Density.STANDARD:
        return _format_trace_standard(trace)
    else:
        return _format_trace_verbose(trace)


# ─── SessionEvent formatters ────────────────────────────────────────────────


def _format_event_minimal(event: SessionEvent) -> str:
    risk = ""
    tool = ""
    if event.metadata and event.metadata.classification:
        cls = event.metadata.classification
        risk = f"[{cls.risk_band}] " if hasattr(cls, "risk_band") and cls.risk_band else ""
    if event.metadata and event.metadata.tool_display:
        tool = event.metadata.tool_display
    elif "tool_name" in event.payload:
        tool = event.payload["tool_name"]
    else:
        tool = event.kind

    effect = ""
    if event.metadata and event.metadata.classification:
        cls = event.metadata.classification
        if hasattr(cls, "effect") and cls.effect:
            effect = f" → {cls.effect}"

    return f"{risk}{tool}{effect}"


def _format_event_standard(event: SessionEvent) -> str:
    line1 = f"[{event.kind}] session={event.session_id} @ {event.timestamp.isoformat()}"
    parts: list[str] = []
    if event.metadata:
        if event.metadata.tool_display:
            parts.append(f"tool={event.metadata.tool_display}")
        if event.metadata.classification:
            cls = event.metadata.classification
            if hasattr(cls, "effect") and cls.effect:
                parts.append(f"effect={cls.effect}")
            if hasattr(cls, "risk_band") and cls.risk_band:
                parts.append(f"risk={cls.risk_band}")
    line2 = "  " + " ".join(parts) if parts else ""
    return f"{line1}\n{line2}".rstrip()


def _format_event_verbose(event: SessionEvent) -> str:
    lines = [
        f"[{event.kind}] id={event.id}",
        f"  session_id: {event.session_id}",
        f"  timestamp:  {event.timestamp.isoformat()}",
    ]
    if event.metadata:
        if event.metadata.tool_display:
            lines.append(f"  tool:       {event.metadata.tool_display}")
        if event.metadata.classification:
            lines.append(f"  classification: {event.metadata.classification}")
        if event.metadata.governance:
            lines.append(f"  governance: {event.metadata.governance}")
    # Truncated raw payload excerpt
    import json

    payload_str = json.dumps(event.payload, default=str)
    if len(payload_str) > 200:
        payload_str = payload_str[:200] + "..."
    lines.append(f"  payload:    {payload_str}")
    return "\n".join(lines)


# ─── EventTrace formatters ──────────────────────────────────────────────────


def _format_trace_minimal(trace: EventTrace) -> str:
    risk = f"[{trace.risk_band.value}] " if trace.risk_band else ""
    tool = trace.tool_name or trace.kind.value
    effect = f" → {trace.effect.value}" if trace.effect else ""
    return f"{risk}{tool}{effect}"


def _format_trace_standard(trace: EventTrace) -> str:
    line1 = f"[{trace.kind.value}] session={trace.session_id} tool={trace.tool_name or '-'}"
    parts: list[str] = []
    if trace.mechanism:
        parts.append(f"mechanism={trace.mechanism.value}")
    if trace.effect:
        parts.append(f"effect={trace.effect.value}")
    if trace.risk_band:
        parts.append(f"risk={trace.risk_band.value}")
    if trace.suggested_action:
        parts.append(f"action={trace.suggested_action.value}")
    line2 = "  " + " ".join(parts) if parts else ""
    return f"{line1}\n{line2}".rstrip()


def _format_trace_verbose(trace: EventTrace) -> str:
    lines = [
        f"[{trace.kind.value}] id={trace.id}",
        f"  session_id:  {trace.session_id}",
        f"  timestamp:   {trace.timestamp.isoformat()}",
        f"  tool_name:   {trace.tool_name or '-'}",
        f"  stage:       {trace.stage.value}",
    ]
    if trace.mechanism:
        lines.append(f"  mechanism:   {trace.mechanism.value}")
    if trace.effect:
        lines.append(f"  effect:      {trace.effect.value}")
    if trace.scope:
        lines.append(f"  scope:       {', '.join(s.value for s in trace.scope)}")
    if trace.risk_score is not None:
        lines.append(f"  risk_score:  {trace.risk_score}")
    if trace.risk_band:
        lines.append(f"  risk_band:   {trace.risk_band.value}")
    if trace.suggested_action:
        lines.append(f"  action:      {trace.suggested_action.value}")
    if trace.reason:
        lines.append(f"  reason:      {trace.reason}")
    # Raw event excerpt
    import json

    raw_str = json.dumps(dict(trace.raw_event) if trace.raw_event else {}, default=str)
    if len(raw_str) > 200:
        raw_str = raw_str[:200] + "..."
    lines.append(f"  raw_event:   {raw_str}")
    return "\n".join(lines)
