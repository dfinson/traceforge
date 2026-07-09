"""Unit tests for the ``traceforge dashboard`` CLI command (no server I/O).

Covers command registration on the Click group, ``--help`` option surface, the
``--config`` -> sqlite-sink path resolution, and the DB-status echo helper. The
actual HTTP serving is exercised by ``tests/e2e/test_dashboard_server.py``.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from traceforge.cli import main
from traceforge.cli.dashboard_cmd import _db_line, _output_db_from_config
from traceforge.config.models import (
    FileWatchSourceConfig,
    JsonlSinkConfig,
    MappedJsonAdapterConfig,
    PipelineConfig,
    SqliteSinkConfig,
    TraceforgeConfig,
)


def _config(sinks: list) -> TraceforgeConfig:
    return TraceforgeConfig(
        pipelines=[
            PipelineConfig(
                name="p",
                source=FileWatchSourceConfig(path="/tmp/a.jsonl"),
                adapter=MappedJsonAdapterConfig(mapping="copilot"),
                sinks=sinks,
            )
        ]
    )


def test_dashboard_is_registered_on_group() -> None:
    assert "dashboard" in main.commands


def test_dashboard_help_lists_options() -> None:
    result = CliRunner().invoke(main, ["dashboard", "--help"])
    assert result.exit_code == 0
    for flag in ("--output-db", "--system-db", "--config", "--host", "--port", "--no-open"):
        assert flag in result.output


def test_output_db_from_config_returns_first_sqlite_path(monkeypatch) -> None:
    cfg = _config([JsonlSinkConfig(path="/x.jsonl"), SqliteSinkConfig(path="~/custom/traces.db")])
    # The command imports load_config_from_path lazily from the loader module, so
    # patch it at the source; the call-time `from ... import` picks up the patch.
    monkeypatch.setattr("traceforge.config.loader.load_config_from_path", lambda _p: cfg)
    assert _output_db_from_config("ignored.yaml") == Path("~/custom/traces.db").expanduser()


def test_output_db_from_config_none_when_no_sqlite_sink(monkeypatch) -> None:
    cfg = _config([JsonlSinkConfig(path="/only.jsonl")])
    monkeypatch.setattr("traceforge.config.loader.load_config_from_path", lambda _p: cfg)
    assert _output_db_from_config("ignored.yaml") is None


def test_db_line_marks_missing_and_present(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    line = _db_line("output DB", missing, missing_note="empty state")
    assert str(missing) in line
    assert "empty state" in line

    present = tmp_path / "there.db"
    present.write_text("x", encoding="utf-8")
    assert _db_line("output DB", present, missing_note="empty state") == f"  output DB: {present}"
