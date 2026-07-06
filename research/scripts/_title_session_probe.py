"""Zero-shot SESSION-NAMING probe for an arbitrary span titler.

Session naming in traceforge is the REQUEST task: the session label is its opening
intent -- ``TitleInferencer.request_title(first_user_message)`` titles the first
meaningful sentence of the user's prompt (see inferencer.py ``_maybe_session_title``).
The serve single-model fallback reprefixes the span model to the request prefix
(``_REQUEST_PREFIX``) and decodes with grounding OFF (request-task serve parity).

This probe drives THAT exact path on a held-out real request eval set, so we can
measure -- with numbers, not vibes -- whether a span-only-trained model (e.g. the
distilbart-xsum-distilled flan-t5-small) ALSO serves session naming zero-shot,
before spending any GPU on a request-task training arm.

Contract (env):
    TITLE_SERVE_DIR   int8 ONNX dir (encoder/decoder/tokenizer) of the span model
    TITLE_DATASET     request eval parquet (columns: ctx=raw request, gold, ...)
    TITLE_PREDS_OUT   output preds parquet for ``_title_judge --preds``
"""

from __future__ import annotations

import os

import pandas as pd

from traceforge.title.hygiene import best_of
from traceforge.title.inference import TitleModel
from traceforge.title.inferencer import _REQUEST_PREFIX


def main() -> None:
    serve = os.environ["TITLE_SERVE_DIR"]
    dataset = os.environ["TITLE_DATASET"]
    out = os.environ["TITLE_PREDS_OUT"]

    model = TitleModel.load(serve, threads=1).reprefixed(_REQUEST_PREFIX)
    df = pd.read_parquet(dataset)
    preds = [best_of(model.candidates(c, ground=False)) for c in df["ctx"]]
    df = df.assign(pred=preds)

    cols = [c for c in ("sid", "tier", "src", "gold", "ctx", "pred") if c in df.columns]
    df[cols].to_parquet(out, index=False)
    print(f"serve={serve}\nwrote {out}: {len(df)} session-name preds (request head, ground off)")


if __name__ == "__main__":
    main()
