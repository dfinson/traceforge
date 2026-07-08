"""traceforge gate — CLI command for cross-process gating relay."""

from __future__ import annotations

import click

#: CLI / editor agents that expose an injectable, blocking preflight hook. The
#: injector (``traceforge init``) writes ``gate --stdin --agent <name>`` into each
#: agent's hook config, and ``--agent`` selects the matching deny-contract dialect
#: (JSON shape + exit code) — see ``traceforge.gate.client._output_deny``.
SUPPORTED_AGENTS: tuple[str, ...] = (
    "claude-code",
    "copilot-cli",
    "codex",
    "gemini",
    "cline",
    "cursor",
    "amazon-q",
    "opencode",
    "openhands",
)


@click.command("gate")
@click.option("--stdin", "from_stdin", is_flag=True, help="Read event JSON from stdin.")
@click.option(
    "--agent",
    "agent",
    default=None,
    type=click.Choice(SUPPORTED_AGENTS),
    help="Translate the verdict into this agent's native hook deny contract "
    "(shape + exit code). Defaults to the Claude Code dialect.",
)
@click.option(
    "--format",
    "output_format",
    default=None,
    type=click.Choice(["claude-code", "json"]),
    help='Output format for the verdict (legacy; prefer --agent). "json" emits the raw verdict.',
)
def gate(from_stdin: bool, agent: str | None, output_format: str | None) -> None:
    """Relay a tool call event to the running Pipeline for gating.

    Reads event JSON from stdin, looks up the Pipeline IPC server by session_id,
    sends the event for scoring, and outputs the verdict in the target agent's
    native hook dialect.

    Typically invoked by agent hooks (e.g. Claude Code PreToolUse, Copilot CLI
    preToolUse, Codex PreToolUse). ``--agent`` picks the deny contract; when it is
    omitted the Claude Code dialect is used (also selectable via ``--format``).
    """
    if not from_stdin:
        raise click.UsageError("--stdin is required (only stdin mode is currently supported)")

    # --agent is the primary selector; --format stays for backward compatibility.
    dialect = agent or output_format or "claude-code"

    from traceforge.gate.client import gate_from_stdin

    gate_from_stdin(format=dialect)
