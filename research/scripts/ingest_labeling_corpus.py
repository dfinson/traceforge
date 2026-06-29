"""Run manifest-driven ingest: produce per-session canonical parquet for the
labeling corpus.

Reads ``data/interim/labeling-manifest.yaml`` (built by
``build_labeling_manifest.py``) and writes one parquet per session under
``data/interim/labeling-corpus/<session_id>.parquet``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import yaml

from tracemill_research.ingest.copilot import (
    CopilotIngestConfig,
    ingest_from_manifest,
)
from tracemill_research.paths import DATA_INTERIM, RESEARCH_ROOT

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ingest-corpus")


MANIFEST_PATH = DATA_INTERIM / "labeling-manifest.yaml"
CORPUS_DIR = DATA_INTERIM / "labeling-corpus"


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else (RESEARCH_ROOT / path)


def main() -> int:
    if not MANIFEST_PATH.is_file():
        log.error("manifest missing: %s — run build_labeling_manifest.py first", MANIFEST_PATH)
        return 1
    with MANIFEST_PATH.open("r", encoding="utf-8") as fh:
        manifest = yaml.safe_load(fh)

    db_path = _resolve(Path(manifest["session_store_path"]))
    session_ids = [row["session_id"] for row in manifest["sessions"]]
    log.info("ingesting %d sessions from %s into %s", len(session_ids), db_path, CORPUS_DIR)

    cfg = CopilotIngestConfig(
        session_store_path=db_path,
        output_dir=CORPUS_DIR,
        max_sessions=None,
    )
    import asyncio

    stats = asyncio.run(ingest_from_manifest(cfg, session_ids))
    log.info(
        "stats: sessions=%d, turns=%d, events=%d, failures=%d",
        stats.sessions_processed, stats.turns_processed, stats.events_emitted,
        len(stats.failures),
    )
    for sid, err in stats.failures:
        log.error("  failure %s: %s", sid, err)
    return 0 if stats.sessions_processed > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
