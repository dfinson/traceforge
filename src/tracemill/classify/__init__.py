"""Classification package — multi-dimensional tool and shell analysis."""

from tracemill.classify.cmd import classify_cmd_command
from tracemill.classify.core import (
    Action,
    Capability,
    Classification,
    Effect,
    Mechanism,
    Phase,
    Role,
    Scope,
    ShellActivity,
    ShellDialect,
    Structure,
    ToolCategory,
    Visibility,
    aggregate_effect,
)
from tracemill.classify.coding import (
    CodingAction,
    CodingMechanism,
    CodingRole,
    CodingScope,
)
from tracemill.classify.powershell import classify_powershell_command
from tracemill.classify.registry import DimensionRegistry, get_default_registry
from tracemill.classify.shell import (
    SHELL_GIT_OPS,
    SHELL_IMPLEMENTATION,
    SHELL_INVESTIGATION,
    SHELL_SETUP,
    SHELL_VERIFICATION,
    _extract_commands_from_ast,
    classify_shell,
    classify_shell_command,
)
from tracemill.classify.tools import (
    CANONICAL_TOOLS,
    TOOL_CATEGORY_MAP,
    classify_tool,
    classify_tool_detailed,
    normalize_tool_name,
)

__all__ = [
    # Core types
    "Action",
    "Capability",
    "Classification",
    "Effect",
    "Mechanism",
    "Phase",
    "Role",
    "Scope",
    "ShellActivity",
    "ShellDialect",
    "Structure",
    "ToolCategory",
    "Visibility",
    "aggregate_effect",
    # Coding domain
    "CodingAction",
    "CodingMechanism",
    "CodingRole",
    "CodingScope",
    # Registry
    "DimensionRegistry",
    "get_default_registry",
    # Shell classifiers
    "SHELL_GIT_OPS",
    "SHELL_IMPLEMENTATION",
    "SHELL_INVESTIGATION",
    "SHELL_SETUP",
    "SHELL_VERIFICATION",
    "_extract_commands_from_ast",
    "classify_shell",
    "classify_shell_command",
    "classify_cmd_command",
    "classify_powershell_command",
    # Tool classifiers
    "CANONICAL_TOOLS",
    "TOOL_CATEGORY_MAP",
    "classify_tool",
    "classify_tool_detailed",
    "normalize_tool_name",
]
