"""Full SWE-agent ingest across downloaded shards.

Reads every parquet under ``data/raw/swe-agent-nebius/data/`` listed in the
download manifest, ingests every resolved trajectory whose enriched event
count is ≤ ``max_events_per_session``, and writes one parquet per session
to ``data/interim/labeling-corpus/swe-agent-nebius/``.

All thresholds come from ``research/experiments/labeling-runtime.yaml``
(specifically ``canonical_view.max_events_per_call``) so we don't hardcode
the cap.
"""

from __future__ import annotations

import json
import logging
import sys

from traceforge_research.config import load_labeling_runtime_config
from traceforge_research.ingest.swe_agent import (
    SweAgentIngestConfig,
    default_output_dir,
    run_sync,
)
from traceforge_research.paths import DATA_RAW, EXPERIMENTS_DIR

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ingest-swe-agent-full")


def main() -> int:
    cfg = load_labeling_runtime_config(EXPERIMENTS_DIR / "labeling-runtime.yaml")
    cap = cfg.canonical_view.max_events_per_call.value
    log.info("max_events_per_session cap from yaml: %d", cap)

    manifest_path = DATA_RAW / "swe-agent-nebius" / "MANIFEST.json"
    if not manifest_path.is_file():
        log.error(
            "no download manifest at %s; run download_swe_agent_shard.py first", manifest_path
        )
        return 2

    records = json.loads(manifest_path.read_text())
    shard_paths = tuple(sorted((DATA_RAW / "swe-agent-nebius" / r["local_path"]) for r in records))
    log.info("ingesting %d shards: %s", len(shard_paths), [p.name for p in shard_paths])

    ingest_cfg = SweAgentIngestConfig(
        shard_paths=shard_paths,
        output_dir=default_output_dir(),
        only_resolved=True,
        max_sessions=None,
        max_events_per_session=cap,
    )
    stats = run_sync(ingest_cfg)
    log.info(
        "done: seen=%d emitted=%d skipped_too_large=%d skipped_unresolved=%d failed=%d events=%d",
        stats.sessions_seen,
        stats.sessions_emitted,
        stats.sessions_skipped_too_large,
        stats.sessions_skipped_unresolved,
        stats.sessions_failed,
        stats.events_emitted,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
