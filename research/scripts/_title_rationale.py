"""Teacher rationale generator for rationale distillation (arXiv:2305.02301).

Distilling Step-by-Step trains a small model multitask on (label) AND (rationale),
where the rationale is a teacher LLM's reasoning that connects input -> label. It
injects task knowledge the small model would otherwise need far more data to learn.

Here the "label" is the gold title and the "rationale" is ONE plain, entity-rich
sentence naming the buried intent the terse title omits:
  * request rows  -> what the user actually asks (action + the specific thing it
                     acts on), grounded in the rambling message.
  * span rows     -> what the agent actually did and to what, grounded in the trace.

The rationale is an AUXILIARY task: co-trained under its own T5 prefix, never decoded
at serve, so it adds zero runtime footprint. The blessed Sonnet labeler (same backend
as the LLM judge) writes the rationales.

Subcommands:
  probe  -- generate + print a handful of rationales for human inspection (no write)
  build  -- generate rationales for every TRAIN row and write an augmented parquet
            (original rows get task="title"; rationale rows get task="rationale").
            Held-out rows are NOT augmented (judged on the title only).

Run (from research/, env CUDA_VISIBLE_DEVICES=-1 not needed -- no torch here):
  $env:PYTHONIOENCODING="utf-8"
  .venv\\Scripts\\python.exe -u -m scripts._title_rationale probe --task request --n 5
  .venv\\Scripts\\python.exe -u -m scripts._title_rationale build \
      --dataset data/interim/t5-title-multitask.parquet \
      --out data/interim/t5-title-rationale.parquet --concurrency 4
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import pandas as pd

from tracemill_research.config import load_labeling_runtime_config
from tracemill_research.labeling.backends.copilot_sdk import CopilotSdkBackend

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Rationale task prefixes (distinct from the title prefixes the model already
# routes on). Mapping is by whether the source row is a request or a span row.
_REQUEST_TITLE_PREFIX = "title task from request: "
_RATIONALE_PREFIX = {"request": "explain request: ", "span": "explain step: "}

_SYSTEM = (
    "You expose the hidden intent buried in a noisy message so a tiny model can "
    "learn to find it. You answer with exactly one plain declarative sentence, "
    "grounded entirely in the given text. You never invent specifics and never "
    "add preamble."
)

_PROMPT = {
    "request": """\
A user sent this raw message to a coding agent. A human later titled the session:
"{gold}".

Write ONE plain declarative sentence stating the user's ACTUAL request: the concrete
action they want and the specific thing it acts on, plus the one detail that pins it
down. Ground every word in the message; do not invent specifics. Name the real
entities (features, files, settings, errors) explicitly, including the ones the short
title leaves out. Do not begin with "The user"; just state the request. Max ~30 words.

MESSAGE:
{ctx}

REQUEST (one sentence):""",
    "span": """\
Below is a distilled trace of ONE step an agent took. A human titled it: "{gold}".

Write ONE plain declarative sentence stating what the agent ACTUALLY did in this step
and to what: the concrete files, commands, or targets involved. Ground every word in
the trace; do not invent. Name the real entities explicitly, including the ones the
short title leaves out. Do not begin with "The agent"; just state the action.
Max ~30 words.

TRACE:
{ctx}

ACTION (one sentence):""",
}


def _utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def _kind(row: dict) -> str:
    """request vs span, inferred from the row's title prefix (no source tag)."""
    pref = row.get("prefix")
    if isinstance(pref, str) and pref.startswith("title task from request"):
        return "request"
    return "span"


def _clean_sentence(text: str) -> str:
    """First non-empty line, single sentence, no surrounding quotes/labels."""
    s = (text or "").strip()
    for tag in ("REQUEST (one sentence):", "ACTION (one sentence):"):
        if s.startswith(tag):
            s = s[len(tag) :].strip()
    s = s.splitlines()[0].strip() if s else ""
    if len(s) >= 2 and s[0] in "\"'" and s[-1] == s[0]:
        s = s[1:-1].strip()
    return s


async def _one(
    backend: CopilotSdkBackend, sem: asyncio.Semaphore, idx: int, row: dict
) -> tuple[int, str | None]:
    kind = _kind(row)
    prompt = _PROMPT[kind].format(gold=row["gold"], ctx=str(row["ctx"])[:1600])
    try:
        async with sem:
            res = await backend.complete(prompt, system_message=_SYSTEM)
    except Exception:  # noqa: BLE001 - one bad SDK call must not abort the batch
        return idx, None
    return idx, (_clean_sentence(res.text or "") or None)


def _flush(base: pd.DataFrame, rationale_rows: list[dict], out: str) -> None:
    """Atomically write base + whatever rationales exist so far (crash-safe)."""
    frames = [base] + ([pd.DataFrame(rationale_rows)] if rationale_rows else [])
    combined = pd.concat(frames, ignore_index=True)
    tmp = out + ".tmp"
    combined.to_parquet(tmp, index=False)
    os.replace(tmp, out)


async def _probe(args: argparse.Namespace) -> int:
    _utf8()
    df = pd.read_parquet(args.dataset)
    df = df[df.split == "train"] if "split" in df.columns else df
    if args.task != "both":
        if "prefix" in df.columns:
            is_req = df.prefix.astype(str).str.startswith("title task from request")
            df = df[is_req] if args.task == "request" else df[~is_req]
    sample = df.sample(min(args.n, len(df)), random_state=17).to_dict("records")

    cfg = load_labeling_runtime_config()
    backend = CopilotSdkBackend(cfg.backend)
    sem = asyncio.Semaphore(args.concurrency)
    results = await asyncio.gather(*[_one(backend, sem, i, r) for i, r in enumerate(sample)])
    rats: list[str | None] = [None] * len(sample)
    for i, rat in results:
        rats[i] = rat
    for r, rat in zip(sample, rats):
        print(f"\n[{_kind(r)}]  gold: {r['gold']!r}")
        print(f"  ctx : {str(r['ctx'])[:160]!r}")
        print(f"  RAT : {rat!r}")
    return 0


async def _build(args: argparse.Namespace) -> int:
    _utf8()
    df = pd.read_parquet(args.dataset)
    base = df.copy()
    base["task"] = "title"
    tr = df[df.split == "train"].reset_index(drop=True) if "split" in df.columns else df
    rows = tr.to_dict("records")

    cfg = load_labeling_runtime_config()
    backend = CopilotSdkBackend(cfg.backend)
    sem = asyncio.Semaphore(args.concurrency)

    rationale_rows: list[dict] = []
    done = 0
    flush_every = max(200, len(rows) // 20)  # ~20 checkpoints over the run
    tasks = [asyncio.ensure_future(_one(backend, sem, i, r)) for i, r in enumerate(rows)]
    for fut in asyncio.as_completed(tasks):
        idx, rat = await fut
        done += 1
        if rat:
            nr = dict(rows[idx])
            nr["gold"] = rat
            nr["prefix"] = _RATIONALE_PREFIX[_kind(rows[idx])]
            nr["task"] = "rationale"
            rationale_rows.append(nr)
        if done % 100 == 0:
            print(f"  rationalized {done}/{len(rows)}", file=sys.stderr)
        if done % flush_every == 0:
            _flush(base, rationale_rows, args.out)
            print(f"  checkpoint -> {args.out} ({len(rationale_rows)} rationales)", file=sys.stderr)

    _flush(base, rationale_rows, args.out)
    out = pd.read_parquet(args.out)
    print(
        f"wrote {len(out)} rows -> {args.out}  "
        f"(title={len(base)}, rationale={len(rationale_rows)})"
    )
    print(out.groupby(["task"]).size())
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="teacher rationale generator (distilling step-by-step)")
    sub = p.add_subparsers(dest="cmd", required=True)
    default_ds = os.path.join(ROOT, "data", "interim", "t5-title-multitask.parquet")

    pp = sub.add_parser("probe")
    pp.add_argument("--dataset", default=default_ds)
    pp.add_argument("--task", choices=["request", "span", "both"], default="both")
    pp.add_argument("--n", type=int, default=6)
    pp.add_argument("--concurrency", type=int, default=4)

    pb = sub.add_parser("build")
    pb.add_argument("--dataset", default=default_ds)
    pb.add_argument("--out", default=os.path.join(ROOT, "data", "interim", "t5-title-rationale.parquet"))
    pb.add_argument("--concurrency", type=int, default=4)

    args = p.parse_args()
    if args.cmd == "probe":
        return asyncio.run(_probe(args))
    return asyncio.run(_build(args))


if __name__ == "__main__":
    raise SystemExit(main())
