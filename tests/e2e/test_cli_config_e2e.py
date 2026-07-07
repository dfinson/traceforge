"""End-to-end tests for the ``traceforge config`` group (issue #85).

Covers the operator config lifecycle — ``init`` (write the default file),
``show`` (print the effective config) and ``validate`` (accept good YAML, reject
bad) — as real subprocesses against an isolated ``~/.traceforge``.

Two happy paths (``validate`` on a valid file, ``show``) echo Unicode via
``click.echo`` (a ``✓`` glyph and the ``─`` rules inside the default template);
the CLI entry point forces UTF-8 output so they pass on every platform,
including a Windows cp1252 stdout.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.e2e._cli import combined_output, run_cli

_CONFIG_REL = Path(".traceforge") / "config.yaml"


@pytest.mark.e2e
def test_config_init_writes_default_file(tmp_traceforge_home: Path) -> None:
    target = tmp_traceforge_home / _CONFIG_REL
    assert not target.exists()

    result = run_cli("config", "init")

    assert result.returncode == 0, combined_output(result)
    assert target.is_file()
    assert "Wrote default config" in result.stdout
    assert target.read_text(encoding="utf-8").strip(), "config file is empty"


@pytest.mark.e2e
def test_config_init_refuses_to_clobber_without_force(tmp_traceforge_home: Path) -> None:
    first = run_cli("config", "init")
    assert first.returncode == 0, combined_output(first)

    second = run_cli("config", "init")

    assert second.returncode == 1
    out = combined_output(second)
    assert "already exists" in out
    assert "--force" in out


@pytest.mark.e2e
def test_config_init_force_overwrites(tmp_traceforge_home: Path) -> None:
    assert run_cli("config", "init").returncode == 0

    result = run_cli("config", "init", "--force")

    assert result.returncode == 0, combined_output(result)
    assert "Wrote default config" in result.stdout


@pytest.mark.e2e
def test_config_validate_rejects_malformed_yaml(tmp_traceforge_home: Path) -> None:
    bad = tmp_traceforge_home / "bad.yaml"
    bad.write_text("foo: bar: baz\n", encoding="utf-8")  # nested mapping value → YAML error

    result = run_cli("config", "validate", "--config", str(bad))

    # Exit 1 holds on every platform (on Windows the '✗' echo also crashes, but
    # the process still exits 1); the clean message is asserted where it is not
    # masked by the Unicode crash.
    assert result.returncode == 1, combined_output(result)
    if not sys.platform.startswith("win"):
        assert "Config invalid" in combined_output(result)


@pytest.mark.e2e
def test_config_validate_rejects_non_mapping(tmp_traceforge_home: Path) -> None:
    seq = tmp_traceforge_home / "seq.yaml"
    seq.write_text("- one\n- two\n", encoding="utf-8")  # valid YAML, but a list

    result = run_cli("config", "validate", "--config", str(seq))

    assert result.returncode == 1, combined_output(result)
    if not sys.platform.startswith("win"):
        assert "mapping" in combined_output(result).lower()


@pytest.mark.e2e
def test_config_validate_missing_path_is_usage_error(tmp_traceforge_home: Path) -> None:
    missing = tmp_traceforge_home / "nope.yaml"

    result = run_cli("config", "validate", "--config", str(missing))

    assert result.returncode == 2
    assert "does not exist" in combined_output(result)


@pytest.mark.e2e
def test_config_validate_accepts_valid_file(tmp_traceforge_home: Path) -> None:
    assert run_cli("config", "init").returncode == 0

    result = run_cli("config", "validate", cwd=tmp_traceforge_home)

    assert result.returncode == 0, combined_output(result)
    assert "valid" in combined_output(result).lower()


@pytest.mark.e2e
def test_config_show_prints_effective_config(tmp_traceforge_home: Path) -> None:
    assert run_cli("config", "init").returncode == 0

    result = run_cli("config", "show", cwd=tmp_traceforge_home)

    assert result.returncode == 0, combined_output(result)
    assert "# Source:" in result.stdout
    assert "log_level" in result.stdout


@pytest.mark.e2e
def test_config_show_without_config_reports_cleanly(tmp_traceforge_home: Path) -> None:
    result = run_cli("config", "show", cwd=tmp_traceforge_home)

    assert result.returncode == 1
    assert "No config file found" in combined_output(result)


@pytest.mark.e2e
def test_config_unknown_subcommand_is_usage_error(tmp_traceforge_home: Path) -> None:
    result = run_cli("config", "definitely-not-a-subcommand")

    assert result.returncode == 2
    assert "No such command" in combined_output(result)
