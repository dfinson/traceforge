"""Classification package — multi-dimensional tool and shell analysis."""

from tracemill.classify.cmd import classify_cmd_command
from tracemill.classify.config import (
    ClassificationEngine,
    ClassifyConfig,
    get_default_engine,
    load_config,
    reset_default_engine,
    set_default_engine,
)
from tracemill.classify.core import (
    Action,
    Capability,
    Classification,
    Effect,
    Mechanism,
    PhaseSegment,
    Role,
    Scope,
    Structure,
    aggregate_effect,
)
from tracemill.classify.coding import (
    CodingAction,
    CodingMechanism,
    CodingRole,
    CodingScope,
    ShellDialect,
    ShellStructure,
)
from tracemill.classify.mcp import (
    McpServerProfile,
    McpToolOverride,
    classify_mcp_tool,
    extract_mcp_namespace,
)
from tracemill.classify.powershell import classify_powershell_command
from tracemill.classify.registry import DimensionRegistry, get_default_registry
from tracemill.classify.risk import Confidence, RiskAssessment, assess_risk
from tracemill.classify.shell import (
    _extract_commands_from_ast,
    classify_shell,
)
from tracemill.classify.tools import classify_tool, normalize_tool_name
from tracemill.classify.workflow import Phase, Visibility

__all__ = [
    # Core types
    "Action",
    "Capability",
    "Classification",
    "Effect",
    "Mechanism",
    "Phase",
    "PhaseSegment",
    "Role",
    "Scope",
    "ShellDialect",
    "ShellStructure",
    "Structure",
    "Visibility",
    "aggregate_effect",
    # Coding domain
    "CodingAction",
    "CodingMechanism",
    "CodingRole",
    "CodingScope",
    # Config
    "ClassificationEngine",
    "ClassifyConfig",
    "get_default_engine",
    "load_config",
    "reset_default_engine",
    "set_default_engine",
    # Registry
    "DimensionRegistry",
    "get_default_registry",
    # Classifiers
    "_extract_commands_from_ast",
    "classify_shell",
    "classify_cmd_command",
    "classify_powershell_command",
    "classify_tool",
    "classify_mcp_tool",
    "extract_mcp_namespace",
    "McpServerProfile",
    "McpToolOverride",
    "normalize_tool_name",
    # Risk scoring
    "Confidence",
    "RiskAssessment",
    "assess_risk",
]
