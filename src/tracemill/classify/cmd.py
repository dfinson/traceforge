"""cmd.exe command classification via lightweight tokenization.

No tree-sitter grammar exists for cmd.exe with sufficient maturity.
Uses simple splitting on & and && operators (respecting quotes) and
the shared rule table for binary-level classification.
"""

from __future__ import annotations

import os

from tracemill.classify.rules import (
    ACTIVITY_PRIORITY,
    SHELL_IMPLEMENTATION,
    classify_binary,
)


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


def classify_cmd_command(command: str) -> str:
    """Classify a cmd.exe command string into an activity category."""
    if not command or not command.strip():
        return SHELL_IMPLEMENTATION

    segments = _split_cmd_commands(command)
    if not segments:
        return SHELL_IMPLEMENTATION

    best_activity = SHELL_IMPLEMENTATION
    best_priority = -1

    for segment in segments:
        binary, subcmd, flags = _extract_binary_and_subcmd(segment)
        activity = classify_binary(binary, subcmd, flags)
        priority = ACTIVITY_PRIORITY.get(activity, 0)
        if priority > best_priority:
            best_priority = priority
            best_activity = activity

    return best_activity
