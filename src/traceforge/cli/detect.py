"""Detect command — discover installed AI coding agent frameworks."""

from __future__ import annotations

import json

import click

from traceforge.sources.auto_detect import detect_frameworks


@click.command()
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON array.")
@click.option(
    "--frameworks",
    default=None,
    help="Comma-separated list of frameworks to check (default: all).",
)
def detect(as_json: bool, frameworks: str | None) -> None:
    """Discover installed AI coding agent frameworks."""
    fw_list = [f.strip() for f in frameworks.split(",")] if frameworks else None
    detected = detect_frameworks(fw_list)

    if not detected:
        if as_json:
            click.echo("[]")
        else:
            click.echo("No frameworks detected.")
        return

    if as_json:
        rows = [
            {
                "name": d.name,
                "path": str(d.path),
                "adapter": d.adapter,
                "ingestion_mode": d.ingestion_mode,
            }
            for d in detected
        ]
        click.echo(json.dumps(rows, indent=2))
    else:
        click.echo(f"{'Framework':<12} {'Mode':<12} {'Path'}")
        click.echo("─" * 60)
        for d in detected:
            click.echo(f"{d.name:<12} {d.ingestion_mode:<12} {d.path}")
