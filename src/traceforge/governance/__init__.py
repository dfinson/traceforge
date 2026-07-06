"""Governance extensions — enrichment pipeline, labeling, and persistence."""

from traceforge.governance.types import (
    CommandAnalysis,
    EnrichmentContext,
    PipeSegment,
    SessionEvent,
    ToolCallEvent,
    ToolResultEvent,
    compute_source_event_key,
)
from traceforge.governance.state import (
    BudgetSnapshot,
    SessionState,
    SessionStateSnapshot,
    TaintEntry,
)
from traceforge.governance.persistence import SystemStore
from traceforge.governance.labeler import GovernanceLabeler, GovernanceResult
from traceforge.governance.results import (
    Evidence,
    EvidencePointer,
    EscalationContext,
    Phase3Result,
    RecommendedAction,
    RecommendationResult,
    RiskRecommendation,
    SessionMeta,
    TransformSuggestion,
)
from traceforge.governance.pipeline import GovernancePipeline
from traceforge.governance.rules import (
    Predicate,
    Rule,
    RuleMatch,
    RecommendationTemplate,
    evaluate_rules,
    parse_rules,
)
from traceforge.governance.risk_wrapper import RiskModifiers, assess_governance_risk
from traceforge.governance.canonical import compute_canonical_hash
from traceforge.governance.pii import PIIScanner
from traceforge.governance.ifc import IFCChecker, SCOPE_TO_LABEL, PATH_LABEL_RULES
from traceforge.governance.integrity import IntegrityVerifier
from traceforge.governance.mcp_drift import MCPIntegrityScanner, MCPIntegrityAlert, MCPToolProfile
from traceforge.governance.drift import DriftDetector, DriftAssessment
from traceforge.governance.budget import BudgetThresholds, BudgetTracker
from traceforge.governance.envelope import ContextGapEvent, EnrichedEvent
from traceforge.governance.emitter import EnrichedEmitter
from traceforge.governance.observer import (
    AgentContext,
    GovernanceObserver,
    TraceforgeObserver,
    create_observer,
)

__all__ = [
    # Types
    "CommandAnalysis",
    "EnrichmentContext",
    "PipeSegment",
    "SessionEvent",
    "ToolCallEvent",
    "ToolResultEvent",
    "compute_source_event_key",
    # State
    "BudgetSnapshot",
    "SessionState",
    "SessionStateSnapshot",
    "TaintEntry",
    # Persistence
    "SystemStore",
    # Labeler
    "GovernanceLabeler",
    "GovernanceResult",
    # Pipeline
    "Evidence",
    "EvidencePointer",
    "EscalationContext",
    "GovernancePipeline",
    "Phase3Result",
    "RecommendedAction",
    "RecommendationResult",
    "RiskRecommendation",
    "SessionMeta",
    "TransformSuggestion",
    # Rules
    "Predicate",
    "Rule",
    "RuleMatch",
    "RecommendationTemplate",
    "evaluate_rules",
    "parse_rules",
    # Risk
    "RiskModifiers",
    "assess_governance_risk",
    # Canonical
    "compute_canonical_hash",
    # Scanners
    "PIIScanner",
    "IFCChecker",
    "SCOPE_TO_LABEL",
    "PATH_LABEL_RULES",
    "IntegrityVerifier",
    "MCPIntegrityScanner",
    "MCPIntegrityAlert",
    "MCPToolProfile",
    "DriftDetector",
    "DriftAssessment",
    # Budget
    "BudgetThresholds",
    "BudgetTracker",
    # Envelope & Observer
    "ContextGapEvent",
    "EnrichedEvent",
    "EnrichedEmitter",
    "TraceforgeObserver",
    "GovernanceObserver",
    "AgentContext",
    "create_observer",
]
