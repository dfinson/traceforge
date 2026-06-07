"""PowerShell command classification via tree-sitter AST analysis."""

from __future__ import annotations

import os

import tree_sitter as ts
import tree_sitter_powershell as tsps

from tracemill.classify.rules import (
    ACTIVITY_PRIORITY,
    SHELL_IMPLEMENTATION,
    classify_binary,
)

_PS_LANGUAGE = ts.Language(tsps.language())
_parser = ts.Parser(_PS_LANGUAGE)
_Q_COMMANDS = ts.Query(_PS_LANGUAGE, "(command) @cmd")


def _extract_from_command_node(node: ts.Node) -> tuple[str, str | None, list[str]]:
    """Extract cmdlet/binary name, first positional arg, and parameters from a command node."""
    name = ""
    positionals: list[str] = []
    parameters: list[str] = []

    for child in node.children:
        if child.type == "command_name" and child.text:
            name = child.text.decode("utf-8").strip()
        elif child.type == "command_elements":
            for sub in child.children:
                if sub.type == "generic_token" and sub.text:
                    positionals.append(sub.text.decode("utf-8"))
                elif sub.type == "command_parameter" and sub.text:
                    parameters.append(sub.text.decode("utf-8"))

    subcmd = positionals[0] if positionals else None
    return name, subcmd, parameters


def classify_powershell_command(command: str) -> str:
    """Classify a PowerShell command string into an activity category."""
    if not command or not command.strip():
        return SHELL_IMPLEMENTATION

    tree = _parser.parse(command.encode("utf-8"))
    cursor = ts.QueryCursor(_Q_COMMANDS)
    matches = cursor.matches(tree.root_node)

    if not matches:
        return SHELL_IMPLEMENTATION

    best_activity = SHELL_IMPLEMENTATION
    best_priority = -1

    for _pat, captures in matches:
        for node in captures.get("cmd", []):
            name, subcmd, parameters = _extract_from_command_node(node)
            if not name:
                continue

            # Normalize: strip path/extension for non-cmdlet binaries
            binary = os.path.basename(name).lower()
            for suffix in (".exe", ".cmd", ".bat", ".ps1", ".sh"):
                if binary.endswith(suffix):
                    binary = binary[: -len(suffix)]

            # Use shared rule table (handles both cmdlets and binaries)
            activity = classify_binary(binary, subcmd, parameters)
            priority = ACTIVITY_PRIORITY.get(activity, 0)
            if priority > best_priority:
                best_priority = priority
                best_activity = activity

    return best_activity
