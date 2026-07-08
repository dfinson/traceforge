"""Traceforge CLI — Click application entry point."""

from __future__ import annotations

import sys

import click

from traceforge.cli.detect import detect
from traceforge.cli.config_cmd import config
from traceforge.cli.watch import watch
from traceforge.cli.score import score
from traceforge.cli.replay import replay
from traceforge.cli.status import status
from traceforge.cli.gate_cmd import gate
from traceforge.cli.init_cmd import init


@click.group()
@click.version_option(package_name="traceforge-toolkit")
def main() -> None:
    """Traceforge — governance pipeline for AI coding agents."""
    # Force UTF-8 on stdout/stderr so the CLI's Unicode glyphs (box rules,
    # ✓/✗) never raise UnicodeEncodeError on a non-UTF-8 console such as a
    # Windows cp1252 stdout. Guarded because streams may lack reconfigure
    # (e.g. when replaced by an in-memory buffer under test).
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


main.add_command(watch)
main.add_command(score)
main.add_command(detect)
main.add_command(replay)
main.add_command(config)
main.add_command(status)
main.add_command(gate)
main.add_command(init)
