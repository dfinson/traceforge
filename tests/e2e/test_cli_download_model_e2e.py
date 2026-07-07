"""End-to-end tests for ``traceforge download-model`` (issue #85).

This command (re)installs the titler weights, optionally from a GitHub-release
mirror (``--source gh``). The DoD is explicit: assert argument handling only and
**never hit the network**. So we drive the real subprocess for the arg grammar
(help text + choice validation, which Click resolves before any install runs)
and assert the ``gh`` mirror *target URL* in-process via the command's own URL
builder — proving the ``--source gh`` path without downloading anything.
"""

from __future__ import annotations

import pytest

from tests.e2e._cli import combined_output, run_cli


@pytest.mark.e2e
def test_download_model_help_lists_sources() -> None:
    result = run_cli("download-model", "--help")

    assert result.returncode == 0, combined_output(result)
    out = result.stdout
    assert "--source" in out
    assert "pypi" in out and "gh" in out
    assert "--version" in out


@pytest.mark.e2e
def test_download_model_rejects_unknown_source() -> None:
    # Click validates the choice before the callback, so no install is attempted.
    result = run_cli("download-model", "--source", "not-a-source")

    assert result.returncode == 2
    assert "Invalid value for '--source'" in combined_output(result)


@pytest.mark.e2e
def test_download_model_gh_mirror_url_shape() -> None:
    """The ``--source gh`` path resolves to the pinned GitHub-release wheel URL.

    Asserted through the command's own builder (no subprocess, no network) so the
    mirror target is verified without triggering a real install/download.
    """
    from traceforge.cli.download_cmd import _MIRROR_VERSION, _REPO, _gh_wheel_url

    url = _gh_wheel_url(_MIRROR_VERSION)

    assert url.startswith(f"https://github.com/{_REPO}/releases/download/")
    assert _MIRROR_VERSION in url
    assert url.endswith(".whl")
