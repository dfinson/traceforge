"""Golden e2e tests over REAL captured framework traces.

Unlike the hand-written fixtures elsewhere in the suite (which encode
traceforge's own post-preprocessor assumptions), these tests feed *verbatim*
native framework output — captured/derived by ``scripts/capture_traces/`` —
through the real adapter for each framework. This is the layer that catches
upstream drift: if a framework changes its on-disk/native shape, the captured
trace changes and these assertions move with reality.

Layout::

    tests/fixtures/raw_traces/<framework>/
        <scenario>.jsonl           # one native record per line
        <scenario>.expected.json   # golden snapshot (event_count + kind histogram)
        meta.yaml                  # provenance (captured vs schema-derived)

A framework directory is matched to the mapping
``src/traceforge/mappings/<framework>.yaml`` by name. Each ``*.jsonl`` file is a
distinct *scenario* and is discovered/parametrized independently, so a framework
may carry many scenarios. Add coverage by committing a new ``<scenario>.jsonl``
plus its ``<scenario>.expected.json``; discovery picks it up automatically.

Adapter selection mirrors production: a mapping written against ``spans:`` (OTel)
is driven by :class:`OtelSpanAdapter`; everything else is a JSON-line mapping
driven by :class:`MappedJsonAdapter` (preprocessor + YAML).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from traceforge.adapters.mapped_json import MappedJsonAdapter
from traceforge.adapters.otel import OtelSpanAdapter
from traceforge.types import EventKind, SessionEvent

REPO_ROOT = Path(__file__).resolve().parents[2]
TRACES_ROOT = REPO_ROOT / "tests" / "fixtures" / "raw_traces"
MAPPINGS_DIR = REPO_ROOT / "src" / "traceforge" / "mappings"

SNAPSHOT_SUFFIX = ".expected.json"


def _mapping_data(framework: str) -> tuple[Path, dict[str, Any]]:
    yaml_path = MAPPINGS_DIR / f"{framework}.yaml"
    assert yaml_path.exists(), f"no mapping for captured framework {framework!r}"
    return yaml_path, yaml.safe_load(yaml_path.read_text(encoding="utf-8"))


def _make_adapter(framework: str):
    """Return the adapter the framework's mapping is written for.

    OTel span mappings (``spans:`` with no ``events:``) are consumed by
    :class:`OtelSpanAdapter`; JSON-line mappings by :class:`MappedJsonAdapter`.
    """
    yaml_path, data = _mapping_data(framework)
    if data.get("spans") and not data.get("events"):
        return OtelSpanAdapter(
            ingestion_mode=data.get("ingestion_mode", "stream"),
            session_id=f"golden-{framework}",
        )
    return MappedJsonAdapter.from_yaml(str(yaml_path), session_id=f"golden-{framework}")


def _parse_scenario(framework: str, jsonl_path: Path) -> list[SessionEvent]:
    """Replay one scenario file through its framework's adapter."""
    adapter = _make_adapter(framework)
    events: list[SessionEvent] = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.extend(adapter.parse(line))
    return events


def _discover_scenarios() -> list[tuple[str, Path]]:
    if not TRACES_ROOT.is_dir():
        return []
    scenarios: list[tuple[str, Path]] = []
    for framework_dir in sorted(TRACES_ROOT.iterdir()):
        if not framework_dir.is_dir():
            continue
        for jsonl in sorted(framework_dir.glob("*.jsonl")):
            scenarios.append((framework_dir.name, jsonl))
    return scenarios


def _kind_histogram(events: list[SessionEvent]) -> dict[str, int]:
    hist: dict[str, int] = {}
    for event in events:
        hist[event.kind] = hist.get(event.kind, 0) + 1
    return dict(sorted(hist.items()))


def _snapshot(events: list[SessionEvent]) -> dict[str, Any]:
    """Order-independent golden summary: total count + per-kind histogram."""
    return {"event_count": len(events), "kinds": _kind_histogram(events)}


SCENARIOS = _discover_scenarios()
FRAMEWORKS = sorted({framework for framework, _ in SCENARIOS})
_SCENARIO_IDS = [f"{framework}/{path.stem}" for framework, path in SCENARIOS]


def _parse_trace(framework: str) -> list[SessionEvent]:
    """Every event across all scenarios of a framework (fresh adapter per file)."""
    events: list[SessionEvent] = []
    for scenario_framework, jsonl in SCENARIOS:
        if scenario_framework == framework:
            events.extend(_parse_scenario(scenario_framework, jsonl))
    return events


@pytest.mark.skipif(not SCENARIOS, reason="no captured raw traces committed yet")
@pytest.mark.parametrize(("framework", "jsonl_path"), SCENARIOS, ids=_SCENARIO_IDS)
def test_raw_trace_parses_without_raw_fallthrough(framework: str, jsonl_path: Path) -> None:
    """Every captured native line must map to a known canonical kind.

    A real trace dropping to ``raw`` means the mapping/preprocessor no longer
    matches the framework's actual output — i.e. drift.
    """
    events = _parse_scenario(framework, jsonl_path)
    assert events, f"{framework}/{jsonl_path.stem}: captured trace produced no events"
    raw = [e for e in events if e.kind == EventKind.RAW]
    assert not raw, (
        f"{framework}/{jsonl_path.stem}: {len(raw)} captured line(s) fell through to raw — "
        f"mapping no longer matches real upstream output"
    )


@pytest.mark.skipif(not SCENARIOS, reason="no captured raw traces committed yet")
@pytest.mark.parametrize(("framework", "jsonl_path"), SCENARIOS, ids=_SCENARIO_IDS)
def test_raw_trace_matches_golden_snapshot(framework: str, jsonl_path: Path) -> None:
    """Pin exact event counts + kind histogram per scenario (drift snapshot).

    Beyond "no raw", this catches *precise* drift: a mapping change that moves a
    line from one canonical kind to another, drops an event, or emits an extra one
    changes the histogram and fails here. Regenerate intentionally with
    ``scripts/capture_traces/regen_golden_snapshots.py`` after a reviewed change.
    """
    snapshot_path = jsonl_path.with_suffix(SNAPSHOT_SUFFIX)
    assert snapshot_path.exists(), (
        f"{framework}/{jsonl_path.stem}: missing golden snapshot {snapshot_path.name}. "
        f"Generate it with scripts/capture_traces/regen_golden_snapshots.py"
    )
    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))
    actual = _snapshot(_parse_scenario(framework, jsonl_path))
    assert actual == expected, (
        f"{framework}/{jsonl_path.stem}: event snapshot drifted.\n"
        f"  expected: {expected}\n  actual:   {actual}\n"
        f"If this change is intentional, regenerate with "
        f"scripts/capture_traces/regen_golden_snapshots.py"
    )


def test_pydantic_ai_part_end_carries_real_content() -> None:
    """Regression guard for issue #40 on REAL captured stream events.

    Native PartEndEvent has no top-level ``content`` — text lives at
    ``part.content``. With the buggy ``content: content`` mapping the assistant
    text would be empty here; this asserts it is populated from real bytes.
    """
    if "pydantic_ai" not in FRAMEWORKS:
        pytest.skip("pydantic_ai trace not captured")
    events = _parse_trace("pydantic_ai")
    assistant_texts = [
        e.payload.get("content") for e in events if e.kind == EventKind.MESSAGE_ASSISTANT
    ]
    non_empty = [t for t in assistant_texts if t]
    assert non_empty, "part_end mapped to empty content — #40 regression"
    assert any("ticket" in t.lower() or "endpoint" in t.lower() for t in non_empty), (
        f"expected real captured task text, got {assistant_texts!r}"
    )
