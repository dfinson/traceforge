"""End-to-end boundary (activity/step) segmentation over unseen real sessions.

Runs the production pipeline (adapter → SessionEvent → Enricher) to get enriched
events exactly as the training corpus was built, projects each event onto the
shared feature-row schema (:func:`traceforge.phase.event_rows.event_to_feature_row`),
then labels every gap with the persisted **causal** boundary classifier
(``traceforge.boundary``).

These local sessions are unlabelled, so this is a **qualitative** e2e: it proves
the full path runs on real data and produces well-formed, sensibly-distributed
segmentation (mostly ``noise`` with a minority of activity/step boundaries,
yielding a plausible 3–8 activities / session table of contents). Quantitative
scoring lives in ``train_boundary_baselines.py`` (leave-session-out CV).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
from collections import Counter
from pathlib import Path

from traceforge.adapters.mapped_json import MappedJsonAdapter
from traceforge.boundary import load as load_boundary_model
from traceforge.boundary import predict_session
from traceforge.cli.runner import load_mapping_path
from traceforge.enricher import Enricher
from traceforge.phase.event_rows import event_to_feature_row
from traceforge.pipeline import EventPipeline
from traceforge.sinks.base import StorageSink
from traceforge.types import SessionEvent

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("eval-boundary-pipeline-e2e")

DEFAULT_ROOTS = (
    Path.home() / ".copilot" / "session-state-snapshots",
    Path.home() / ".copilot" / "session-state",
)


class _Collect(StorageSink):
    """In-memory sink that keeps the enriched events for segmentation."""

    def __init__(self) -> None:
        self.events: list[SessionEvent] = []

    async def on_event(self, event: SessionEvent) -> None:
        self.events.append(event)

    async def on_span(self, span) -> None:  # noqa: D102
        pass

    async def on_usage(self, usage) -> None:  # noqa: D102
        pass

    async def flush(self) -> None:  # noqa: D102
        pass

    async def close(self) -> None:  # noqa: D102
        pass


def _discover(roots: tuple[Path, ...], idle_minutes: int) -> list[tuple[str, Path, int]]:
    """Return (session_id, events.jsonl, size) deduped across roots."""
    cutoff = dt.datetime.now().timestamp() - idle_minutes * 60
    by_sid: dict[str, tuple[str, Path, int]] = {}
    for root in roots:
        if not root.exists():
            continue
        is_snapshot = root.name.endswith("-snapshots")
        for sid_dir in sorted(root.iterdir()):
            ev = sid_dir / "events.jsonl"
            if not sid_dir.is_dir() or not ev.exists():
                continue
            st = ev.stat()
            if not is_snapshot and st.st_mtime > cutoff:
                continue
            if sid_dir.name not in by_sid or is_snapshot:
                by_sid[sid_dir.name] = (sid_dir.name, ev, st.st_size)
    return list(by_sid.values())


async def _enrich_session(mapping_path: Path, sid: str, jsonl: Path) -> list[SessionEvent]:
    adapter = MappedJsonAdapter.from_yaml(str(mapping_path), session_id=sid)
    collect = _Collect()
    pipeline = EventPipeline(sinks=[collect], enricher=Enricher())
    with jsonl.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                for event in adapter.parse(line):
                    await pipeline.push(event)
            except Exception as exc:  # noqa: BLE001
                log.debug("parse error %s: %s", sid, exc)
    await pipeline.close()
    return collect.events


def _segment(model, sid: str, events: list[SessionEvent]) -> list[dict]:
    """Project enriched events to feature rows and label every gap."""
    rows = {ev.id: event_to_feature_row(ev, seq) for seq, ev in enumerate(events)}
    return predict_session(model, sid, "copilot", rows)


async def main_async(args: argparse.Namespace) -> int:
    mapping_path = load_mapping_path("copilot")
    model = load_boundary_model()  # packaged bundle, reused across sessions
    log.info("loaded boundary model: %s, classes=%s", model.feature_set, model.classes)
    log.info(
        "decode params: %s",
        "active (causal threshold+refractory)" if model.decode_params else "none (argmax fallback)",
    )

    sessions = _discover(tuple(args.root), args.idle_minutes)
    sessions.sort(key=lambda r: r[2], reverse=args.largest)
    if args.max_kb:
        sessions = [s for s in sessions if s[2] <= args.max_kb * 1024]
    if args.min_kb:
        sessions = [s for s in sessions if s[2] >= args.min_kb * 1024]
    sessions = sessions[: args.limit]
    log.info("e2e over %d unseen sessions", len(sessions))

    label_counts: Counter[str] = Counter()
    n_sessions = 0
    n_gaps = 0
    activities_per_session: list[int] = []
    steps_per_session: list[int] = []

    for sid, jsonl, _size in sessions:
        try:
            events = await _enrich_session(mapping_path, sid, jsonl)
            preds = _segment(model, sid, events)
        except Exception as exc:  # noqa: BLE001
            log.warning("session %s failed: %s", sid, exc)
            continue
        if not preds:
            continue
        n_sessions += 1
        n_gaps += len(preds)
        n_act = 0
        n_step = 0
        for p in preds:
            label_counts[p["label"]] += 1
            if p["label"] == "activity-boundary":
                n_act += 1
            elif p["label"] == "step-boundary":
                n_step += 1
        # A boundary opens a new block; +1 for the implicit opening block.
        activities_per_session.append(n_act + 1)
        steps_per_session.append(n_act + n_step + 1)
        log.info(
            "  %s: %d gaps -> %d activity-boundaries, %d step-boundaries (%d events)",
            sid[:18],
            len(preds),
            n_act,
            n_step,
            len(events),
        )

    def _median(xs: list[int]) -> float:
        if not xs:
            return 0.0
        s = sorted(xs)
        m = len(s) // 2
        return float(s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2)

    print("\n=== Production boundary e2e (unseen real-world sessions) ===")
    print(f"  sessions segmented     : {n_sessions}")
    print(f"  total gaps             : {n_gaps}")
    total = sum(label_counts.values()) or 1
    print("  gap-label distribution:")
    for lbl, c in label_counts.most_common():
        print(f"    {lbl:18s} {c:7d}  {100 * c / total:5.1f}%")
    print("  table-of-contents shape (per session):")
    print(
        f"    activities       median={_median(activities_per_session):.1f}  "
        f"min={min(activities_per_session or [0])}  max={max(activities_per_session or [0])}"
    )
    print(
        f"    total segments   median={_median(steps_per_session):.1f}  "
        f"min={min(steps_per_session or [0])}  max={max(steps_per_session or [0])}"
    )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, action="append", default=None)
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--max-kb", type=int, default=512, help="skip events.jsonl larger than this")
    p.add_argument("--min-kb", type=int, default=0, help="skip events.jsonl smaller than this")
    p.add_argument("--largest", action="store_true", help="sample largest sessions first")
    p.add_argument("--idle-minutes", type=int, default=10)
    args = p.parse_args()
    if args.root is None:
        args.root = list(DEFAULT_ROOTS)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
