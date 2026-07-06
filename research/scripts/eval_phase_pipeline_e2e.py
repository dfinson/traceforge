"""End-to-end phase inference over unseen, real-world Copilot CLI sessions.

Runs the **production** pipeline — the exact wire-up the corpus was built from —
with the trained phase classifier installed as the session-level phase producer:

    events.jsonl
        ↓ MappedJsonAdapter (copilot.yaml)
        ↓ SessionEvent
    EventPipeline(enricher=Enricher(), sinks=[_Collect()],
                  phase_inferencer=PhaseInferencer())   ← stamps metadata.phase
        ↓ phase-stamped events

These local sessions are unlabelled, so this is a **qualitative** e2e: it proves
the full production path runs on real data and produces sensible, well-formed
phase stamps (every non-session-control event gets a valid phase; distribution
and transitions are coherent). Quantitative scoring lives in
``eval_phase_inference.py`` (held-out labelled sessions, F1_macro 0.931).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
from collections import Counter
from pathlib import Path

from traceforge.adapters.mapped_json import MappedJsonAdapter
from traceforge.cli.runner import load_mapping_path
from traceforge.enricher import Enricher
from traceforge.phase import PhaseInferencer
from traceforge.pipeline import EventPipeline
from traceforge.sinks.base import StorageSink
from traceforge.types import EventKind, SessionEvent

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("eval-phase-pipeline-e2e")

DEFAULT_ROOTS = (
    Path.home() / ".copilot" / "session-state-snapshots",
    Path.home() / ".copilot" / "session-state",
)
# Phases produced by the model; SESSION_* control events carry no phase.
_CONTROL_KINDS = frozenset(
    {EventKind.SESSION_STARTED, EventKind.SESSION_ENDED, EventKind.SESSION_PAUSED}
)


class _Collect(StorageSink):
    """In-memory sink that keeps the stamped events for inspection."""

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


async def _run_session(
    mapping_path: Path, sid: str, jsonl: Path, inferencer: PhaseInferencer
) -> list[SessionEvent]:
    adapter = MappedJsonAdapter.from_yaml(str(mapping_path), session_id=sid)
    collect = _Collect()
    pipeline = EventPipeline(sinks=[collect], enricher=Enricher(), phase_inferencer=inferencer)
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


async def main_async(args: argparse.Namespace) -> int:
    mapping_path = load_mapping_path("copilot")
    inferencer = PhaseInferencer()  # loads the packaged bundle once, reused across sessions
    _ = inferencer.model  # surface load errors up front

    sessions = _discover(tuple(args.root), args.idle_minutes)
    sessions.sort(key=lambda r: r[2], reverse=args.largest)
    if args.max_kb:
        sessions = [s for s in sessions if s[2] <= args.max_kb * 1024]
    if args.min_kb:
        sessions = [s for s in sessions if s[2] >= args.min_kb * 1024]
    sessions = sessions[: args.limit]
    log.info("e2e over %d unseen sessions", len(sessions))

    phase_counts: Counter[str] = Counter()
    transitions: Counter[tuple[str, str]] = Counter()
    n_sessions = 0
    n_events = 0
    n_phased = 0
    n_unphased_noncontrol = 0

    for sid, jsonl, _size in sessions:
        try:
            events = await _run_session(mapping_path, sid, jsonl, inferencer)
        except Exception as exc:  # noqa: BLE001
            log.warning("session %s failed: %s", sid, exc)
            continue
        if not events:
            continue
        n_sessions += 1
        prev: str | None = None
        sess_phases: list[str] = []
        for ev in events:
            n_events += 1
            ph = ev.metadata.phase if ev.metadata else None
            if ph is not None:
                ph = str(ph)
                phase_counts[ph] += 1
                n_phased += 1
                sess_phases.append(ph)
                if prev is not None and ph != prev:
                    transitions[(prev, ph)] += 1
                prev = ph
            elif ev.kind not in _CONTROL_KINDS:
                n_unphased_noncontrol += 1
        log.info(
            "  %s: %d events, phases=%s",
            sid[:18],
            len(events),
            "/".join(f"{p}:{sess_phases.count(p)}" for p in dict.fromkeys(sess_phases)),
        )

    print("\n=== Production-pipeline phase e2e (unseen real-world sessions) ===")
    print(f"  sessions scored        : {n_sessions}")
    print(f"  total events           : {n_events}")
    print(f"  events stamped w/ phase : {n_phased}")
    print(f"  non-control unphased    : {n_unphased_noncontrol}  (expect 0)")
    total = sum(phase_counts.values()) or 1
    print("  phase distribution:")
    for ph, c in phase_counts.most_common():
        print(f"    {ph:16s} {c:7d}  {100 * c / total:5.1f}%")
    print("  top transitions:")
    for (a, b), c in transitions.most_common(8):
        print(f"    {a:14s} -> {b:14s} {c}")
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
