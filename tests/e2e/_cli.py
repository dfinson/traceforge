"""Shared subprocess driver for the CLI e2e stories (issue #85).

Every CLI test runs ``python -m traceforge <cmd>`` as a *real* subprocess — the
operator entry point — and asserts its exit code and stdout/stderr contract.
Isolation is provided by the ``tmp_traceforge_home`` fixture (see
``tests/e2e/conftest.py``): it patches ``$HOME``/``%USERPROFILE%`` (and the
framework-detection env vars) in this pytest process, and ``subprocess.run``
inherits that patched environment, so anything the child reads under
``~/.traceforge`` / ``~/.claude`` lands in the sandbox.

The parent decodes the child's streams as UTF-8 with ``errors="replace"`` so a
child that itself fails to encode Unicode to a cp1252 console (a real Windows
bug these tests pin) still yields capturable output instead of blowing up the
harness.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

#: Generous ceiling for fast Click-validation invocations (they finish in ~2s);
#: heavy commands that load the ML pipeline pass their own larger timeout.
DEFAULT_TIMEOUT = 60.0


def run_cli(
    *args: str,
    cwd: str | Path | None = None,
    stdin: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """Invoke ``python -m traceforge`` with ``args`` and capture the result.

    Args:
        args: CLI arguments after ``traceforge`` (e.g. ``"detect", "--json-output"``).
        cwd: Working directory for the child (defaults to the current one). Set it
            to the sandbox home when a command probes ``Path.cwd()`` (``detect``'s
            aider check) so detection stays deterministic.
        stdin: Optional text piped to the child's stdin.
        timeout: Hard wall-clock ceiling so a hung daemon can never wedge the suite.

    Returns:
        The completed process with ``.returncode``, ``.stdout``, ``.stderr``.
    """
    cmd: list[str] = [sys.executable, "-m", "traceforge", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd) if cwd is not None else None,
        input=stdin,
        timeout=timeout,
    )


def combined_output(result: subprocess.CompletedProcess[str]) -> str:
    """stdout + stderr joined, for asserting on a message regardless of stream."""
    return f"{result.stdout}\n{result.stderr}"
