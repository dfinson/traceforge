"""End-to-end tests for ``traceforge status`` (issue #85).

``status`` reports governance-DB counters an operator uses to confirm the
pipeline has run. We build a real (empty) ``system.db`` through the production
``SystemStore`` so the schema matches, point ``--db`` at it, and assert the
machine-readable JSON contract cross-platform. The missing-DB path must fail
cleanly (exit 1, not a traceback).

The human-readable table draws a ``─`` rule; the CLI entry point forces UTF-8
output so this succeeds on every platform, including a Windows cp1252 stdout.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e._cli import combined_output, run_cli

_EXPECTED_KEYS = {
    "active_sessions",
    "processed_events",
    "mcp_profiles",
    "completed_sessions",
    "last_session_recommendations",
}


def _build_system_db(home: Path) -> Path:
    """Create an empty but schema-complete governance DB via the real store."""
    from traceforge.governance.persistence import SystemStore

    db_path = home / "system.db"
    store = SystemStore(db_path)  # runs migrations → all tables status queries
    store.close()
    return db_path


@pytest.mark.e2e
def test_status_json_output_reports_zeroed_counters(tmp_traceforge_home: Path) -> None:
    db_path = _build_system_db(tmp_traceforge_home)

    result = run_cli("status", "--json-output", "--db", str(db_path))

    assert result.returncode == 0, combined_output(result)
    stats = json.loads(result.stdout)
    assert set(stats) == _EXPECTED_KEYS
    assert stats["active_sessions"] == 0
    assert stats["processed_events"] == 0
    assert stats["mcp_profiles"] == 0
    assert stats["completed_sessions"] == 0
    assert stats["last_session_recommendations"] is None


@pytest.mark.e2e
def test_status_missing_db_reports_cleanly(tmp_traceforge_home: Path) -> None:
    missing = tmp_traceforge_home / "absent.db"

    result = run_cli("status", "--json-output", "--db", str(missing))

    assert result.returncode == 1
    assert "No system database found" in combined_output(result)


@pytest.mark.e2e
def test_status_plain_table_succeeds(tmp_traceforge_home: Path) -> None:
    db_path = _build_system_db(tmp_traceforge_home)

    result = run_cli("status", "--db", str(db_path))

    assert result.returncode == 0, combined_output(result)
    assert "System Status" in result.stdout


@pytest.mark.e2e
def test_status_unknown_option_is_usage_error(tmp_traceforge_home: Path) -> None:
    result = run_cli("status", "--not-a-real-option")

    assert result.returncode == 2
    assert "No such option" in combined_output(result)
