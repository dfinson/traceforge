"""Build training tables from labeled sessions.

Reads ``data/processed/labels/<sid>.json`` plus
``data/interim/labeling-corpus/<sid>.parquet`` and emits three parquet files
under ``data/processed/``:

* ``phase-labels.parquet`` — one row per event with the canonical phases.
* ``boundary-labels.parquet`` — one row per gap with the boundary label.
* ``activity-step-toc.parquet`` — one row per activity (with steps inline).

Sessions whose status is not in ``ACCEPTABLE_STATUSES`` are skipped and a
diagnostic count is logged at the end.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from tracemill_research.paths import DATA_INTERIM, DATA_PROCESSED

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("training-tables")


LABELS_DIR = DATA_PROCESSED / "labels"
CORPUS_DIR = DATA_INTERIM / "labeling-corpus"
OUT_DIR = DATA_PROCESSED

ACCEPTABLE_STATUSES = {"labeled", "labeled-flagged"}


def _load_labels() -> list[dict]:
    out = []
    for p in sorted(LABELS_DIR.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            log.warning("skip malformed %s: %s", p.name, exc)
    return out


def _event_seq_map(parquet_path: Path) -> dict[str, int]:
    if not parquet_path.is_file():
        return {}
    t = pq.read_table(parquet_path, columns=["event_id", "seq"])
    return {str(r["event_id"]): int(r["seq"] or 0) for r in t.to_pylist()}


def _emit_phase_rows(record: dict, seq_map: dict[str, int]) -> Iterable[dict]:
    sid = record["session_id"]
    source = record.get("source", "copilot-cli")
    for pl in record["labels"]["phase_labels"]:
        yield {
            "session_id": sid,
            "source": source,
            "session_type": record.get("session_type", "unknown"),
            "event_id": pl["event_id"],
            "seq": seq_map.get(pl["event_id"]),
            "phases": list(pl["phases"]),
            "phase_accept_fraction": record["phase_accept_fraction"],
            "status": record["status"],
        }


def _emit_boundary_rows(record: dict, seq_map: dict[str, int]) -> Iterable[dict]:
    sid = record["session_id"]
    source = record.get("source", "copilot-cli")
    for bl in record["labels"]["boundary_labels"]:
        yield {
            "session_id": sid,
            "source": source,
            "session_type": record.get("session_type", "unknown"),
            "after_event_id": bl["after_event_id"],
            "after_seq": seq_map.get(bl["after_event_id"]),
            "label": bl["label"],
            "boundary_accept_fraction": record["boundary_accept_fraction"],
            "status": record["status"],
        }


def _emit_toc_rows(record: dict, seq_map: dict[str, int]) -> Iterable[dict]:
    sid = record["session_id"]
    source = record.get("source", "copilot-cli")
    for idx, act in enumerate(record["labels"]["toc"]):
        yield {
            "session_id": sid,
            "source": source,
            "session_type": record.get("session_type", "unknown"),
            "activity_index": idx,
            "activity_title": act["activity_title"],
            "summary": act["summary"],
            "start_event_id": act["start_event_id"],
            "end_event_id": act["end_event_id"],
            "start_seq": seq_map.get(act["start_event_id"]),
            "end_seq": seq_map.get(act["end_event_id"]),
            "steps": [
                {
                    "step_title": s["step_title"],
                    "summary": s["summary"],
                    "start_event_id": s["start_event_id"],
                    "end_event_id": s["end_event_id"],
                    "start_seq": seq_map.get(s["start_event_id"]),
                    "end_seq": seq_map.get(s["end_event_id"]),
                }
                for s in act["steps"]
            ],
            "toc_accept": record.get("toc_accept", False),
            "status": record["status"],
        }


def main() -> int:
    records = _load_labels()
    log.info("loaded %d label files", len(records))

    counts = {
        "labeled": 0, "labeled-flagged": 0, "skipped-too-large": 0,
        "labeler-failed": 0, "redteam-failed": 0, "validate-failed": 0,
        "other": 0,
    }
    phase_rows: list[dict] = []
    boundary_rows: list[dict] = []
    toc_rows: list[dict] = []

    for rec in records:
        status = rec.get("status", "other")
        counts[status] = counts.get(status, 0) + 1
        if status not in ACCEPTABLE_STATUSES:
            continue
        sid = rec["session_id"]
        seq_map = _event_seq_map(CORPUS_DIR / f"{sid}.parquet")
        phase_rows.extend(_emit_phase_rows(rec, seq_map))
        boundary_rows.extend(_emit_boundary_rows(rec, seq_map))
        toc_rows.extend(_emit_toc_rows(rec, seq_map))

    log.info("status counts: %s", counts)
    log.info(
        "rows: phase=%d boundary=%d toc=%d",
        len(phase_rows), len(boundary_rows), len(toc_rows),
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(phase_rows), OUT_DIR / "phase-labels.parquet")
    pq.write_table(pa.Table.from_pylist(boundary_rows), OUT_DIR / "boundary-labels.parquet")
    pq.write_table(pa.Table.from_pylist(toc_rows), OUT_DIR / "activity-step-toc.parquet")
    log.info("wrote tables to %s", OUT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
