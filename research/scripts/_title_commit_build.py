"""Fold the CommitPackFT span gold into the titler training mix.

Reads the per-commit distillation shards produced by
``scripts.ingest_commitpack`` (``data/processed/commitpack-distill/*.parquet``,
each row = ``context -> subject`` for one permissive-licensed commit) and appends
them to the current rescue mix (``t5-title-codeplane-rescue.parquet``), matching
its schema exactly.

The new rows carry the **source-agnostic span prefix** ("summarize agent step:
") -- the model sees no source token; ``src`` is provenance only, used by the
source-parity sampler. Every commit row is ``split == "train"``: the held-out
evaluation set is the real agent-step distribution (copilot / claude / codeplane
spans), never commits, so commit data is auxiliary training signal and cannot
leak into eval by construction.

Output: ``data/interim/t5-title-commit.parquet``.
"""

from __future__ import annotations

import pandas as pd
import pyarrow.parquet as pq

from tracemill_research.paths import DATA_INTERIM, DATA_PROCESSED

SPAN_PREFIX = "summarize agent step: "

BASE = DATA_INTERIM / "t5-title-codeplane-rescue.parquet"
SHARD_DIR = DATA_PROCESSED / "commitpack-distill"
OUT = DATA_INTERIM / "t5-title-commit.parquet"


def _load_commit_rows() -> pd.DataFrame:
    shards = sorted(SHARD_DIR.glob("*.parquet"))
    if not shards:
        raise SystemExit(f"no commit shards under {SHARD_DIR} -- run scripts.ingest_commitpack")
    frames = [pq.read_table(p).to_pandas() for p in shards]
    df = pd.concat(frames, ignore_index=True)
    df = df[(df.context != "(no signal)") & df.subject.notna()].copy()
    df["subject"] = df.subject.astype(str).str.strip()
    return df[df.subject.str.len() > 0]


def main() -> int:
    base = pd.read_parquet(BASE)
    commits = _load_commit_rows()

    rows = pd.DataFrame(
        {
            "src": "commitpackft",
            "sid": commits.session_id.astype(str),
            "aid": commits.commit.astype(str),
            "order": "0",
            "tier": "step",
            "gold": commits.subject.astype(str),
            "ctx": commits.context.astype(str),
            "split": "train",
            "prefix": SPAN_PREFIX,
            "task": "title",
        }
    )[list(base.columns)]

    merged = pd.concat([base, rows], ignore_index=True)
    for col in merged.columns:
        merged[col] = merged[col].astype(str)
    merged.to_parquet(OUT, index=False)

    # Informational leak check: held-out (real agent-step) golds that coincide
    # with any train gold. Commit rows are train-only, so a hit here is a
    # coincidental short-title collision across distinct data, not context leak
    # (the ingester guarantees the gold never appears in its own context).
    train_gold = set(merged[merged.split == "train"].gold.str.lower())
    ho_gold = merged[merged.split == "heldout"].gold.str.lower()
    coincident = sum(1 for g in ho_gold if g in train_gold)

    print(f"base rows           {len(base)}")
    print(
        f"commitpackft langs  {commits.lang.nunique()}  licenses {sorted(commits.license.unique())}"
    )
    print(f"commitpackft add    {len(rows)}  (all train)")
    print(f"merged total        {len(merged)} -> {OUT.name}")
    print(f"heldout-gold coincident-with-train: {coincident}/{len(ho_gold)}")
    print("split x task:")
    print(merged.groupby(["split", "task"]).size())
    print("src (title rows only):")
    print(merged[merged.task == "title"].src.value_counts())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
