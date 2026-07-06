"""Status command — show tracemill system state."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import click


_DB_PATH = Path.home() / ".tracemill" / "system.db"


@click.command()
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--db", "db_path", type=click.Path(), default=None, help="Override DB path.")
def status(as_json: bool, db_path: str | None) -> None:
    """Show tracemill system state from the governance database."""
    path = Path(db_path) if db_path else _DB_PATH
    if not path.exists():
        click.echo("No system database found. Has tracemill run yet?")
        sys.exit(1)

    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")

    stats: dict = {}

    # Session count
    row = conn.execute("SELECT COUNT(*) FROM session_state").fetchone()
    stats["active_sessions"] = row[0] if row else 0

    # Processed events
    row = conn.execute("SELECT COUNT(*) FROM processed_events").fetchone()
    stats["processed_events"] = row[0] if row else 0

    # MCP tool profiles
    row = conn.execute("SELECT COUNT(*) FROM mcp_profiles").fetchone()
    stats["mcp_profiles"] = row[0] if row else 0

    # Session summaries
    row = conn.execute("SELECT COUNT(*) FROM session_summaries").fetchone()
    stats["completed_sessions"] = row[0] if row else 0

    # Recent recommendations breakdown
    row = conn.execute(
        "SELECT recommendation_counts_json FROM session_summaries ORDER BY ended_at DESC LIMIT 1"
    ).fetchone()
    stats["last_session_recommendations"] = json.loads(row[0]) if row and row[0] else None

    conn.close()

    if as_json:
        click.echo(json.dumps(stats, indent=2))
    else:
        click.echo("Tracemill System Status")
        click.echo("─" * 40)
        click.echo(f"  Active sessions:    {stats['active_sessions']}")
        click.echo(f"  Processed events:   {stats['processed_events']}")
        click.echo(f"  MCP profiles:       {stats['mcp_profiles']}")
        click.echo(f"  Completed sessions: {stats['completed_sessions']}")
        if stats["last_session_recommendations"]:
            click.echo(f"  Last session recs:  {stats['last_session_recommendations']}")
