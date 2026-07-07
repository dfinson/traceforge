"""Config command group — init, show, validate, dump."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click
import yaml

from traceforge.config.defaults import DEFAULT_CONFIG_YAML


_DEFAULT_CONFIG_PATH = Path.home() / ".traceforge" / "config.yaml"


@click.group()
def config() -> None:
    """Manage traceforge configuration."""


@config.command()
@click.option("--force", is_flag=True, help="Overwrite existing config file.")
def init(force: bool) -> None:
    """Write default config to ~/.traceforge/config.yaml."""
    if _DEFAULT_CONFIG_PATH.exists() and not force:
        click.echo(f"Config already exists: {_DEFAULT_CONFIG_PATH}")
        click.echo("Use --force to overwrite.")
        sys.exit(1)

    _DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _DEFAULT_CONFIG_PATH.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
    click.echo(f"Wrote default config to {_DEFAULT_CONFIG_PATH}")


@config.command()
@click.option("--config", "config_path", type=click.Path(exists=True), default=None)
def show(config_path: str | None) -> None:
    """Print the effective merged configuration."""
    path = Path(config_path) if config_path else _resolve_config_path()
    if path is None or not path.exists():
        click.echo("No config file found. Run `traceforge config init` to create one.")
        sys.exit(1)

    content = path.read_text(encoding="utf-8")
    click.echo(f"# Source: {path}\n")
    click.echo(content)


@config.command()
@click.option("--config", "config_path", type=click.Path(exists=True), default=None)
def validate(config_path: str | None) -> None:
    """Validate config file without running."""
    path = Path(config_path) if config_path else _resolve_config_path()
    if path is None or not path.exists():
        click.echo("No config file found.")
        sys.exit(1)

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Config must be a YAML mapping")
    except Exception as exc:
        click.echo(f"✗ Config invalid: {exc}", err=True)
        sys.exit(1)

    # Emit the success line outside the try so a valid config is never
    # misreported as invalid if the echo itself were to fail.
    click.echo(f"✓ Config valid: {path}")


@config.command()
@click.option("--config", "config_path", type=click.Path(exists=True), default=None)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["yaml", "json"]),
    default="yaml",
    help="Serialization format for the resolved config.",
)
def dump(config_path: str | None, output_format: str) -> None:
    """Print the fully-resolved, effective configuration.

    Unlike `show` (which prints the raw config file), `dump` runs the real
    loader — applying the full precedence chain kwargs > env > project YAML >
    user YAML > defaults — and serializes the typed `TraceforgeConfig` it
    produces. With no config file present, the resolved defaults are dumped.
    """
    from traceforge.config.loader import load_config

    if config_path:
        # Point the real loader at the requested file via its runtime path
        # override (TRACEFORGE_CONFIG), so the full precedence chain still
        # applies — env vars and defaults still layer around the file.
        previous = os.environ.get("TRACEFORGE_CONFIG")
        os.environ["TRACEFORGE_CONFIG"] = str(Path(config_path))
        try:
            cfg = load_config()
        finally:
            if previous is None:
                os.environ.pop("TRACEFORGE_CONFIG", None)
            else:
                os.environ["TRACEFORGE_CONFIG"] = previous
    else:
        cfg = load_config()

    resolved = cfg.model_dump(mode="json")

    if output_format == "json":
        click.echo(json.dumps(resolved, indent=2))
    else:
        click.echo(yaml.safe_dump(resolved, sort_keys=False))


def _resolve_config_path() -> Path | None:
    """Find config file via env var, local, or default location."""
    env = os.environ.get("TRACEFORGE_CONFIG")
    if env:
        return Path(env)

    local = Path("traceforge.yaml")
    if local.exists():
        return local

    if _DEFAULT_CONFIG_PATH.exists():
        return _DEFAULT_CONFIG_PATH

    return None
