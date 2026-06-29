"""Pilot evaluation harness for labeled training data.

Reads ``data/processed/{phase-labels,boundary-labels,activity-step-toc}.parquet``
and produces a brief report of dataset characteristics versus the targets in
``research/docs/05-data-sizing.md``. This is a *coverage* report; classifier
training proper lives in a separate script (out of scope for tonight).

Outputs are written to ``data/processed/pilot-eval.json``. The script reads
all knobs from ``research/experiments/labeling-runtime.yaml`` so we don't bake
magic numbers in here.
"""

from __future__ import annotations

import json
import logging
import sys
from collections import Counter
from pathlib import Path

import pyarrow.parquet as pq

from tracemill_research.config import load_labeling_runtime_config
from tracemill_research.paths import DATA_PROCESSED, EXPERIMENTS_DIR

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pilot-eval")


def _read(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return pq.read_table(path).to_pylist()


def main() -> int:
    cfg = load_labeling_runtime_config(EXPERIMENTS_DIR / "labeling-runtime.yaml")

    phase_rows = _read(DATA_PROCESSED / "phase-labels.parquet")
    boundary_rows = _read(DATA_PROCESSED / "boundary-labels.parquet")
    toc_rows = _read(DATA_PROCESSED / "activity-step-toc.parquet")

    sessions_phase = {r["session_id"] for r in phase_rows}
    sessions_boundary = {r["session_id"] for r in boundary_rows}
    sessions_toc = {r["session_id"] for r in toc_rows}

    by_session_type = Counter(r["session_type"] for r in phase_rows if r.get("session_type"))
    by_source = Counter(r.get("source", "copilot-cli") for r in phase_rows)
    by_source_session_type = Counter(
        (r.get("source", "copilot-cli"), r.get("session_type", "unknown"))
        for r in phase_rows
    )
    sessions_by_source = Counter()
    seen_sessions: set[tuple[str, str]] = set()
    for r in phase_rows:
        key = (r.get("source", "copilot-cli"), r["session_id"])
        if key in seen_sessions:
            continue
        seen_sessions.add(key)
        sessions_by_source[r.get("source", "copilot-cli")] += 1

    phase_counts = Counter(p for r in phase_rows for p in r["phases"])
    boundary_counts = Counter(r["label"] for r in boundary_rows)

    report = {
        "schema_version": cfg.schema_version,
        "totals": {
            "sessions_with_phase_labels": len(sessions_phase),
            "sessions_with_boundary_labels": len(sessions_boundary),
            "sessions_with_toc": len(sessions_toc),
            "phase_event_rows": len(phase_rows),
            "boundary_gap_rows": len(boundary_rows),
            "toc_activity_rows": len(toc_rows),
        },
        "sessions_by_source": dict(sessions_by_source),
        "phase_rows_by_source": dict(by_source),
        "phase_rows_by_source_session_type": {
            f"{src}:{st}": n for (src, st), n in by_source_session_type.items()
        },
        "session_types": dict(by_session_type),
        "phase_distribution": dict(phase_counts),
        "boundary_distribution": dict(boundary_counts),
        "rare_class_rows": {
            "review_phase": phase_counts.get("review", 0),
            "implementation_phase": phase_counts.get("implementation", 0),
            "exploration_phase": phase_counts.get("exploration", 0),
            "step_boundary": boundary_counts.get("step-boundary", 0),
            "activity_boundary": boundary_counts.get("activity-boundary", 0),
        },
        "sizing_targets": {
            "phase_n_min_sessions": 100,
            "phase_n_sweet_sessions": 400,
            "boundary_n_min_sessions": 350,
            "boundary_n_sweet_sessions": 800,
        },
    }

    out_path = DATA_PROCESSED / "pilot-eval.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    log.info("wrote %s", out_path)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
