"""Budget and session summary formatting utilities."""

from __future__ import annotations

from collections import Counter

from traceforge.types import SessionEvent


def format_budget_summary(budget_snapshot: dict) -> str:
    """Format budget consumption as a compact table string.

    Expects a dict with keys like:
        total_tool_calls, max_tool_calls,
        by_effect (dict of effect → count),
        by_capability (dict of capability → count),
        by_scope (dict of scope → count).
    """
    lines: list[str] = []
    lines.append("┌─ Budget Summary ────────────────────┐")

    total = budget_snapshot.get("total_tool_calls", 0)
    max_calls = budget_snapshot.get("max_tool_calls")
    if max_calls is not None:
        pct = (total / max_calls * 100) if max_calls > 0 else 0
        lines.append(f"│ Tool calls: {total}/{max_calls} ({pct:.0f}%)")
    else:
        lines.append(f"│ Tool calls: {total} (no limit)")

    for category in ("by_effect", "by_capability", "by_scope"):
        data = budget_snapshot.get(category)
        if data:
            label = category.replace("by_", "").title()
            entries = ", ".join(f"{k}={v}" for k, v in sorted(data.items()))
            lines.append(f"│ {label}: {entries}")

    lines.append("└─────────────────────────────────────┘")
    return "\n".join(lines)


def format_session_summary(
    events: list[SessionEvent],
    *,
    include_risk: bool = True,
) -> str:
    """Aggregate stats: event count by kind, risk distribution, top tools.

    Returns a multi-line summary string suitable for terminal display.
    """
    if not events:
        return "No events recorded."

    # Count by kind
    kind_counts: Counter[str] = Counter()
    risk_counts: Counter[str] = Counter()
    tool_counts: Counter[str] = Counter()

    for event in events:
        kind_counts[event.kind] += 1

        if event.metadata:
            if event.metadata.tool_display:
                tool_counts[event.metadata.tool_display] += 1
            if include_risk and event.metadata.classification:
                cls = event.metadata.classification
                if hasattr(cls, "risk_band") and cls.risk_band:
                    risk_counts[str(cls.risk_band)] += 1

    lines: list[str] = []
    lines.append(f"Session: {events[0].session_id} ({len(events)} events)")
    lines.append("")

    # Event kinds
    lines.append("Events by kind:")
    for kind, count in kind_counts.most_common(10):
        lines.append(f"  {kind:<35} {count:>4}")

    # Top tools
    if tool_counts:
        lines.append("")
        lines.append("Top tools:")
        for tool, count in tool_counts.most_common(5):
            lines.append(f"  {tool:<35} {count:>4}")

    # Risk distribution
    if include_risk and risk_counts:
        lines.append("")
        lines.append("Risk distribution:")
        for risk, count in risk_counts.most_common():
            lines.append(f"  {risk:<35} {count:>4}")

    return "\n".join(lines)
