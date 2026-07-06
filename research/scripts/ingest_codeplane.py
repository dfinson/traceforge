"""Harvest CodePlane trail-node gold via the production ingest pipeline.

Reads the slim CodePlane extract at ``data/raw/codeplane/data.db`` (trail_nodes
+ jobs), writes one enriched-corpus parquet per job to
``data/interim/labeling-corpus/codeplane/`` and the assembled per-node
distillation table to ``data/processed/codeplane-distill.parquet``, then prints
a gold field-fill + repo-diversity report so the harvested volume is visible
before any model change.

Registered in ``research/experiments/titler-codeplane-distill.yaml``.
"""

from __future__ import annotations

import logging
import sys

from traceforge_research.ingest.codeplane import (
    CodeplaneIngestConfig,
    default_db_path,
    default_distill_out,
    default_output_dir,
    run_sync,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ingest-codeplane")


def main() -> int:
    db_path = default_db_path()
    if not db_path.is_file():
        log.error("no CodePlane DB at %s; copy the slim extract there first", db_path)
        return 2

    cfg = CodeplaneIngestConfig(
        db_path=db_path,
        output_dir=default_output_dir(),
        distill_out=default_distill_out(),
        max_jobs=None,
    )
    stats = run_sync(cfg)

    log.info(
        "done: jobs seen=%d emitted=%d failed=%d | nodes=%d events=%d",
        stats.jobs_seen,
        stats.jobs_emitted,
        stats.jobs_failed,
        stats.nodes_seen,
        stats.events_emitted,
    )
    total = stats.distill_rows or 1
    print("\n=== CodePlane distillation gold ===")
    print(f"distill rows (nodes)   {stats.distill_rows}")
    print(f"repos                  {len(stats.repos)}: {', '.join(stats.repos)}")
    print("\ngold field fill:")
    for label, n in (
        ("intent (title gold)", stats.gold_intent),
        ("rationale", stats.gold_rationale),
        ("outcome", stats.gold_outcome),
        ("purpose", stats.gold_purpose),
        ("semantic_kind", stats.gold_semantic_kind),
    ):
        print(f"  {label:22s} {n:5d}/{stats.distill_rows}  ({100 * n / total:4.1f}%)")
    if stats.failures:
        print(f"\nfailures ({len(stats.failures)}):")
        for job_id, err in stats.failures[:10]:
            print(f"  {job_id}: {err}")
    print(f"\ndistill table -> {default_distill_out()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
