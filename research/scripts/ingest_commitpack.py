"""Harvest permissive-licensed commit subjects from bigcode/commitpackft.

Runs the production-pipeline ingester
(:mod:`traceforge_research.ingest.commitpack`): every permissive-licensed commit
is projected through the real ``Enricher`` + ``ParquetSink`` as one code-edit
step, and its serve-side ``distilled_context`` is recomputed, so the commit
``subject`` (one-line imperative title) joins the titler corpus with byte-
identical schema and zero train/serve skew.

The only row filter is the per-row SPDX license (a legal constraint); there is
no content/quality filtering. By default every language file in the repo is
ingested (no hand-selection). Per-language distill shards checkpoint the run, so
re-invoking resumes where it stopped.

Usage::

    python -m scripts.ingest_commitpack                 # all languages
    python -m scripts.ingest_commitpack --langs python go rust
    python -m scripts.ingest_commitpack --max-per-lang 500   # smoke
"""

from __future__ import annotations

import argparse
import logging
import sys

from traceforge_research.ingest.commitpack import (
    CommitpackIngestConfig,
    default_distill_out,
    default_distill_shard_dir,
    default_output_dir,
    reattach,
    run_sync,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ingest-commitpack")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--langs",
        nargs="+",
        default=None,
        help="language subdirs to ingest (default: every language in the repo)",
    )
    p.add_argument(
        "--max-per-lang",
        type=int,
        default=None,
        help="cap commits per language (smoke/debug only)",
    )
    p.add_argument(
        "--reattach",
        action="store_true",
        help="recompute distilled_context from the existing corpus + rewrite "
        "shards (no re-enrich); use after a projection fix",
    )
    args = p.parse_args()

    cfg = CommitpackIngestConfig(
        langs=tuple(args.langs) if args.langs else None,
        output_dir=default_output_dir(),
        distill_shard_dir=default_distill_shard_dir(),
        distill_out=default_distill_out(),
        max_commits_per_lang=args.max_per_lang,
    )
    if args.reattach:
        return reattach(cfg)
    stats = run_sync(cfg)
    log.info(
        "done: %d/%d languages, %d commits kept -> %s",
        stats.langs_emitted,
        stats.langs_seen,
        stats.commits_kept,
        cfg.distill_out,
    )
    return 0 if stats.commits_kept else 1


if __name__ == "__main__":
    sys.exit(main())
