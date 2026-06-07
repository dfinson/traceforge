"""PowerShell command classification via tree-sitter AST analysis."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

import tree_sitter as ts
import tree_sitter_powershell as tsps

from tracemill.classify.core import Classification
from tracemill.classify.coding import CodingMechanism
from tracemill.classify.shell import (
    _CommandClassification,
    build_classification_from_commands,
    classify_single_command,
)

if TYPE_CHECKING:
    from tracemill.classify.config import ClassificationEngine

_PS_LANGUAGE = ts.Language(tsps.language())
_parser = ts.Parser(_PS_LANGUAGE)
_Q_COMMANDS = ts.Query(_PS_LANGUAGE, "(command) @cmd")
_STRIP_BINARY_EXT = re.compile(r"\.(exe|cmd|bat|ps1|sh)$", re.IGNORECASE)


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


def classify_powershell_command(
    command: str,
    *,
    engine: ClassificationEngine,
) -> Classification:
    """Classify a PowerShell command string into a full Classification."""
    if not command or not command.strip():
        return Classification(
            mechanism=CodingMechanism.PROCESS_SHELL,
            effect=None,
        )

    tree = _parser.parse(command.encode("utf-8"))
    cursor = ts.QueryCursor(_Q_COMMANDS)
    matches = cursor.matches(tree.root_node)

    if not matches:
        return Classification(
            mechanism=CodingMechanism.PROCESS_SHELL,
            effect=None,
            capability=frozenset({"subprocess"}),
        )

    command_results: list[_CommandClassification] = []

    for _pat, captures in matches:
        for node in captures.get("cmd", []):
            name, subcmd, parameters = _extract_from_command_node(node)
            if not name:
                continue

            # Normalize: strip path/extension for non-cmdlet binaries
            binary = _STRIP_BINARY_EXT.sub("", os.path.basename(name).lower())

            cmd_cls = classify_single_command(binary, subcmd, parameters, engine=engine)
            command_results.append(cmd_cls)

    if not command_results:
        return Classification(
            mechanism=CodingMechanism.PROCESS_SHELL,
            effect=None,
            capability=frozenset({"subprocess"}),
        )

    return build_classification_from_commands(command_results, shell_dialect="powershell")
