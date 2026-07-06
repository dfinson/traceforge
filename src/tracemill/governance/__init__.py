"""Governance extensions — enrichment pipeline, labeling, and persistence."""

from tracemill.governance.types import (
    CommandAnalysis,
    EnrichmentContext,
    PipeSegment,
    SessionEvent,
    ToolCallEvent,
    ToolResultEvent,
    compute_source_event_key,
)
from tracemill.governance.state import (
    BudgetSnapshot,
    SessionState,
    SessionStateSnapshot,
    TaintEntry,
)
from tracemill.governance.persistence import SystemStore
from tracemill.governance.labeler import GovernanceLabeler, GovernanceResult
from tracemill.governance.results import (
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
from tracemill.governance.pipeline import GovernancePipeline
from tracemill.governance.rules import (
    Predicate,
    Rule,
    RuleMatch,
    RecommendationTemplate,
    evaluate_rules,
    parse_rules,
)
from tracemill.governance.risk_wrapper import RiskModifiers, assess_governance_risk
from tracemill.governance.canonical import compute_canonical_hash
from tracemill.governance.pii import PIIScanner
from tracemill.governance.ifc import IFCChecker, SCOPE_TO_LABEL, PATH_LABEL_RULES
from tracemill.governance.integrity import IntegrityVerifier
from tracemill.governance.mcp_drift import MCPIntegrityScanner, MCPIntegrityAlert, MCPToolProfile
from tracemill.governance.drift import DriftDetector, DriftAssessment
from tracemill.governance.budget import BudgetThresholds, BudgetTracker
from tracemill.governance.envelope import ContextGapEvent, EnrichedEvent
from tracemill.governance.emitter import EnrichedEmitter
from tracemill.governance.observer import (
    AgentContext,
    GovernanceObserver,
    TracemillObserver,
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
    "TracemillObserver",
    "GovernanceObserver",
    "AgentContext",
    "create_observer",
]
