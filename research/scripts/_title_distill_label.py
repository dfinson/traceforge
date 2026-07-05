"""Sequence-level knowledge-distillation labeling for the span titler.

Kim & Rush (2016) seq-KD: a strong TEACHER (the gold-parity distilbart-xsum
summarization backbone, 306M -- too large to ship at ~393MB int8) decodes a
target title for every TRAIN row's distilled context; the shippable T5 STUDENT
(tiny 16M / small 80M, 31-90MB int8) is then trained on those teacher targets
instead of the raw gold. This transfers the teacher's summarization-objective
quality into a near-zero-footprint artifact WITHOUT any heuristic, threshold, or
architecture change -- the only variable is "target = teacher decode".

HELD-OUT ROWS KEEP THEIR REAL GOLD so the downstream judge stays an honest,
teacher-independent measurement of the student.

Contract (env):
    TITLE_DATASET     source parquet (must have columns: split, ctx, gold, ...)
    TITLE_MODEL_DIR   the TEACHER checkpoint dir (decoded to produce targets)
    TITLE_MAX_SRC     encoder truncation (match the teacher's training)
    TITLE_DISTILL_OUT output parquet path (train gold := teacher decode)

Usage:
    TITLE_DATASET=... TITLE_MODEL_DIR=<teacher> TITLE_MAX_SRC=512 \
    TITLE_DISTILL_OUT=... python -m scripts._title_distill_label
"""

from __future__ import annotations

import os

import pandas as pd

from scripts._title_judge import _generate
from scripts._title_t5_train import DATASET, MODEL_DIR


def main() -> None:
    out = os.environ.get("TITLE_DISTILL_OUT")
    if not out:
        raise SystemExit("set TITLE_DISTILL_OUT to the distilled-dataset output path")

    df = pd.read_parquet(DATASET)
    train = df[df["split"] == "train"].copy()
    held = df[df["split"] != "train"].copy()  # keep REAL gold for honest eval

    teacher_titles = _generate(train)  # decodes MODEL_DIR (the teacher) over ctx
    train["gold"] = teacher_titles

    result = pd.concat([train, held], ignore_index=True)
    result.to_parquet(out, index=False)
    print(
        f"teacher={MODEL_DIR}\n"
        f"wrote {out}: {len(train)} distilled-train (gold:=teacher) "
        f"+ {len(held)} heldout (real gold)",
    )


if __name__ == "__main__":
    main()
