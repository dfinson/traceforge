"""tracemill gate — CLI command for cross-process gating relay."""

from __future__ import annotations

import click


@click.command("gate")
@click.option("--stdin", "from_stdin", is_flag=True, help="Read event JSON from stdin.")
@click.option(
    "--format",
    "output_format",
    default="claude-code",
    type=click.Choice(["claude-code", "json"]),
    help="Output format for the verdict.",
)
def gate(from_stdin: bool, output_format: str) -> None:
    """Relay a tool call event to the running Pipeline for gating.

    Reads event JSON from stdin, looks up the Pipeline IPC server by session_id,
    sends the event for scoring, and outputs the verdict in the specified format.

    Typically invoked by agent hooks (e.g., Claude Code PreToolUse).
    """
    if not from_stdin:
        raise click.UsageError("--stdin is required (only stdin mode is currently supported)")

    from tracemill.gate.client import gate_from_stdin

    gate_from_stdin(format=output_format)
