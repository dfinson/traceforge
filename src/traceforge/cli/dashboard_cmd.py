"""Dashboard command — serve the local "trace the traces" observability portal.

Launches a read-only local server (``traceforge.dashboard.server``) that serves
the bundled single-page app plus a JSON API over traceforge's two SQLite
databases. Nothing here writes to either database. The lifecycle mirrors
``traceforge.cli.score``: start the server in a daemon thread and block until
Ctrl+C, optionally opening a browser once it is listening.
"""

from __future__ import annotations

import threading
import webbrowser
from pathlib import Path

import click

from traceforge.dashboard.repository import DashboardRepository, resolve_paths
from traceforge.dashboard.server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    BackgroundServer,
    create_server,
    default_static_dir,
)


def _output_db_from_config(config_path: str) -> Path | None:
    """Return the first sqlite sink path declared in a traceforge config file.

    The dashboard reads whatever the ``sqlite`` output sink writes; when a user
    points ``--config`` at their traceforge config we honour that sink's path
    instead of the well-known default.
    """
    from traceforge.config.loader import load_config_from_path
    from traceforge.config.models import SqliteSinkConfig

    config = load_config_from_path(config_path)
    for pipeline in config.pipelines:
        for sink in pipeline.sinks:
            if isinstance(sink, SqliteSinkConfig):
                return Path(sink.path).expanduser()
    return None


@click.command()
@click.option(
    "--output-db",
    "output_db",
    type=click.Path(),
    default=None,
    help="Output-sink SQLite DB (default: ~/.traceforge/traceforge.db).",
)
@click.option(
    "--system-db",
    "system_db",
    type=click.Path(),
    default=None,
    help="Governance system.db (default: ~/.traceforge/system.db).",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=None,
    help="Read the output DB path from a traceforge config file (--output-db wins).",
)
@click.option("--host", default=DEFAULT_HOST, help=f"Bind host (default: {DEFAULT_HOST}).")
@click.option(
    "--port", default=DEFAULT_PORT, type=int, help=f"Bind port (default: {DEFAULT_PORT})."
)
@click.option(
    "--open/--no-open",
    "open_browser",
    default=True,
    help="Open the dashboard in a browser once it is listening (default: --open).",
)
def dashboard(
    output_db: str | None,
    system_db: str | None,
    config_path: str | None,
    host: str,
    port: int,
    open_browser: bool,
) -> None:
    """Serve the traceforge dashboard (read-only) over the local SQLite databases."""
    # Explicit --output-db wins; otherwise fall back to a config file's sqlite
    # sink, then to the well-known default (resolved inside resolve_paths).
    resolved_output: str | Path | None = output_db
    if resolved_output is None and config_path is not None:
        resolved_output = _output_db_from_config(config_path)

    paths = resolve_paths(output_db=resolved_output, system_db=system_db)
    repository = DashboardRepository(paths)
    static_dir = default_static_dir()

    if not static_dir.is_dir():
        click.echo("Warning: dashboard assets are not bundled in this install.", err=True)
        click.echo(
            "  Build them from a source checkout: python scripts/build_dashboard.py", err=True
        )

    server = create_server(repository, host=host, port=port, static_dir=static_dir)
    bg = BackgroundServer(server).start()
    url = f"http://{bg.host}:{bg.port}"

    click.echo(f"traceforge dashboard serving on {url}")
    click.echo(_db_line("output DB", paths.output_db, missing_note="missing — empty state"))
    click.echo(
        _db_line(
            "system DB",
            paths.system_db,
            missing_note="missing — governance memory disabled (degraded mode)",
        )
    )
    click.echo("Press Ctrl+C to stop.")

    if open_browser:
        # The socket is already bound and listening (bind happens at construction),
        # so connections initiated by the browser will be accepted immediately.
        webbrowser.open(url)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        click.echo("\nShutting down...")
        bg.stop()


def _db_line(label: str, path: Path, *, missing_note: str) -> str:
    suffix = "" if path.exists() else f" ({missing_note})"
    return f"  {label}: {path}{suffix}"
