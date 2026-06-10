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
from tracemill.governance.pipeline import (
    Evidence,
    EvidencePointer,
    EscalationContext,
    GovernancePipeline,
    Phase3Result,
    RecommendedAction,
    RecommendationResult,
    RiskRecommendation,
    SessionMeta,
    TransformSuggestion,
)
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
from tracemill.governance.ifc import IFCChecker
from tracemill.governance.integrity import IntegrityVerifier
from tracemill.governance.mcp_drift import MCPIntegrityScanner
from tracemill.governance.drift import DriftDetector
from tracemill.governance.budget import BudgetThresholds, BudgetTracker

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
    "IntegrityVerifier",
    "MCPIntegrityScanner",
    "DriftDetector",
    # Budget
    "BudgetThresholds",
    "BudgetTracker",
]
