"""Build the CodePlane-rescue titler training set.

Folds the 365 never-trained CodePlane span (context -> intent) pairs from
``data/processed/codeplane-distill.parquet`` into the shipped rationale
multitask mix (``t5-title-rationale.parquet``), matching its schema exactly and
using the trainer's own deterministic session-grouped split (md5 of the session
id, 85/15) so no CodePlane job straddles train/heldout.

The new rows carry the source-agnostic span prefix ("summarize agent step: ")
-- the model sees no source token; ``src`` is provenance only, used by the
source-balanced sampler and for held-out slicing.

Output: ``data/interim/t5-title-codeplane-rescue.parquet``.
"""

from __future__ import annotations

import hashlib

import pandas as pd

from traceforge_research.paths import DATA_INTERIM, DATA_PROCESSED

HELDOUT_FRAC = 0.15  # matches _title_t5_train.HELDOUT_FRAC
SPAN_PREFIX = "summarize agent step: "

BASE = DATA_INTERIM / "t5-title-rationale.parquet"
DISTILL = DATA_PROCESSED / "codeplane-distill.parquet"
OUT = DATA_INTERIM / "t5-title-codeplane-rescue.parquet"


def _split_of(sid: str) -> str:
    h = int(hashlib.md5(sid.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return "heldout" if h < HELDOUT_FRAC else "train"


def main() -> int:
    base = pd.read_parquet(BASE)
    distill = pd.read_parquet(DISTILL)

    pairs = distill[(distill.context != "(no signal)") & (distill.intent.notna())].copy()
    rows = pd.DataFrame(
        {
            "src": "codeplane-node",
            "sid": pairs.session_id.astype(str),
            "aid": pairs.node_id.astype(str),
            "order": pairs.seq.astype(str),
            "tier": "step",
            "gold": pairs.intent.astype(str),
            "ctx": pairs.context.astype(str),
            "split": pairs.session_id.astype(str).map(_split_of),
            "prefix": SPAN_PREFIX,
            "task": "title",
        }
    )[list(base.columns)]

    merged = pd.concat([base, rows], ignore_index=True)
    # Unify column dtypes (base `order` is int64; new rows are str) so pyarrow
    # gets a single type per column.
    for col in merged.columns:
        merged[col] = merged[col].astype(str)
    merged.to_parquet(OUT, index=False)

    # Leak guard: no held-out gold may appear as a train target.
    train_gold = set(merged[merged.split == "train"].gold.str.lower())
    ho_gold = merged[merged.split == "heldout"].gold.str.lower()
    leaks = sum(1 for g in ho_gold if g in train_gold)

    print(f"base rows          {len(base)}")
    print(
        f"codeplane-node add {len(rows)}  (train {sum(rows.split == 'train')} / "
        f"heldout {sum(rows.split == 'heldout')})"
    )
    print(f"merged total       {len(merged)} -> {OUT.name}")
    print(f"heldout gold-in-train leaks: {leaks}/{len(ho_gold)}")
    print("split x task:")
    print(merged.groupby(["split", "task"]).size())
    print("src (title rows only):")
    print(merged[merged.task == "title"].src.value_counts())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
