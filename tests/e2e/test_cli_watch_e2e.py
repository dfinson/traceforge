"""End-to-end tests for ``traceforge watch`` (issue #85).

``watch`` is the primary daemon: detect frameworks, bind the gate IPC server,
run the governance pipeline. We exercise three lifecycles as real subprocesses:

* **no detection** — exits 0 with a clear message (fast, no pipeline).
* **``--once``** — a bounded run that starts the pipeline, processes existing
  content, and shuts down cleanly with exit 0 (the ``--once``-style teardown the
  DoD calls for).
* **daemon** — via the shared ``watch_daemon`` fixture: it binds the gate IPC
  server and registers the ``_default`` session before we assert.

Argument-grammar failures (Click) round out the contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.e2e._cli import combined_output, run_cli


@pytest.mark.e2e
def test_watch_without_detected_framework_exits_zero(tmp_traceforge_home: Path) -> None:
    # No ~/.claude/projects seeded → nothing to watch → clean exit, not a hang.
    result = run_cli("watch", "--frameworks", "claude")

    assert result.returncode == 0, combined_output(result)
    assert "No frameworks detected" in combined_output(result)


@pytest.mark.e2e
@pytest.mark.slow
def test_watch_once_starts_pipeline_and_exits_clean(tmp_traceforge_home: Path) -> None:
    # Seed the framework so detection succeeds; --once processes existing content
    # (none) and tears the pipeline down, so the daemon returns on its own.
    (tmp_traceforge_home / ".claude" / "projects").mkdir(parents=True, exist_ok=True)

    result = run_cli("watch", "--frameworks", "claude", "--once", "--no-score", timeout=240.0)

    assert result.returncode == 0, combined_output(result)
    assert "Detected 1 framework" in combined_output(result)


@pytest.mark.e2e
@pytest.mark.slow
def test_watch_daemon_binds_gate_ipc(watch_daemon, gate_socket_lookup) -> None:
    assert watch_daemon.is_running(), watch_daemon.output
    assert watch_daemon.system_db.exists()

    # The CLI's operator-facing wiring: it announced the gate server and
    # registered the default session so hook clients can find it.
    assert "Gate IPC server listening" in watch_daemon.output
    assert gate_socket_lookup("_default"), watch_daemon.output


@pytest.mark.e2e
def test_watch_bad_config_path_is_usage_error(tmp_traceforge_home: Path) -> None:
    result = run_cli("watch", "--config", str(tmp_traceforge_home / "nope.yaml"))

    assert result.returncode == 2
    assert "does not exist" in combined_output(result)


@pytest.mark.e2e
def test_watch_unknown_option_is_usage_error(tmp_traceforge_home: Path) -> None:
    result = run_cli("watch", "--not-a-real-option")

    assert result.returncode == 2
    assert "No such option" in combined_output(result)
