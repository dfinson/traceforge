"""Golden e2e tests over REAL captured framework traces.

Unlike the hand-written fixtures elsewhere in the suite (which encode
traceforge's own post-preprocessor assumptions), these tests feed *verbatim*
native framework output — captured by ``scripts/capture_traces/`` — through the
real MappedJsonAdapter (preprocessor + YAML mapping). This is the layer that
catches upstream drift: if a framework changes its on-disk/native shape, the
captured trace changes and these assertions move with reality.

A fixture directory ``tests/fixtures/raw_traces/<framework>/`` is matched to the
mapping ``src/traceforge/mappings/<framework>.yaml`` by name. Add new frameworks
by committing a captured trace; the discovery test picks it up automatically.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from traceforge.adapters.mapped_json import MappedJsonAdapter
from traceforge.types import EventKind

REPO_ROOT = Path(__file__).resolve().parents[2]
TRACES_ROOT = REPO_ROOT / "tests" / "fixtures" / "raw_traces"
MAPPINGS_DIR = REPO_ROOT / "src" / "traceforge" / "mappings"


def _discover() -> list[str]:
    if not TRACES_ROOT.is_dir():
        return []
    return sorted(d.name for d in TRACES_ROOT.iterdir() if d.is_dir() and any(d.glob("*.jsonl")))


def _parse_trace(framework: str) -> list:
    """Run every line of every scenario for a framework through its mapping."""
    yaml_path = MAPPINGS_DIR / f"{framework}.yaml"
    assert yaml_path.exists(), f"no mapping for captured framework {framework!r}"
    events = []
    for jsonl in sorted((TRACES_ROOT / framework).glob("*.jsonl")):
        adapter = MappedJsonAdapter.from_yaml(str(yaml_path), session_id=f"golden-{framework}")
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.extend(adapter.parse(line))
    return events


FRAMEWORKS = _discover()


@pytest.mark.skipif(not FRAMEWORKS, reason="no captured raw traces committed yet")
@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_raw_trace_parses_without_raw_fallthrough(framework: str) -> None:
    """Every captured native line must map to a known canonical kind.

    A real trace dropping to ``raw`` means the mapping/preprocessor no longer
    matches the framework's actual output — i.e. drift.
    """
    events = _parse_trace(framework)
    assert events, f"{framework}: captured trace produced no events"
    raw = [e for e in events if e.kind == EventKind.RAW]
    assert not raw, (
        f"{framework}: {len(raw)} captured line(s) fell through to raw — "
        f"mapping no longer matches real upstream output"
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
