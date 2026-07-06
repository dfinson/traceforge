"""Ingest native Copilot CLI session events through the canonical pipeline.

Source: ``~/.copilot/session-state/<session_id>/events.jsonl`` — the actual
event log the Copilot CLI writes for every session it runs. Each line is a
``{type, id, timestamp, data, parentId}`` event in the schema that
``src/traceforge/mappings/copilot.yaml`` already covers.

This is the canonical wire-up:

    events.jsonl line
        ↓
    MappedJsonAdapter.from_yaml("copilot.yaml")
        ↓ SessionEvent
    EventPipeline(sinks=[ParquetSink], enricher=Enricher())
        ↓ per-session parquet

No handrolled enrichment. No bespoke schema. Just the production pipeline
fed from the real source.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import os
from pathlib import Path

from traceforge.adapters.mapped_json import MappedJsonAdapter
from traceforge.cli.runner import load_mapping_path
from traceforge.enricher import Enricher
from traceforge.pipeline import EventPipeline
from traceforge.sinks.parquet import ParquetSink
from traceforge_research.paths import DATA_INTERIM

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ingest-copilot-sessions")


OUT_DIR = DATA_INTERIM / "labeling-corpus" / "copilot-cli-native"
DEFAULT_ROOTS = (
    Path.home() / ".copilot" / "session-state",
    Path.home() / ".copilot" / "session-state-snapshots",
)


def _discover_sessions(roots: tuple[Path, ...], idle_minutes: int) -> list[tuple[str, Path, int]]:
    """Return (session_id, events.jsonl path, byte size) for usable sessions.

    Across all roots, dedupes by session_id (snapshots win over live dirs).
    ``idle_minutes``: skip sessions whose events.jsonl was touched within
    this many minutes — they're probably still being written to. The
    snapshot dir is always trusted regardless of mtime, since the snapshot
    is the whole point.
    """
    now = dt.datetime.now().timestamp()
    cutoff = now - idle_minutes * 60
    by_sid: dict[str, tuple[str, Path, int]] = {}
    for root in roots:
        if not root.exists():
            continue
        is_snapshot = root.name.endswith("-snapshots")
        for sid_dir in sorted(root.iterdir()):
            if not sid_dir.is_dir():
                continue
            ev = sid_dir / "events.jsonl"
            if not ev.exists():
                continue
            st = ev.stat()
            if not is_snapshot and st.st_mtime > cutoff:
                log.debug("skip live session %s (mtime within %d min)", sid_dir.name, idle_minutes)
                continue
            # Snapshot wins if both exist
            existing = by_sid.get(sid_dir.name)
            if existing is None or is_snapshot:
                by_sid[sid_dir.name] = (sid_dir.name, ev, st.st_size)
    return list(by_sid.values())


async def _process_session(
    mapping_path: Path,
    session_id: str,
    jsonl_path: Path,
    out_dir: Path,
) -> tuple[int, int]:
    """Run one session's events.jsonl through the canonical pipeline.

    Returns ``(n_lines_read, n_events_emitted)``.
    """
    adapter = MappedJsonAdapter.from_yaml(str(mapping_path), session_id=session_id)
    sink = ParquetSink(path=str(out_dir / "{session_id}.parquet"))
    pipeline = EventPipeline(sinks=[sink], enricher=Enricher())

    n_lines = 0
    n_emit = 0
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                n_lines += 1
                try:
                    for event in adapter.parse(line):
                        await pipeline.push(event)
                        n_emit += 1
                except Exception as exc:  # noqa: BLE001
                    # One bad line shouldn't kill the whole session.
                    log.debug("parse error in %s line %d: %s", session_id, n_lines, exc)
    finally:
        await sink.close()
    return n_lines, n_emit


async def main_async(args: argparse.Namespace) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    mapping_path = load_mapping_path("copilot")

    sessions = _discover_sessions(tuple(args.root), args.idle_minutes)
    sessions.sort(key=lambda r: r[2])  # smallest first for fast feedback

    if args.max_mb:
        before = len(sessions)
        max_bytes = args.max_mb * 1024 * 1024
        sessions = [s for s in sessions if s[2] <= max_bytes]
        log.info("size filter: %d → %d (max %d MB)", before, len(sessions), args.max_mb)

    if args.limit:
        sessions = sessions[: args.limit]

    log.info("ingesting %d sessions from %s", len(sessions), args.root)

    stats = {"seen": 0, "ok": 0, "empty": 0, "failed": 0, "lines": 0, "events": 0}
    for sid, jsonl, size in sessions:
        stats["seen"] += 1
        try:
            n_lines, n_emit = await _process_session(mapping_path, sid, jsonl, OUT_DIR)
        except Exception as exc:  # noqa: BLE001
            log.exception("session %s failed: %s", sid, exc)
            stats["failed"] += 1
            continue
        if n_emit == 0:
            stats["empty"] += 1
            # Empty parquet — clean up so the corpus stays tight.
            parquet = OUT_DIR / f"{sid}.parquet"
            if parquet.exists():
                try:
                    os.remove(parquet)
                except OSError:
                    pass
        else:
            stats["ok"] += 1
        stats["lines"] += n_lines
        stats["events"] += n_emit
        if stats["seen"] % 25 == 0:
            log.info(
                "  progress seen=%d ok=%d empty=%d failed=%d events=%d",
                stats["seen"],
                stats["ok"],
                stats["empty"],
                stats["failed"],
                stats["events"],
            )

    log.info("done: %s", stats)
    log.info("output: %s", OUT_DIR)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        action="append",
        default=None,
        help="Copilot session-state root (repeatable). "
        "Defaults to ~/.copilot/session-state plus "
        "~/.copilot/session-state-snapshots.",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Optional cap on sessions to process."
    )
    parser.add_argument(
        "--max-mb",
        type=int,
        default=None,
        help="Skip sessions whose events.jsonl is larger than this. "
        "Large ones (>>50 MB) are usually long-running infra/agent "
        "sessions whose token cost dwarfs the labeling-quality gain.",
    )
    parser.add_argument(
        "--idle-minutes",
        type=int,
        default=10,
        help="Skip sessions whose events.jsonl was modified within this "
        "many minutes — they're likely still being written.",
    )
    args = parser.parse_args()
    if args.root is None:
        args.root = list(DEFAULT_ROOTS)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
