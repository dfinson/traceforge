"""End-to-end tests for configuration precedence and ``TRACEFORGE_*`` overrides.

Covers issue #88 (Wave 7a): ``config/loader.py::load_config`` resolves values with
the precedence chain **kwargs > TRACEFORGE_* env > YAML > Pydantic defaults**, the
module singleton is reset via ``reset_config()``, and the resolved config makes a
real round-trip through :meth:`traceforge.sdk.Pipeline.from_config`.

Isolation is load-bearing here, for two reasons the coordinator flagged as the
flakiness risk:

* The config layer is a **module singleton** (``loader._config`` / ``_bootstrapped``).
  Every test calls ``reset_config()`` before and after so no config state leaks
  across tests.
* The loader resolves the ``~/.traceforge`` paths as **module-level constants
  computed at import time** — long before ``tmp_traceforge_home`` patches ``$HOME``.
  The ``config_env`` fixture ``monkeypatch.setattr``s those constants into the
  throwaway home so bootstrap/discovery can never read or write the real
  ``~/.traceforge``.

The matrix tests drive ``TRACEFORGE_CONFIG`` at a written YAML file: that env var
short-circuits file discovery to exactly one file (see ``_find_config_files``), so
each layer under test is controlled and the user/project bootstrap files never
bleed in.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from traceforge.config import loader
from traceforge.config.loader import get_config, load_config, reset_config
from traceforge.config.models import TraceforgeConfig
from traceforge.sdk import Pipeline

pytestmark = pytest.mark.e2e


# ─── Isolation fixture + layering helpers ─────────────────────────────────────


@pytest.fixture
def config_env(tmp_traceforge_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fully isolate the config-singleton surface for one test.

    Builds on ``tmp_traceforge_home`` (isolated ``$HOME``) and additionally:

    * redirects the loader's import-time path constants into the sandbox so
      bootstrap/discovery stay off the real ``~/.traceforge``;
    * scrubs **every** ``TRACEFORGE_*`` env var (``tmp_traceforge_home`` only drops
      ``TRACEFORGE_CONFIG``), so a stray override from the host can't contaminate;
    * calls ``reset_config()`` before *and* after so the singleton never leaks.
    """
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


def _nested(dotted: str, value) -> dict:
    """Expand ``"a.b.c", 1`` into ``{"a": {"b": {"c": 1}}}`` for YAML bodies."""
    out: dict = {}
    cursor = out
    parts = dotted.split(".")
    for part in parts[:-1]:
        cursor[part] = {}
        cursor = cursor[part]
    cursor[parts[-1]] = value
    return out


def _get(cfg: TraceforgeConfig, dotted: str):
    """Read a dotted attribute path off a resolved config object."""
    obj = cfg
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


def _load_layered(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    yaml_data: dict | None = None,
    env: dict[str, str] | None = None,
    kwargs: dict | None = None,
) -> TraceforgeConfig:
    """Write a YAML file, point ``TRACEFORGE_CONFIG`` at it, layer env + kwargs, load.

    ``TRACEFORGE_CONFIG`` makes discovery return exactly this file, so the YAML
    layer is precisely what we wrote — nothing from the sandbox bootstrap leaks in.
    """
    cfg_file = home / "layered.yaml"
    cfg_file.write_text(yaml.safe_dump(yaml_data or {}), encoding="utf-8")
    monkeypatch.setenv("TRACEFORGE_CONFIG", str(cfg_file))
    for key, val in (env or {}).items():
        monkeypatch.setenv(key, val)
    reset_config()
    return load_config(**(kwargs or {}))


# Representative keys spanning the shapes the AC calls out: a threshold int, a
# gate-enable bool, a deeply-nested threshold, and a model-path string. Each YAML
# value is chosen to differ from the Pydantic default so "YAML beats default" is
# an honest assertion. (Defaults: sdk.batch_size=64, score.enabled=True,
# governance.budget.max_tool_calls=None, title...api.model="gpt-4o-mini".)
NESTED_KEYS = [
    ("sdk.batch_size", "TRACEFORGE_SDK__BATCH_SIZE", 16, "128", 128),
    ("score.enabled", "TRACEFORGE_SCORE__ENABLED", False, "true", True),
    (
        "governance.budget.max_tool_calls",
        "TRACEFORGE_GOVERNANCE__BUDGET__MAX_TOOL_CALLS",
        3,
        "9",
        9,
    ),
    (
        "title.session_naming.api.model",
        "TRACEFORGE_TITLE__SESSION_NAMING__API__MODEL",
        "yaml-model",
        "env-model",
        "env-model",
    ),
]


# ─── Scalar precedence: full chain on one top-level key (log_level) ────────────


def test_kwarg_beats_env_beats_yaml_scalar(config_env: Path, monkeypatch: pytest.MonkeyPatch):
    """All four layers set on ``log_level`` -> the explicit kwarg wins."""
    cfg = _load_layered(
        config_env,
        monkeypatch,
        yaml_data={"log_level": "WARNING"},
        env={"TRACEFORGE_LOG_LEVEL": "ERROR"},
        kwargs={"log_level": "DEBUG"},
    )
    assert cfg.log_level == "DEBUG"


def test_env_beats_yaml_scalar(config_env: Path, monkeypatch: pytest.MonkeyPatch):
    """No kwarg -> the ``TRACEFORGE_*`` env override beats the YAML value."""
    cfg = _load_layered(
        config_env,
        monkeypatch,
        yaml_data={"log_level": "WARNING"},
        env={"TRACEFORGE_LOG_LEVEL": "ERROR"},
    )
    assert cfg.log_level == "ERROR"


def test_yaml_beats_default_scalar(config_env: Path, monkeypatch: pytest.MonkeyPatch):
    """No env/kwarg -> YAML beats the Pydantic default (which differs)."""
    cfg = _load_layered(config_env, monkeypatch, yaml_data={"log_level": "WARNING"})
    assert cfg.log_level == "WARNING"
    assert TraceforgeConfig().log_level != "WARNING"  # default is INFO


def test_unset_falls_back_to_default_scalar(config_env: Path, monkeypatch: pytest.MonkeyPatch):
    """Empty YAML, no env/kwarg -> the Pydantic default surfaces."""
    cfg = _load_layered(config_env, monkeypatch, yaml_data={})
    assert cfg.log_level == TraceforgeConfig().log_level == "INFO"


# ─── Nested representative keys: thresholds, gate-enable, model path ───────────


@pytest.mark.parametrize(
    "dotted,env_var,yaml_val,env_str,env_typed",
    NESTED_KEYS,
    ids=[row[0] for row in NESTED_KEYS],
)
def test_env_beats_yaml_nested(
    config_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    dotted: str,
    env_var: str,
    yaml_val,
    env_str: str,
    env_typed,
):
    """For each representative key, the env override beats YAML and is coerced
    to the right scalar type (bool/int/str) by ``_coerce_env_value``."""
    cfg = _load_layered(
        config_env,
        monkeypatch,
        yaml_data=_nested(dotted, yaml_val),
        env={env_var: env_str},
    )
    resolved = _get(cfg, dotted)
    assert resolved == env_typed
    assert type(resolved) is type(env_typed)  # e.g. "false" -> bool, "9" -> int


@pytest.mark.parametrize(
    "dotted,env_var,yaml_val,env_str,env_typed",
    NESTED_KEYS,
    ids=[row[0] for row in NESTED_KEYS],
)
def test_yaml_beats_default_nested(
    config_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    dotted: str,
    env_var: str,
    yaml_val,
    env_str: str,
    env_typed,
):
    """With no env override, the YAML value populates the key and differs from
    the built-in default (proving YAML > defaults)."""
    cfg = _load_layered(config_env, monkeypatch, yaml_data=_nested(dotted, yaml_val))
    assert _get(cfg, dotted) == yaml_val
    assert _get(TraceforgeConfig(), dotted) != yaml_val


@pytest.mark.parametrize(
    "dotted,env_var,yaml_val,env_str,env_typed",
    NESTED_KEYS,
    ids=[row[0] for row in NESTED_KEYS],
)
def test_unset_falls_back_to_default_nested(
    config_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    dotted: str,
    env_var: str,
    yaml_val,
    env_str: str,
    env_typed,
):
    """Empty YAML and no env -> each key falls back to its Pydantic default."""
    cfg = _load_layered(config_env, monkeypatch, yaml_data={})
    assert _get(cfg, dotted) == _get(TraceforgeConfig(), dotted)


def test_kwarg_deep_merge_beats_env_nested(config_env: Path, monkeypatch: pytest.MonkeyPatch):
    """A nested-dict kwarg is deep-merged last, so it beats the env override too."""
    cfg = _load_layered(
        config_env,
        monkeypatch,
        yaml_data={"sdk": {"batch_size": 16}},
        env={"TRACEFORGE_SDK__BATCH_SIZE": "128"},
        kwargs={"sdk": {"batch_size": 999}},
    )
    assert cfg.sdk.batch_size == 999


def test_env_bool_coercion_both_directions(config_env: Path, monkeypatch: pytest.MonkeyPatch):
    """The gate-enable bool coerces from string in both directions ("false"/"true")."""
    off = _load_layered(config_env, monkeypatch, env={"TRACEFORGE_SCORE__ENABLED": "false"})
    assert off.score.enabled is False
    on = _load_layered(config_env, monkeypatch, env={"TRACEFORGE_SCORE__ENABLED": "true"})
    assert on.score.enabled is True


# ─── Singleton reset semantics (the cross-test contamination guard) ───────────


def test_reset_config_prevents_singleton_leak(config_env: Path, monkeypatch: pytest.MonkeyPatch):
    """``get_config`` caches; only ``reset_config`` picks up a changed YAML.

    This is the exact mechanic the coordinator flagged: without ``reset_config``
    the module singleton would carry a prior test's config forward.
    """
    home = config_env
    cfg_file = home / "singleton.yaml"
    monkeypatch.setenv("TRACEFORGE_CONFIG", str(cfg_file))

    cfg_file.write_text(yaml.safe_dump({"sdk": {"batch_size": 16}}), encoding="utf-8")
    reset_config()
    assert load_config().sdk.batch_size == 16
    assert get_config().sdk.batch_size == 16  # cached singleton

    # Rewrite the file but do NOT reset -> the cached singleton is unchanged.
    cfg_file.write_text(yaml.safe_dump({"sdk": {"batch_size": 32}}), encoding="utf-8")
    assert get_config().sdk.batch_size == 16

    # After an explicit reset the next access reloads from disk.
    reset_config()
    assert get_config().sdk.batch_size == 32


# ─── Real round-trip through Pipeline.from_config ─────────────────────────────


def test_pipeline_from_config_env_beats_yaml_project_root(
    config_env: Path, monkeypatch: pytest.MonkeyPatch
):
    """A real ``Pipeline.from_config`` resolves ``governance.project_root`` with
    env beating YAML.

    ``governance._project_root`` is the genuine config-flow observable: the value
    threads config -> ``GovernancePipeline.create`` -> ``instance._project_root``
    (see governance/pipeline.py), so asserting on it proves the resolved config
    actually reached the constructed pipeline — not just the loader dict.
    """
    home = config_env
    cfg_file = home / "roundtrip.yaml"
    cfg_file.write_text(
        yaml.safe_dump({"governance": {"project_root": "yaml-root"}}), encoding="utf-8"
    )
    monkeypatch.setenv("TRACEFORGE_GOVERNANCE__PROJECT_ROOT", "env-root")
    reset_config()

    pipeline = Pipeline.from_config(path=cfg_file)
    assert pipeline.governance._project_root == "env-root"


def test_pipeline_from_config_yaml_beats_default_project_root(
    config_env: Path, monkeypatch: pytest.MonkeyPatch
):
    """With no env override, ``Pipeline.from_config`` carries the YAML value
    (default ``project_root`` is ``None``)."""
    home = config_env
    cfg_file = home / "roundtrip.yaml"
    cfg_file.write_text(
        yaml.safe_dump({"governance": {"project_root": "yaml-root"}}), encoding="utf-8"
    )
    reset_config()

    pipeline = Pipeline.from_config(path=cfg_file)
    assert pipeline.governance._project_root == "yaml-root"
    assert TraceforgeConfig().governance.project_root is None
