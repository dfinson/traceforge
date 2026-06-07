"""Classification package — tool normalization, category mapping, and shell analysis."""

from tracemill.classify.shell import (
    SHELL_GIT_OPS,
    SHELL_IMPLEMENTATION,
    SHELL_INVESTIGATION,
    SHELL_SETUP,
    SHELL_VERIFICATION,
    _extract_commands_from_ast,
    classify_shell_command,
)
from tracemill.classify.tools import (
    CANONICAL_TOOLS,
    TOOL_CATEGORY_MAP,
    classify_tool,
    normalize_tool_name,
)

__all__ = [
    "CANONICAL_TOOLS",
    "SHELL_GIT_OPS",
    "SHELL_IMPLEMENTATION",
    "SHELL_INVESTIGATION",
    "SHELL_SETUP",
    "SHELL_VERIFICATION",
    "TOOL_CATEGORY_MAP",
    "_extract_commands_from_ast",
    "classify_shell_command",
    "classify_tool",
    "normalize_tool_name",
]
