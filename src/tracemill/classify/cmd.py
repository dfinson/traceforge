"""cmd.exe command classification via lightweight tokenization.

No tree-sitter grammar exists for cmd.exe with sufficient maturity.
Uses simple splitting on & and && operators (respecting quotes) and
the shared rule table for binary-level classification.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from tracemill.classify.core import Classification
from tracemill.classify.coding import CodingMechanism
from tracemill.classify.shell import (
    build_classification_from_commands,
    classify_single_command,
)

if TYPE_CHECKING:
    from tracemill.classify.config import ClassificationEngine


def _split_cmd_commands(command: str) -> list[str]:
    """Split a cmd.exe command on & and && operators, respecting quotes."""
    segments: list[str] = []
    current = ""
    i = 0
    in_dquote = False

    while i < len(command):
        ch = command[i]
        if ch == '"':
            in_dquote = not in_dquote
            current += ch
        elif ch == "&" and not in_dquote:
            if current.strip():
                segments.append(current.strip())
            current = ""
            if i + 1 < len(command) and command[i + 1] == "&":
                i += 1
        else:
            current += ch
        i += 1

    if current.strip():
        segments.append(current.strip())
    return segments


def _extract_binary_and_subcmd(segment: str) -> tuple[str, str | None, list[str]]:
    """Extract binary, first positional arg, and flags from a cmd segment."""
    parts = segment.split()
    if not parts:
        return "", None, []

    binary = os.path.basename(parts[0]).lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1", ".sh", ".com"):
        if binary.endswith(suffix):
            binary = binary[: -len(suffix)]

    subcmd = None
    flags: list[str] = []
    for p in parts[1:]:
        if p.startswith("/") or p.startswith("-"):
            flags.append(p)
        elif subcmd is None:
            subcmd = p

    return binary, subcmd, flags


def classify_cmd_command(
    command: str,
    *,
    engine: ClassificationEngine,
) -> Classification:
    """Classify a cmd.exe command string into a full Classification."""
    if not command or not command.strip():
        return Classification(
            mechanism=CodingMechanism.PROCESS_SHELL,
            effect=None,
        )

    segments = _split_cmd_commands(command)
    if not segments:
        return Classification(
            mechanism=CodingMechanism.PROCESS_SHELL,
            effect=None,
        )

    from tracemill.classify.shell import _CommandClassification

    command_results: list[_CommandClassification] = []
    for segment in segments:
        binary, subcmd, flags = _extract_binary_and_subcmd(segment)
        if not binary:
            continue
        cmd_cls = classify_single_command(binary, subcmd, flags, engine=engine)
        command_results.append(cmd_cls)

    if not command_results:
        return Classification(
            mechanism=CodingMechanism.PROCESS_SHELL,
            effect=None,
            capability=frozenset({"subprocess"}),
        )

    return build_classification_from_commands(command_results, shell_dialect="cmd")
