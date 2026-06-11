"""Tracemill CLI — Click application entry point."""

from __future__ import annotations

import click

from tracemill.cli.detect import detect
from tracemill.cli.config_cmd import config
from tracemill.cli.watch import watch
from tracemill.cli.score import score
from tracemill.cli.replay import replay
from tracemill.cli.status import status


@click.group()
@click.version_option(package_name="tracemill")
def main() -> None:
    """Tracemill — governance pipeline for AI coding agents."""


main.add_command(watch)
main.add_command(score)
main.add_command(detect)
main.add_command(replay)
main.add_command(config)
main.add_command(status)
