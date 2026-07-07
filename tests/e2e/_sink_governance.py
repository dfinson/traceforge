"""Governance-stamped event factory for the sink e2e suite (issue #83).

Several sinks (:class:`~traceforge.sinks.console.ConsoleSink`,
:class:`~traceforge.sinks.webhook.WebhookSink`,
:class:`~traceforge.sinks.otel_exporter.OtelExporterSink`) only emit — or only
emit *richly* — for events that carry a governance ``recommendation`` (and, for
the console, a ``classification``). This helper builds such an event the same way
the enrichment pipeline stamps one, so the e2e tests drive the real filter/serialize
paths instead of hand-rolling metadata in every module.

The leading underscore keeps pytest from collecting this as a test module.
"""

from __future__ import annotations

from tests.conftest import make_event
from traceforge.classify.core import Classification
from traceforge.classify.risk import RiskAssessment
from traceforge.governance.results import (
    RecommendedAction,
    RiskRecommendation,
    SessionMeta,
)
from traceforge.types import EventKind, EventMetadata, SessionEvent


def governed_event(
    action: str = "deny",
    *,
    session_id: str = "gov-session",
    tool_name: str = "rm",
    arguments: str = "-rf /tmp/x",
    score: int = 90,
    level: str = "critical",
    confidence: str = "high",
    reason_code: str = "rule.block",
    kind: str = EventKind.TOOL_CALL_STARTED,
) -> SessionEvent:
    """A tool-call event stamped with a governance ``recommendation`` of ``action``.

    ``action`` is one of the :class:`RecommendedAction` values ("allow", "warn",
    "escalate", "deny", "transform"). The event also carries a ``classification``
    so the console sink (which gates on it) emits.
    """
    classification = Classification(mechanism="shell.execute", effect="destructive")
    risk = RiskAssessment(
        score=score,
        level=level,
        confidence=confidence,
        factors=("shell_execute",),
        mitre=("T1059",),
        version="risk-v2",
    )
    recommendation = RiskRecommendation(
        recommended_action=RecommendedAction(action),
        assessment=risk,
        reason_code=reason_code,
        canonical_id="cid-test",
    )
    governance = SessionMeta(
        classification=classification,
        risk_assessment=risk,
        recommendation=recommendation,
    )
    metadata = EventMetadata(classification=classification, governance=governance)
    return make_event(
        kind=kind,
        session_id=session_id,
        payload={"tool_name": tool_name, "arguments": arguments},
        metadata=metadata,
    )
