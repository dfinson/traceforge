"""Tests for the tool-display resolver, registry, and enrichment population.

Covers the general mechanism that fills the ``EventMetadata.tool_display`` stub:
the :class:`ToolDisplayResolver` precedence chain (providers -> static map ->
None), the ``ClassifyConfig.tool_display`` config section + engine
materialization, the config-override chain (consumer overrides win over the
shipped generic defaults), and the Enricher wiring.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from traceforge import Enricher, EventKind, SessionEvent
from traceforge.classify import ToolDisplayProvider, ToolDisplayResolver
from traceforge.classify.config import (
    ClassificationEngine,
    ClassifyConfig,
    _load_builtin_defaults,
    _merge_raw,
    load_config,
)

TS = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


# ── Fixtures / helpers ──


def _start(tool_name: str, tool_call_id: str = "tc-1", session_id: str = "s1") -> SessionEvent:
    return SessionEvent(
        kind=EventKind.TOOL_CALL_STARTED,
        session_id=session_id,
        timestamp=TS,
        payload={"tool_call_id": tool_call_id, "tool_name": tool_name},
    )


def _complete(tool_name: str, tool_call_id: str = "tc-1", session_id: str = "s1") -> SessionEvent:
    return SessionEvent(
        kind=EventKind.TOOL_CALL_COMPLETED,
        session_id=session_id,
        timestamp=TS,
        payload={"tool_call_id": tool_call_id, "tool_name": tool_name},
    )


def _enrich_pair(enricher: Enricher, tool_name: str) -> SessionEvent:
    """Push a start+complete pair and return the emitted (completed) event."""
    assert enricher.process(_start(tool_name)) is None  # start is buffered
    out = enricher.process(_complete(tool_name))
    assert isinstance(out, SessionEvent)
    return out


class _StaticProvider:
    """Test provider that returns a fixed label for one canonical identity."""

    def __init__(self, canonical: str, label: str | None) -> None:
        self._canonical = canonical
        self._label = label

    def display_for(self, canonical: str, raw: str) -> str | None:
        return self._label if canonical == self._canonical else None


# ── ToolDisplayResolver: static map ──


def test_resolver_returns_mapped_label():
    r = ToolDisplayResolver({"view": "read file"})
    assert r.resolve(canonical="view", raw="read_file") == "read file"


def test_resolver_unknown_returns_none():
    r = ToolDisplayResolver({"view": "read file"})
    assert r.resolve(canonical="totally_unknown", raw="totally_unknown") is None


def test_resolver_empty_map_returns_none():
    assert ToolDisplayResolver().resolve(canonical="view", raw="view") is None


def test_resolver_normalizes_keys_case_and_dashes():
    r = ToolDisplayResolver({"Web-Fetch": "fetch url"})
    assert r.resolve(canonical="web_fetch", raw="WebFetch") == "fetch url"


def test_resolver_empty_string_value_is_treated_as_none():
    # A map entry cannot force an empty label onto the stub.
    r = ToolDisplayResolver({"view": ""})
    assert r.resolve(canonical="view", raw="view") is None


# ── ToolDisplayResolver: providers (extension point) ──


def test_provider_wins_over_static_map():
    r = ToolDisplayResolver({"view": "read file"}, [_StaticProvider("view", "CUSTOM")])
    assert r.resolve(canonical="view", raw="read_file") == "CUSTOM"


def test_provider_returning_none_defers_to_map():
    r = ToolDisplayResolver({"view": "read file"}, [_StaticProvider("edit", "X")])
    assert r.resolve(canonical="view", raw="read_file") == "read file"


def test_provider_returning_empty_string_defers():
    r = ToolDisplayResolver({"view": "read file"}, [_StaticProvider("view", "")])
    assert r.resolve(canonical="view", raw="read_file") == "read file"


def test_first_nonempty_provider_wins_in_order():
    providers = [
        _StaticProvider("view", None),
        _StaticProvider("view", "SECOND"),
        _StaticProvider("view", "THIRD"),
    ]
    r = ToolDisplayResolver({"view": "read file"}, providers)
    assert r.resolve(canonical="view", raw="read_file") == "SECOND"


def test_provider_exception_is_swallowed_and_falls_through():
    class _Boom:
        def display_for(self, canonical: str, raw: str) -> str | None:
            raise RuntimeError("boom")

    r = ToolDisplayResolver({"view": "read file"}, [_Boom()])
    assert r.resolve(canonical="view", raw="read_file") == "read file"


def test_provider_protocol_is_runtime_checkable():
    assert isinstance(_StaticProvider("view", "x"), ToolDisplayProvider)


# ── ClassifyConfig + engine materialization ──


def test_default_engine_has_generic_display_map():
    shipped = _load_builtin_defaults()["tool_display"]
    assert shipped, "built-in tool_display defaults must be present"
    # A handful of universal, generic identities are covered.
    for key in ("view", "edit", "create", "shell", "grep"):
        assert key in shipped


def test_engine_normalizes_config_display_keys():
    engine = ClassificationEngine(
        ClassifyConfig(tool_display={"Web-Fetch": "fetch url", "VIEW": "read"})
    )
    assert engine.tool_display["web_fetch"] == "fetch url"
    assert engine.tool_display["view"] == "read"


def test_default_display_map_has_zero_consumer_named_values():
    # Acceptance: the shipped default set is GENERAL — no consumer vocabulary.
    shipped = _load_builtin_defaults()["tool_display"]
    for key, value in shipped.items():
        assert "codeplane" not in key.lower()
        assert "codeplane" not in value.lower()


# ── Config-override chain: consumer overrides win over defaults ──


def test_merge_raw_override_wins_per_key():
    base = {"tool_display": {"view": "read file", "shell": "shell"}}
    override = {"tool_display": {"view": "CUSTOM VIEW"}}
    merged = _merge_raw(base, override)
    # Per-key override wins; non-overridden default is preserved.
    assert merged["tool_display"]["view"] == "CUSTOM VIEW"
    assert merged["tool_display"]["shell"] == "shell"


def test_config_file_override_wins_over_builtin_defaults(tmp_path, monkeypatch):
    # Isolate the discovery chain so only built-in defaults + the explicit file apply.
    monkeypatch.setattr("traceforge.classify.config._find_project_config", lambda: None)
    monkeypatch.setattr("traceforge.classify.config._find_user_config", lambda: None)
    monkeypatch.setattr("traceforge.classify.config._load_entry_point_configs", lambda: [])
    monkeypatch.delenv("TRACEFORGE_CONFIG", raising=False)

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("tool_display:\n  view: CONSUMER READ\n", encoding="utf-8")

    cfg = load_config(config_path=cfg_file)
    # Consumer override wins for its key...
    assert cfg.tool_display["view"] == "CONSUMER READ"
    # ...while untouched built-in generic defaults survive the merge.
    assert cfg.tool_display["shell"] == "shell"


# ── Enricher integration: the stub gets populated ──


def test_enricher_populates_display_for_known_tool():
    enricher = Enricher()
    out = _enrich_pair(enricher, "read_file")  # alias -> canonical "view"
    assert out.metadata.tool_display == "read file"


def test_enricher_shell_alias_maps_to_shell_display():
    enricher = Enricher()
    out = _enrich_pair(enricher, "Bash")  # alias -> canonical "shell"
    assert out.metadata.tool_display == "shell"


def test_enricher_unknown_tool_leaves_display_none():
    enricher = Enricher()
    out = _enrich_pair(enricher, "some_bespoke_unmapped_tool")
    assert out.metadata.tool_display is None


def test_enricher_no_tool_name_leaves_display_none():
    enricher = Enricher()
    # No tool_call_id -> the start is emitted immediately (not buffered).
    ev = SessionEvent(
        kind=EventKind.TOOL_CALL_STARTED,
        session_id="s1",
        timestamp=TS,
        payload={},  # no tool_name, no tool_call_id
    )
    out = enricher.process(ev)
    assert isinstance(out, SessionEvent)
    assert out.metadata.tool_display is None


def test_enricher_display_survives_start_complete_merge():
    # tool_display is set on the START and must be carried onto the merged COMPLETE.
    enricher = Enricher()
    out = _enrich_pair(enricher, "write_file")  # alias -> canonical "create"
    assert out.metadata.tool_display == "create file"


def test_enricher_provider_overrides_config_map():
    enricher = Enricher(tool_display_providers=[_StaticProvider("view", "PROVIDER LABEL")])
    out = _enrich_pair(enricher, "read_file")
    assert out.metadata.tool_display == "PROVIDER LABEL"


def test_enricher_config_override_reflected_in_enrichment(tmp_path, monkeypatch):
    # A merged config (built-in canonical_tools + a tool_display override) drives
    # enrichment: "read_file" normalizes to canonical "view", which the override
    # relabels. Isolate the discovery chain for determinism.
    monkeypatch.setattr("traceforge.classify.config._find_project_config", lambda: None)
    monkeypatch.setattr("traceforge.classify.config._find_user_config", lambda: None)
    monkeypatch.setattr("traceforge.classify.config._load_entry_point_configs", lambda: [])
    monkeypatch.delenv("TRACEFORGE_CONFIG", raising=False)

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("tool_display:\n  view: OVERRIDDEN\n", encoding="utf-8")

    enricher = Enricher(config=load_config(config_path=cfg_file))
    out = _enrich_pair(enricher, "read_file")  # alias -> canonical "view"
    assert out.metadata.tool_display == "OVERRIDDEN"


def test_enricher_display_is_deterministic():
    labels = {_enrich_pair(Enricher(), "grep").metadata.tool_display for _ in range(5)}
    assert labels == {"search"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("read_file", "read file"),
        ("Read", "read file"),
        ("edit_file", "edit file"),
        ("create_file", "create file"),
        ("Bash", "shell"),
        ("powershell", "shell"),
        ("Grep", "search"),
    ],
)
def test_enricher_alias_display_matrix(raw, expected):
    assert _enrich_pair(Enricher(), raw).metadata.tool_display == expected
