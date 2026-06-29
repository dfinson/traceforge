"""End-to-end TOC + titles over real Claude-CLI sessions via the NATIVE pipeline.

No bespoke adapter: ingest each Claude transcript through tracemill's packaged
``claude`` mapping (MappedJsonAdapter + Enricher), segment every gap with the
causal boundary classifier (``tracemill.boundary``), then title each detected
activity/step window with the fine-tuned tiny seq2seq titler
(``data/interim/t5-title-model``).

This is a pure cross-source generalisation eyeball test: the titler never saw a
Claude trace and there are no gold titles, so we render the produced activity/
step tree inline for the human read.

Run (research venv; GPU optional):
  cd research
  $env:CUDA_VISIBLE_DEVICES="0"; $env:PYTHONIOENCODING="utf-8"
  .venv\\Scripts\\python.exe -u -m scripts._title_claude_e2e \
      --dir data/interim/claude-sessions
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from tracemill.boundary import load as load_boundary_model
from tracemill.boundary import predict_session
from tracemill.cli.runner import load_mapping_path
from tracemill.phase.event_rows import event_to_feature_row

# reuse the proven native enrich path from the boundary e2e harness
from scripts.eval_boundary_pipeline_e2e import _enrich_session  # noqa: E402
from scripts._title_t5 import distilled_context  # noqa: E402
from scripts._title_t5_train import MAX_SRC, MAX_TGT, MODEL_DIR, PREFIX  # noqa: E402
from scripts._title_hygiene import pick_distinct  # noqa: E402

NB = 5  # beams == returned sequences; alternates feed sibling-dedup at render


def _assemble(events, rows_by_id, preds):
    """Walk per-gap labels into activities -> steps -> event-row windows.

    Each gap dict's ``event_id`` is the *after* event of the gap; a boundary on
    that gap opens a new block at the NEXT event. Activity boundaries open a new
    activity (and its first step); step boundaries open a new step inside the
    current activity; everything else extends the current step.
    """
    label_after = {p["event_id"]: p["label"] for p in preds}
    acts: list[dict] = []

    def new_act() -> None:
        acts.append({"rows": [], "steps": [{"rows": []}]})

    new_act()
    for i, ev in enumerate(events):
        row = rows_by_id[ev.id]
        if i > 0:
            lbl = label_after.get(events[i - 1].id, "noise")
            if lbl == "activity-boundary":
                new_act()
            elif lbl == "step-boundary":
                acts[-1]["steps"].append({"rows": []})
        acts[-1]["rows"].append(row)
        acts[-1]["steps"][-1]["rows"].append(row)
    return acts


def _gen_titles(mdl, tok, dev, contexts):
    """Beam-decode a title (+ alternates) for every context string."""
    import torch

    out_main: list[str] = []
    out_alts: list[list[str]] = []
    with torch.no_grad():
        for i in range(0, len(contexts), 64):
            chunk = contexts[i:i + 64]
            enc = tok([PREFIX + c for c in chunk], padding=True, truncation=True,
                      max_length=MAX_SRC, return_tensors="pt").to(dev)
            gen = mdl.generate(**enc, max_new_tokens=MAX_TGT, num_beams=NB,
                               num_return_sequences=NB, no_repeat_ngram_size=2,
                               repetition_penalty=1.3, length_penalty=0.8,
                               early_stopping=True)
            dec = [s.strip() for s in tok.batch_decode(gen, skip_special_tokens=True)]
            for j in range(len(chunk)):
                cand = dec[j * NB:(j + 1) * NB]
                out_main.append(cand[0])
                out_alts.append(cand)
    return out_main, out_alts





async def _run(args: argparse.Namespace) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    import torch
    from transformers import AutoTokenizer, T5ForConditionalGeneration

    mapping_path = load_mapping_path("claude")
    model = load_boundary_model()
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    tok = AutoTokenizer.from_pretrained(MODEL_DIR)
    mdl = T5ForConditionalGeneration.from_pretrained(MODEL_DIR).to(dev).eval()
    print(f"device={dev}  boundary classes={model.classes}  "
          f"decode={'causal' if model.decode_params else 'argmax'}", file=sys.stderr)
    show_ctx = os.environ.get("SHOW_CTX") == "1"

    files = sorted(Path(args.dir).glob("*.jsonl"))
    if not files:
        print(f"no .jsonl under {args.dir}", file=sys.stderr)
        return 1

    for jsonl in files:
        sid = jsonl.stem
        events = await _enrich_session(mapping_path, sid, jsonl)
        if not events:
            print(f"\n SESSION  [claude]  {sid}: no events", file=sys.stderr)
            continue
        rows_by_id = {ev.id: event_to_feature_row(ev, seq)
                      for seq, ev in enumerate(events)}
        preds = predict_session(model, sid, args.boundary_source, rows_by_id)
        acts = _assemble(events, rows_by_id, preds)

        # flatten every segment that yields signal into one batch for the titler
        jobs: list[tuple[int, int, str]] = []  # (act_idx, step_idx (-1=activity), ctx)
        ctxs: list[str] = []
        jobs_ctx: dict[tuple[int, int], str] = {}
        for ai, act in enumerate(acts):
            actx = distilled_context(act["rows"], src=args.src)
            jobs.append((ai, -1, actx))
            ctxs.append(actx)
            jobs_ctx[(ai, -1)] = actx
            for si, st in enumerate(act["steps"]):
                sctx = distilled_context(st["rows"], src=args.src)
                jobs.append((ai, si, sctx))
                ctxs.append(sctx)
                jobs_ctx[(ai, si)] = sctx
        mains, alts = _gen_titles(mdl, tok, dev, ctxs)
        title = {(j[0], j[1]): (m, a) for j, m, a in zip(jobs, mains, alts)}

        n_steps = sum(len(a["steps"]) for a in acts)
        print(f"\n SESSION  [claude]  {sid}  "
              f"({len(events)} events -> {len(acts)} activities / {n_steps} segments)")
        used: set[str] = set()
        for ai, act in enumerate(acts):
            last_act = ai == len(acts) - 1
            abr = "└─" if last_act else "├─"
            m, a = title[(ai, -1)]
            g = pick_distinct(used, a)
            nev = len(act["rows"])
            print(f" {abr} ACTIVITY  {g!r}   ({nev} ev)")
            if show_ctx:
                print(f" {'   ' if last_act else '│  '}     CTX: {jobs_ctx[(ai, -1)]}")
            pad = "   " if last_act else "│  "
            steps = act["steps"]
            for si, st in enumerate(steps):
                sbr = "└─" if si == len(steps) - 1 else "├─"
                sm, sa = title[(ai, si)]
                sg = pick_distinct(used, sa)
                print(f" {pad}{sbr} step  {sg!r}   ({len(st['rows'])} ev)")
                if show_ctx:
                    print(f" {pad}     CTX: {jobs_ctx[(ai, si)]}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    default_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "interim", "claude-sessions")
    p.add_argument("--dir", default=default_dir,
                   help="directory of Claude *.jsonl transcripts")
    p.add_argument("--src", default="claude",
                   help="source key for distilled_context boilerplate filter "
                        "(claude has no learned boilerplate -> no-op)")
    p.add_argument("--boundary-source", default="copilot",
                   help="source arg passed to predict_session feature rows")
    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
