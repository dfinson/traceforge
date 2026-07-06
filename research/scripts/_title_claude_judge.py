"""Build an OOD judge parquet from real Claude-CLI sessions.

Claude traces are NEVER in the titler's training data (training = copilot + swe
only), so they are the genuine cross-source / cross-framework generalisation
probe. We run the native traceforge pipeline (claude mapping -> enrich -> causal
boundary segmentation) to produce per-segment distilled_context, then fill the
judge's `gold`/B column with BASELINE-TINY titles. Pointing _title_judge at this
parquet (with the nl6 model as A) yields, in one blinded run:
  * nl6 absolute coherence/faithfulness on unseen Claude (does it generalise?)
  * baseline-tiny absolute on Claude (the gold_scores column)
  * nl6-vs-tiny head-to-head on unseen Claude (does the depth win hold OOD,
    i.e. is nl6 generalising or overfitting copilot/swe?)

No judge changes: the only variable is the dataset path + which checkpoint is A.

Run (research venv):
  $env:CUDA_VISIBLE_DEVICES="-1"; $env:PYTHONIOENCODING="utf-8"
  .venv\\Scripts\\python.exe -u -m scripts._title_claude_judge \
      --tiny-dir data/interim/t5-title-model-tiny-baseline \
      --out data/interim/t5-title-claude-ood.parquet
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import pandas as pd

from traceforge.boundary import load as load_boundary_model
from traceforge.boundary import predict_session
from traceforge.cli.runner import load_mapping_path
from traceforge.phase.event_rows import event_to_feature_row

from scripts.eval_boundary_pipeline_e2e import _enrich_session  # noqa: E402
from scripts._title_t5 import distilled_context  # noqa: E402
from scripts._title_t5_train import MAX_SRC, MAX_TGT, PREFIX  # noqa: E402
from scripts._title_claude_e2e import _assemble  # noqa: E402
from scripts._title_hygiene import best_of  # noqa: E402

NB = 5


def _gen_baseline(tiny_dir: str, contexts: list[str]) -> list[str]:
    """Best-of-beam titles from the BASELINE tiny model (the B/reference column)."""
    import torch
    from transformers import AutoTokenizer, T5ForConditionalGeneration

    torch.set_num_threads(int(os.environ.get("OMP_NUM_THREADS", "2")))
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    tok = AutoTokenizer.from_pretrained(tiny_dir)
    mdl = T5ForConditionalGeneration.from_pretrained(tiny_dir).to(dev).eval()
    out: list[str] = []
    with torch.no_grad():
        for i in range(0, len(contexts), 64):
            chunk = contexts[i : i + 64]
            enc = tok(
                [PREFIX + c for c in chunk],
                padding=True,
                truncation=True,
                max_length=MAX_SRC,
                return_tensors="pt",
            ).to(dev)
            gen = mdl.generate(
                **enc,
                max_new_tokens=MAX_TGT,
                num_beams=NB,
                num_return_sequences=NB,
                no_repeat_ngram_size=2,
                repetition_penalty=1.3,
                length_penalty=0.8,
                early_stopping=True,
            )
            dec = [s.strip() for s in tok.batch_decode(gen, skip_special_tokens=True)]
            for j in range(len(chunk)):
                out.append(best_of(dec[j * NB : (j + 1) * NB]))
    return out


async def _run(args: argparse.Namespace) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    mapping_path = load_mapping_path("claude")
    bmodel = load_boundary_model()

    recs: list[dict] = []
    files = sorted(Path(args.dir).glob("*.jsonl"))
    for jsonl in files:
        sid = jsonl.stem
        events = await _enrich_session(mapping_path, sid, jsonl)
        if not events:
            continue
        rows_by_id = {ev.id: event_to_feature_row(ev, seq) for seq, ev in enumerate(events)}
        preds = predict_session(bmodel, sid, args.boundary_source, rows_by_id)
        acts = _assemble(events, rows_by_id, preds)
        order = 0
        for ai, act in enumerate(acts):
            actx = distilled_context(act["rows"], src="claude")
            if actx and actx != "(no signal)":
                recs.append(
                    dict(
                        src="claude-cli",
                        sid=sid,
                        aid=ai,
                        order=order,
                        tier="activity",
                        ctx=actx,
                        split="heldout",
                    )
                )
                order += 1
            for si, st in enumerate(act["steps"]):
                sctx = distilled_context(st["rows"], src="claude")
                if sctx and sctx != "(no signal)":
                    recs.append(
                        dict(
                            src="claude-cli",
                            sid=sid,
                            aid=ai * 100 + si,
                            order=order,
                            tier="step",
                            ctx=sctx,
                            split="heldout",
                        )
                    )
                    order += 1

    if not recs:
        print("no claude segments produced", file=sys.stderr)
        return 1

    df = pd.DataFrame(recs)
    print(
        f"claude segments: {len(df)} "
        f"({df.tier.value_counts().to_dict()}) from {df.sid.nunique()} sessions",
        file=sys.stderr,
    )
    df["gold"] = _gen_baseline(args.tiny_dir, df.ctx.tolist())
    df.to_parquet(args.out, index=False)
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    base = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "interim"
    )
    p.add_argument("--dir", default=os.path.join(base, "claude-sessions"))
    p.add_argument("--tiny-dir", default=os.path.join(base, "t5-title-model-tiny-baseline"))
    p.add_argument("--out", default=os.path.join(base, "t5-title-claude-ood.parquet"))
    p.add_argument("--boundary-source", default="copilot")
    return asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
