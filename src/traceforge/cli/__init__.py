"""Traceforge CLI — Click application entry point."""

from __future__ import annotations

import click

from traceforge.cli.detect import detect
from traceforge.cli.config_cmd import config
from traceforge.cli.watch import watch
from traceforge.cli.score import score
from traceforge.cli.replay import replay
from traceforge.cli.status import status
from traceforge.cli.gate_cmd import gate
from traceforge.cli.init_cmd import init
from traceforge.cli.download_cmd import download_model


@click.group()
@click.version_option(package_name="traceforge")
def main() -> None:
    """Traceforge — governance pipeline for AI coding agents."""


main.add_command(watch)
main.add_command(score)
main.add_command(detect)
main.add_command(replay)
main.add_command(config)
main.add_command(status)
main.add_command(gate)
main.add_command(init)
main.add_command(download_model)
