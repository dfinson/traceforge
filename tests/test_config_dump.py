"""Tests for ``traceforge config dump`` — the resolved effective-config export (#115).

``show`` prints the *raw* config file; ``dump`` must run the real loader
(``traceforge.config.loader.load_config``) so its output reflects the full
precedence merge kwargs > env > project YAML > user YAML > defaults. These tests
drive the command in-process with click's ``CliRunner`` and prove the merge —
not a raw file read — is what gets serialized.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from traceforge.cli.config_cmd import config
from traceforge.config import TraceforgeConfig, reset_config
from traceforge.config import loader as loader_mod


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch: pytest.MonkeyPatch):
    """Reset the loader singleton and scrub ``TRACEFORGE_*`` env for determinism.

    Stray ``TRACEFORGE_*`` vars in the ambient environment would otherwise leak
    into the precedence merge and make assertions non-deterministic.
    """
    reset_config()
    for key in list(os.environ):
        if key.startswith("TRACEFORGE_"):
            monkeypatch.delenv(key, raising=False)
    yield
    reset_config()


def _write_yaml(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _invoke(*args: str):
    result = CliRunner().invoke(config, ["dump", *args])
    assert result.exit_code == 0, result.output
    return result


# ─── The key distinction from `show`: env + file overrides are merged ────────


def test_dump_applies_env_override_over_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Env vars win over the file, and defaults fill the rest — a real merge."""
    cfg_file = _write_yaml(tmp_path / "traceforge.yaml", {"log_level": "WARNING"})
    monkeypatch.setenv("TRACEFORGE_LOG_LEVEL", "ERROR")

    result = _invoke("--config", str(cfg_file))
    resolved = yaml.safe_load(result.output)

    # Env override beat the file's WARNING → this is the effective value, not raw.
    assert resolved["log_level"] == "ERROR"
    # Defaults were materialized for keys absent from the raw file — `show` would
    # never surface these, proving dump serializes the merged typed config.
    assert "sdk" in resolved and resolved["sdk"]["batch_size"] == 64
    assert "governance" in resolved and resolved["governance"]["pii_scanning"] is True
    # And the output genuinely differs from the raw file content.
    assert resolved != yaml.safe_load(cfg_file.read_text(encoding="utf-8"))


def test_dump_reflects_file_override(tmp_path: Path):
    """File values override defaults; unspecified fields keep their defaults."""
    cfg_file = _write_yaml(
        tmp_path / "traceforge.yaml",
        {"log_level": "DEBUG", "sdk": {"batch_size": 128}},
    )

    resolved = yaml.safe_load(_invoke("--config", str(cfg_file)).output)

    assert resolved["log_level"] == "DEBUG"  # from file
    assert resolved["sdk"]["batch_size"] == 128  # from file
    assert resolved["sdk"]["flush_interval"] == 5.0  # default filled around it


def test_dump_nested_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Nested ``TRACEFORGE_*__*`` env overrides land in the resolved output."""
    cfg_file = _write_yaml(tmp_path / "traceforge.yaml", {"sdk": {"batch_size": 128}})
    monkeypatch.setenv("TRACEFORGE_SDK__BATCH_SIZE", "256")

    resolved = yaml.safe_load(_invoke("--config", str(cfg_file)).output)

    assert resolved["sdk"]["batch_size"] == 256  # env beat the file's 128


# ─── Both formats parse and round-trip to the same resolved dict ─────────────


def test_dump_yaml_and_json_agree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg_file = _write_yaml(tmp_path / "traceforge.yaml", {"log_level": "WARNING"})
    monkeypatch.setenv("TRACEFORGE_SDK__BATCH_SIZE", "256")

    yaml_out = _invoke("--config", str(cfg_file), "--format", "yaml").output
    json_out = _invoke("--config", str(cfg_file), "--format", "json").output

    from_yaml = yaml.safe_load(yaml_out)
    from_json = json.loads(json_out)

    assert from_yaml == from_json
    # Both reflect the merged values (env override + file), not the raw file.
    assert from_json["sdk"]["batch_size"] == 256


def test_dump_defaults_to_yaml(tmp_path: Path):
    cfg_file = _write_yaml(tmp_path / "traceforge.yaml", {"log_level": "WARNING"})

    result = _invoke("--config", str(cfg_file))

    # Default format is YAML: parseable as YAML, and not the JSON-object form.
    assert yaml.safe_load(result.output)["log_level"] == "WARNING"
    assert not result.output.lstrip().startswith("{")


def test_dump_json_is_valid_json(tmp_path: Path):
    cfg_file = _write_yaml(tmp_path / "traceforge.yaml", {"log_level": "WARNING"})

    result = _invoke("--config", str(cfg_file), "--format", "json")

    parsed = json.loads(result.output)  # raises if not valid JSON
    assert parsed["log_level"] == "WARNING"


# ─── `--config PATH` targets the given file ──────────────────────────────────


def test_dump_config_targets_given_file(tmp_path: Path):
    file_a = _write_yaml(tmp_path / "a.yaml", {"log_level": "DEBUG"})
    file_b = _write_yaml(tmp_path / "b.yaml", {"log_level": "ERROR"})

    resolved_a = yaml.safe_load(_invoke("--config", str(file_a)).output)
    resolved_b = yaml.safe_load(_invoke("--config", str(file_b)).output)

    assert resolved_a["log_level"] == "DEBUG"
    assert resolved_b["log_level"] == "ERROR"


def test_dump_missing_config_path_is_usage_error():
    """A non-existent ``--config`` path is a click usage error (exit 2)."""
    result = CliRunner().invoke(config, ["dump", "--config", "does-not-exist.yaml"])

    assert result.exit_code == 2
    assert "does not exist" in result.output


# ─── With no config file present, dump prints resolved defaults (no crash) ───


def test_dump_defaults_when_no_config(monkeypatch: pytest.MonkeyPatch):
    """No discoverable file → the resolved DEFAULT config, not an error."""
    # Make discovery find nothing and suppress user-dir bootstrap so no ambient
    # ~/.traceforge/config.yaml (which sets non-default mappings_dirs) interferes.
    monkeypatch.setattr(loader_mod, "_find_config_files", lambda: [])
    monkeypatch.setattr(loader_mod, "_bootstrapped", True)

    result = _invoke()  # no --config

    resolved = yaml.safe_load(result.output)
    assert resolved == TraceforgeConfig().model_dump(mode="json")
    assert resolved["log_level"] == "INFO"  # the built-in default
