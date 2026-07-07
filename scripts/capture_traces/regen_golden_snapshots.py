"""Regenerate golden ``<scenario>.expected.json`` snapshots for the raw-trace harness.

For every discovered scenario in ``tests/fixtures/raw_traces/<framework>/*.jsonl``
this replays the fixture through the same adapter selection the harness uses and
writes a sibling ``<scenario>.expected.json`` containing the golden summary::

    {"event_count": <int>, "kinds": {<canonical-kind>: <count>, ...}}

``tests/e2e/test_raw_traces.py::test_raw_trace_matches_golden_snapshot`` asserts
each fixture still matches its snapshot, so run this ONLY after an intentional,
reviewed change to a mapping/fixture — never to paper over unexpected drift.

Run:
    uv run --no-progress python scripts/capture_traces/regen_golden_snapshots.py
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "tests" / "e2e" / "test_raw_traces.py"


def _load_harness():
    """Load the test module by path (robust to tests/ not being a package)."""
    spec = importlib.util.spec_from_file_location("_golden_harness", HARNESS)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load harness module from {HARNESS}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    harness = _load_harness()
    scenarios = harness.SCENARIOS
    if not scenarios:
        print("no scenarios discovered under tests/fixtures/raw_traces/")
        return

    for framework, jsonl_path in scenarios:
        events = harness._parse_scenario(framework, jsonl_path)
        snapshot = harness._snapshot(events)
        out_path = jsonl_path.with_suffix(harness.SNAPSHOT_SUFFIX)
        out_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        rel = out_path.relative_to(REPO_ROOT)
        print(f"{framework}/{jsonl_path.stem}: {snapshot['event_count']} events -> {rel}")

    print(f"\nwrote {len(scenarios)} golden snapshot(s)")


if __name__ == "__main__":
    main()
