"""Smoke test for the SWE-agent ingest on shard 0.

Ingests just a handful of resolved trajectories so we can sanity-check the
enriched parquet output before running the full sample.
"""

from __future__ import annotations

import logging
import sys

from traceforge_research.ingest.swe_agent import (
    SweAgentIngestConfig,
    default_output_dir,
    default_shard_dir,
    run_sync,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    shard = default_shard_dir() / "train-00000-of-00012.parquet"
    cfg = SweAgentIngestConfig(
        shard_paths=(shard,),
        output_dir=default_output_dir(),
        only_resolved=True,
        max_sessions=5,
        max_events_per_session=220,
    )
    stats = run_sync(cfg)
    print(stats)
    print("output dir:", cfg.output_dir)
    for p in sorted(cfg.output_dir.glob("*.parquet"))[:5]:
        print(" -", p.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
