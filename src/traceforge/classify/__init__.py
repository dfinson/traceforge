"""Classification package — multi-dimensional tool and shell analysis."""

from traceforge.classify.cmd import classify_cmd_command
from traceforge.classify.config import (
    ClassificationEngine,
    ClassifyConfig,
    get_default_engine,
    load_config,
    reset_default_engine,
    set_default_engine,
)
from traceforge.classify.core import (
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
from traceforge.classify.coding import (
    CodingAction,
    CodingMechanism,
    CodingRole,
    CodingScope,
    ShellDialect,
    ShellStructure,
)
from traceforge.classify.mcp import (
    McpServerProfile,
    McpToolOverride,
    classify_mcp_tool,
    extract_mcp_namespace,
)
from traceforge.classify.powershell import classify_powershell_command
from traceforge.classify.registry import DimensionRegistry, get_default_registry
from traceforge.classify.risk import Confidence, RiskAssessment, assess_risk, assess_tool_risk
from traceforge.classify.shell import (
    _extract_commands_from_ast,
    classify_shell,
)
from traceforge.classify.tools import classify_tool, normalize_tool_name
from traceforge.classify.tool_display import ToolDisplayProvider, ToolDisplayResolver
from traceforge.classify.workflow import Phase, Visibility

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
    # Tool display
    "ToolDisplayProvider",
    "ToolDisplayResolver",
    # Risk scoring
    "Confidence",
    "RiskAssessment",
    "assess_risk",
    "assess_tool_risk",
]
