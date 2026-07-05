"""tracemill download-model — fetch the titler weights when the extra is absent.

The titler weights ship in the separate ``tracemill-title-model`` package,
normally pulled automatically by ``pip install "tracemill[title]"`` /
``uv add "tracemill[title]"``. This command is the manual fallback: it installs
(or upgrades) that package into the current environment, from PyPI by default or
from the GitHub-release mirror when PyPI is unavailable.
"""

from __future__ import annotations

import subprocess
import sys

import click

#: Model wheel version to target for the GitHub-release mirror. PyPI installs
#: resolve the newest compatible release, so this only pins the ``gh`` fallback.
_MIRROR_VERSION = "0.1.0"
_REPO = "dfinson/tracemill"


def _gh_wheel_url(version: str) -> str:
    whl = f"tracemill_title_model-{version}-py3-none-any.whl"
    return f"https://github.com/{_REPO}/releases/download/title-model-v{version}/{whl}"


@click.command("download-model")
@click.option(
    "--source",
    type=click.Choice(["pypi", "gh"]),
    default="pypi",
    show_default=True,
    help="Where to fetch the titler weights from. 'gh' uses the GitHub-release mirror.",
)
@click.option(
    "--version",
    "version",
    default=_MIRROR_VERSION,
    show_default=True,
    help="Model version to fetch from the GitHub mirror (ignored for --source pypi).",
)
def download_model(source: str, version: str) -> None:
    """Install the tracemill titler weights (``tracemill-title-model``).

    Prefer ``pip install "tracemill[title]"`` — this command exists for when the
    weights are missing at runtime or PyPI is unreachable (``--source gh``).
    """
    target = "tracemill-title-model" if source == "pypi" else _gh_wheel_url(version)
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", target]
    click.echo(f"Installing titler weights from {source}: {target}")
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        raise click.ClickException(
            "pip is not available in this environment. Install the weights with your "
            'package manager instead, e.g.  uv add "tracemill[title]"'
        )
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(f"install failed (exit {exc.returncode}). Target: {target}")

    # Confirm the resolver now sees a complete head, so the message is truthful.
    from tracemill.title._resolve import span_dir

    if span_dir() is None:
        raise click.ClickException(
            "install completed but the span head is still not resolvable; the "
            "environment may need to be restarted or the package is incomplete."
        )
    click.echo("Titler weights installed and resolvable.")
