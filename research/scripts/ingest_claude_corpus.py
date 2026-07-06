"""Bridge: Claude Code transcripts → canonical labeling-corpus parquet.

The Copilot ingest (:mod:`traceforge_research.ingest.copilot`) reads from a
Copilot SQLite session-store and is therefore Copilot-only. Claude Code writes
plain JSONL transcripts, so this script runs the *same* downstream pipeline the
Copilot ingest uses — ``MappedJsonAdapter`` (the bundled ``claude`` mapping) →
``Enricher`` → ``ParquetSink`` — to emit per-session parquet under
``data/interim/labeling-corpus/<session_id>.parquet`` with a schema identical to
the Copilot corpus, so ``label_corpus.py`` / ``load_session_view`` consume them
unchanged.

It then appends the new sessions to ``data/interim/labeling-manifest.yaml``
(schema v3) tagged ``source: claude-cli`` so they can be labelled with::

    python -m scripts.label_corpus --source claude-cli

The manifest is backed up before being rewritten; existing entries (and the
1000+ already-resolved labels) are preserved — labelling is resumable.

Inputs are read from ``data/interim/claude-gen/*.jsonl`` (the rate-limited
``_ratelimited/`` quarantine subdir is skipped). The per-session ``session_id``
is the transcript stem (``<domain>-<sid8>``), which is unique, human-readable,
and encodes the domain for later stratified hold-out selection (carried in the
manifest entry as a non-schema ``domain`` key).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Stale dead-proxy base URL is inherited from the host process env; clear it so
# nothing in the import graph accidentally points at it.
os.environ.pop("ANTHROPIC_BASE_URL", None)

import pyarrow.parquet as pq
import yaml

from traceforge.adapters.mapped_json import MappedJsonAdapter
from traceforge.cli.runner import load_mapping_path
from traceforge.enricher import Enricher
from traceforge.pipeline import EventPipeline
from traceforge.sinks.parquet import ParquetSink

from traceforge_research.paths import DATA_INTERIM

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ingest-claude-corpus")

GEN_DIR = DATA_INTERIM / "claude-gen"
CORPUS_DIR = DATA_INTERIM / "labeling-corpus"
MANIFEST_PATH = DATA_INTERIM / "labeling-manifest.yaml"
SOURCE = "claude-cli"


def _domain_of(stem: str) -> str:
    """``data-science-62326eb0`` → ``data-science`` (sid8 is the last segment)."""
    parts = stem.split("-")
    return "-".join(parts[:-1]) if len(parts) > 1 else stem


async def _ingest_one(mapping_path: Path, jsonl: Path) -> tuple[str, list[str], int]:
    """Run claude transcript → parquet. Returns (sid, shard_filenames, n_events)."""
    sid = jsonl.stem
    # Clear any pre-existing shards for an idempotent re-run.
    for old in CORPUS_DIR.glob(f"{sid}.parquet"):
        old.unlink()
    for old in CORPUS_DIR.glob(f"{sid}.*.parquet"):
        old.unlink()

    adapter = MappedJsonAdapter.from_yaml(str(mapping_path), session_id=sid)
    sink = ParquetSink(path=str(CORPUS_DIR / "{session_id}.parquet"))
    pipeline = EventPipeline(sinks=[sink], enricher=Enricher())
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

    shards = sorted(
        [p for p in CORPUS_DIR.glob(f"{sid}.parquet")]
        + [p for p in CORPUS_DIR.glob(f"{sid}.*.parquet")],
        key=lambda p: p.name,
    )
    n_events = sum(pq.read_table(p).num_rows for p in shards)
    return sid, [p.name for p in shards], n_events


def _update_manifest(new_entries: list[dict]) -> None:
    """Append claude entries to the manifest, deduped by session_id."""
    with MANIFEST_PATH.open("r", encoding="utf-8") as fh:
        manifest = yaml.safe_load(fh)

    backup = MANIFEST_PATH.with_suffix(".yaml.bak")
    backup.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    log.info("backed up manifest -> %s", backup)

    sessions = manifest.setdefault("sessions", [])
    existing = {row["session_id"] for row in sessions}
    added = 0
    for entry in new_entries:
        if entry["session_id"] in existing:
            # Replace in place so re-runs refresh n_events/parquets.
            for i, row in enumerate(sessions):
                if row["session_id"] == entry["session_id"]:
                    sessions[i] = entry
                    break
        else:
            sessions.append(entry)
            added += 1

    # Record the new source descriptor if absent (metadata only; the loader
    # reads per-session `source`, not this list).
    sources = manifest.setdefault("sources", [])
    if not any(s.get("name") == SOURCE for s in sources):
        sources.append(
            {
                "name": SOURCE,
                "subdir": "claude-gen",
                "selection": "all",
                "selected_size": len(new_entries),
            }
        )

    manifest["selected_size"] = len(sessions)
    with MANIFEST_PATH.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(manifest, fh, sort_keys=False, allow_unicode=True)
    log.info(
        "manifest updated: +%d new (%d refreshed) -> %d total sessions",
        added,
        len(new_entries) - added,
        len(sessions),
    )


async def main_async(args: argparse.Namespace) -> int:
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    mapping_path = load_mapping_path("claude")
    transcripts = sorted(GEN_DIR.glob("*.jsonl"))  # top-level only; skips _ratelimited/
    if args.limit:
        transcripts = transcripts[: args.limit]
    log.info("ingesting %d claude transcripts via mapping %s", len(transcripts), mapping_path.name)

    entries: list[dict] = []
    failures: list[tuple[str, str]] = []
    for jsonl in transcripts:
        try:
            sid, shards, n_events = await _ingest_one(mapping_path, jsonl)
            if not shards or n_events == 0:
                failures.append((jsonl.stem, "no events emitted"))
                log.warning("no events for %s", jsonl.stem)
                continue
            entries.append(
                {
                    "session_id": sid,
                    "source": SOURCE,
                    "parquets": shards,
                    "n_events": n_events,
                    "domain": _domain_of(sid),
                }
            )
            log.info(
                "ok %-32s -> %d events (%d shard%s)",
                sid,
                n_events,
                len(shards),
                "" if len(shards) == 1 else "s",
            )
        except Exception as exc:  # noqa: BLE001
            failures.append((jsonl.stem, repr(exc)))
            log.exception("ingest failed for %s", jsonl.stem)

    log.info(
        "ingested %d/%d transcripts (%d failed)", len(entries), len(transcripts), len(failures)
    )
    if failures:
        for sid, err in failures:
            log.warning("  FAIL %s: %s", sid, err)

    if entries and not args.no_manifest:
        _update_manifest(entries)
    elif args.no_manifest:
        log.info("--no-manifest: parquets written, manifest untouched")
    return 0 if entries else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=None, help="cap transcripts (smoke)")
    p.add_argument(
        "--no-manifest", action="store_true", help="write parquets only; do not touch the manifest"
    )
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
