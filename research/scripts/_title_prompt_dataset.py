"""Fold the prompt->task-title head into the titler's MULTITASK training set.

CodePlane stores, per autonomous job, the initial user PROMPT and a short human
``title`` (prompt "...Save reverts to default, what's going on?" -> title
"Settings save reverts to defaults"). That is the SAME titling task the traceforge
titler already does, on a different INPUT shape: a raw natural-language request
instead of a distilled agent-trace span. The shipped titler is a T5 with a learned
task PREFIX, so this is a SECOND task on the SAME model, not a second model. Span
rows carry the span prefix; request rows carry the request prefix; the multitask
trainer (``_title_t5_train.py``) routes per row off the ``prefix`` column.

Parameter-free, source-agnostic, no tuned knobs:
  * Held-out split is session-grouped by a content hash of the row's id (no seed,
    same HELDOUT_FRAC the span build uses), so a job is wholly train or wholly
    held-out.
  * The RAW prompt is the context. No hand-tuned filler stripping: the model must
    learn to ignore conversational noise the same way it serves it in production.

Run (research root, torch not required -- pandas only):
  research\\.venv\\Scripts\\python.exe -u -m scripts._title_prompt_dataset

Inputs:
  data/interim/request-title-pairs.json     merged synth+real (prompt, gold, origin)
    -- falls back to codeplane_title_pairs.json if the merged file is absent
  data/interim/t5-title-dataset.parquet      span rows (built by _title_t5_train build)
Output:
  data/interim/t5-title-multitask.parquet    span + request rows, prefix-tagged
"""

from __future__ import annotations

import hashlib
import json

import pandas as pd

from traceforge_research.paths import DATA_INTERIM

# Task prefixes (T5 convention; learned during fine-tune). The span prefix MUST
# match the served titler's prefix in src/traceforge/title/inference.py.
SPAN_PREFIX = "summarize agent step: "
REQUEST_PREFIX = "title task from request: "
# Shared with the span build (_title_t5_train.HELDOUT_FRAC) -- kept in sync by
# value, not import, so this builder has no torch/transformers dependency.
HELDOUT_FRAC = 0.15

# Pairs source: the merged synthetic+real request file when present (built by
# scripts._title_synth_requests), else the original CodePlane-only seed. Each pair
# may carry an ``origin`` mapped to a per-source tag so the trainer's source
# whitelist and mass policy can see synthetic vs real request rows distinctly.
PAIRS_MERGED = DATA_INTERIM / "request-title-pairs.json"
PAIRS_SEED = DATA_INTERIM / "codeplane_title_pairs.json"
SPAN_DATASET = DATA_INTERIM / "t5-title-dataset.parquet"
OUT = DATA_INTERIM / "t5-title-multitask.parquet"

# origin (in the pairs file) -> source tag (on the training row)
ORIGIN_SRC = {
    "codeplane-real": "codeplane-prompt",
    "synth-distill": "synth-request",
}
DEFAULT_SRC = "codeplane-prompt"

COLS = ["src", "sid", "aid", "order", "tier", "gold", "ctx", "split", "prefix"]


def _slug(text: str, n: int = 48) -> str:
    out = []
    for ch in text.lower():
        out.append(ch if ch.isalnum() else "-")
    s = "".join(out).strip("-")
    while "--" in s:
        s = s.replace("--", "-")
    return s[:n] or "job"


def _split_of(sid: str) -> str:
    """Group-stable held-out assignment from the row id alone (parameter-free)."""
    h = int(hashlib.md5(sid.encode("utf-8")).hexdigest(), 16) % 1000
    return "heldout" if h < int(HELDOUT_FRAC * 1000) else "train"


def build_request_rows() -> pd.DataFrame:
    pairs_path = PAIRS_MERGED if PAIRS_MERGED.exists() else PAIRS_SEED
    pairs = json.loads(pairs_path.read_text(encoding="utf-8"))
    rows = []
    seen: dict[str, int] = {}
    for p in pairs:
        prompt = (p.get("prompt") or "").strip()
        gold = (p.get("gold") or "").strip()
        if not prompt or not gold:
            continue
        src = ORIGIN_SRC.get(p.get("origin", ""), DEFAULT_SRC)
        base = _slug(gold)
        seen[base] = seen.get(base, 0) + 1
        sid = base if seen[base] == 1 else f"{base}-{seen[base]}"
        rows.append(
            {
                "src": src,
                "sid": sid,
                "aid": "0",
                "order": 0,
                "tier": "task",
                "gold": gold,
                "ctx": prompt,
                "split": _split_of(sid),
                "prefix": REQUEST_PREFIX,
            }
        )
    return pd.DataFrame(rows, columns=COLS)


def main() -> None:
    req = build_request_rows()

    span = pd.read_parquet(SPAN_DATASET)
    if "prefix" not in span.columns:
        span = span.assign(prefix=SPAN_PREFIX)
    else:
        span["prefix"] = span["prefix"].fillna(SPAN_PREFIX)

    out = pd.concat([span[COLS], req[COLS]], ignore_index=True)
    out.to_parquet(OUT)

    print(f"wrote {OUT}  ({len(out)} rows)")
    print("\nrows by src x split:")
    print(out.groupby(["src", "split"]).size())
    print(
        f"\nrequest head: {len(req)} rows "
        f"({dict(req.split.value_counts())}), distinct golds {req.gold.nunique()}"
    )


if __name__ == "__main__":
    main()
