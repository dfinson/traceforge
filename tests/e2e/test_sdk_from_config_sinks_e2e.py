"""End-to-end tests for SDK sink hydration in ``Pipeline.from_config`` (issue #116).

Closes the SDK/CLI asymmetry: a declarative ``traceforge.yaml`` that lists sinks now
hydrates them onto the SDK backbone when ``sinks=`` is **omitted**, while a
programmatic ``sinks=`` — including an explicit empty list ``[]`` — still wins verbatim
with no auto-hydration.

Isolation mirrors ``test_config_precedence_e2e``: the config layer is a module
singleton whose ``~/.traceforge`` paths are import-time constants, so the fixture
redirects those into a throwaway home, scrubs every ``TRACEFORGE_*`` var, and resets
the singleton before and after each test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from traceforge.config import loader
from traceforge.config.loader import reset_config
from traceforge.sdk import Pipeline
from traceforge.sinks.console import ConsoleSink
from traceforge.sinks.sqlite_output import SqliteOutputSink

pytestmark = pytest.mark.e2e


@pytest.fixture
def config_env(tmp_traceforge_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fully isolate the config-singleton surface for one test (see module docstring)."""
    tf_dir = tmp_traceforge_home / ".traceforge"
    monkeypatch.setattr(loader, "_USER_CONFIG_DIR", tf_dir)
    monkeypatch.setattr(loader, "_USER_MAPPINGS_DIR", tf_dir / "mappings")
    monkeypatch.setattr(loader, "_USER_CONFIG_FILE", tf_dir / "config.yaml")
    monkeypatch.setattr(loader, "_PROJECT_CONFIG_FILE", tmp_traceforge_home / "traceforge.yaml")

    for key in [k for k in os.environ if k.startswith("TRACEFORGE_")]:
        monkeypatch.delenv(key, raising=False)

    reset_config()
    yield tmp_traceforge_home
    reset_config()


def _write_config(home: Path) -> Path:
    """Write a config declaring one pipeline whose sinks are console + sqlite."""
    cfg = {
        "pipelines": [
            {
                "name": "declared",
                "source": {"type": "replay", "path": (home / "session.jsonl").as_posix()},
                "adapter": {"type": "otel_span"},
                "sinks": [
                    {"type": "console"},
                    {"type": "sqlite", "path": (home / "out.db").as_posix()},
                ],
            }
        ]
    }
    cfg_file = home / "traceforge.yaml"
    cfg_file.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return cfg_file


def test_from_config_omitted_sinks_hydrates_from_yaml(config_env: Path):
    cfg_file = _write_config(config_env)

    pipe = Pipeline.from_config(path=cfg_file)

    assert [type(s) for s in pipe.backbone._sinks] == [ConsoleSink, SqliteOutputSink]


def test_from_config_explicit_sinks_override_yaml(config_env: Path):
    cfg_file = _write_config(config_env)
    override = ConsoleSink()

    pipe = Pipeline.from_config(path=cfg_file, sinks=[override])

    # Programmatic sinks win: exactly the passed instance, no YAML hydration.
    assert pipe.backbone._sinks == [override]


def test_from_config_explicit_empty_sinks_honored(config_env: Path):
    cfg_file = _write_config(config_env)

    pipe = Pipeline.from_config(path=cfg_file, sinks=[])

    # Explicit empty list is honored as "no sinks" — distinct from omitted (None).
    assert pipe.backbone._sinks == []
