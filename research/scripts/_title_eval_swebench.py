"""Leak-free honest eval of the SHIPPED request head on SWE-bench_Verified.

Every historical request-head coherence number (0.35 / 0.38 / 0.48 / 0.512) was
judged on the 260 CodePlane reals, ~81% of which sit in the shipped head's
training corpus (t5-title-rationale.parquet) as direct title targets. So none of
those numbers rank the head honestly -- they are leak-inflated.

SWE-bench_Verified (500 human-validated GitHub issues, MIT) is a real dev-text
corpus the shipped head has PROVABLY never trained on (its training inputs are
CodePlane prompts + synthetic request pairs; the SWE-bench inputs are
astropy/django/sympy/... issue bodies -- disjoint domains). Each issue's
problem_statement is `title\nbody`; we feed the BODY as the request and hold the
first-line TITLE as the human reference. This is the first leak-free measurement
of the head we actually ship.

Register/domain caveat (stated, not hidden): the head titles agent REQUESTS
(imperative "add X"), while an issue body is a bug report. The judge scores
heading coherence/faithfulness against the supplied context, which is
register-agnostic, and the gold issue title is the reference ceiling under the
same rubric -- so the model-vs-gold gap is the honest signal even across the
mild domain shift. Generation is the production ONNX request head (CPU, threads=1,
near-zero footprint); judging is SDK network I/O.

Run:
  cd research
  $env:PYTHONIOENCODING="utf-8"
  .venv\\Scripts\\python.exe -u -m scripts._title_eval_swebench --cap 100 --concurrency 6
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
INTERIM = ROOT / "data" / "interim"
SCORES = INTERIM / "title-judge-scores-swebench.jsonl"
TRAIN_CORPUS = INTERIM / "t5-title-rationale.parquet"  # shipped head training data (leak check)

DATASET_ID = "SWE-bench/SWE-bench_Verified"
# Real dev titles sit in a sane length band; drop degenerate first lines (empty,
# one-word, or a wrapped paragraph) so the reference ceiling is a real title.
MIN_TITLE, MAX_TITLE = 15, 140
MIN_BODY = 80
CTX_CAP = 1600  # match the judge's context window


def _utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _split_problem(ps: str) -> tuple[str, str]:
    """(title, body) from a `title\\nbody` problem_statement."""
    ps = (ps or "").replace("\r\n", "\n").strip()
    if "\n" not in ps:
        return ps.strip(), ""
    head, rest = ps.split("\n", 1)
    return head.strip(), rest.strip()


def build_eval(cap: int) -> pd.DataFrame:
    from datasets import load_dataset

    os.environ["HF_HUB_OFFLINE"] = "0"
    os.environ["HF_DATASETS_OFFLINE"] = "0"
    ds = load_dataset(DATASET_ID, split="test")
    rows = []
    for r in ds:
        title, body = _split_problem(r["problem_statement"])
        if not (MIN_TITLE <= len(title) <= MAX_TITLE):
            continue
        if len(body) < MIN_BODY:
            continue
        rows.append(
            {
                "sid": r["instance_id"],
                "repo": r["repo"],
                "gold": title,
                "ctx": body[:CTX_CAP],
                "src": "swebench-verified",
                "tier": "real",
            }
        )
    df = pd.DataFrame(rows)
    # Deterministic sample so reruns are stable; random_state matches the judge.
    if len(df) > cap:
        df = df.sample(cap, random_state=17).reset_index(drop=True)
    _assert_leak_free(df)
    return df


def _assert_leak_free(df: pd.DataFrame) -> None:
    """Fail loudly if any eval gold title is a training target of the shipped head.

    Domains are disjoint by construction, but assert it rather than trust it: the
    whole point of this run is that no historical baseline could make this claim.
    """
    if not TRAIN_CORPUS.exists():
        print(f"WARN: {TRAIN_CORPUS.name} absent -- cannot verify leak-free", file=sys.stderr)
        return
    tc = pd.read_parquet(TRAIN_CORPUS)
    train_targets = set()
    for col in ("gold", "title", "target", "text"):
        if col in tc.columns:
            train_targets |= {str(x).strip() for x in tc[col].dropna()}
    overlap = sum(1 for g in df.gold if g.strip() in train_targets)
    print(f"leak check: {overlap}/{len(df)} eval golds appear in shipped training targets")
    assert overlap == 0, f"LEAK: {overlap} eval golds are training targets -- eval invalid"


def _generate_shipped(df: pd.DataFrame) -> list[str]:
    """Production request head: TitleInferencer().request_title (ONNX, CPU, t=1)."""
    from tracemill.title.inferencer import TitleInferencer

    inf = TitleInferencer()  # default packaged path -> data-request/ head
    return [inf.request_title(c) for c in df.ctx]


async def _run(args: argparse.Namespace) -> int:
    _utf8()
    from scripts._title_judge import _JUDGE_SYSTEM, _RUBRIC, _coin, _extract_json  # noqa: F401
    from scripts._title_judge import _judge_one, _agg  # reuse the exact protocol
    from tracemill_research.config import load_labeling_runtime_config
    from tracemill_research.labeling.backends.copilot_sdk import CopilotSdkBackend

    df = build_eval(args.cap)
    print(f"eval set: {len(df)} SWE-bench_Verified issues ({df.repo.nunique()} repos)")
    print("generating with SHIPPED request head (production ONNX path)...", flush=True)
    df = df.assign(pred=_generate_shipped(df))

    # Sample the head's raw output so the number is legible, not just a mean.
    print("\n  sample (gold  ||  shipped-head pred):")
    for _, r in df.head(8).iterrows():
        print(f"    {r.gold[:70]!r}\n      -> {r.pred!r}")

    cfg = load_labeling_runtime_config()
    backend = CopilotSdkBackend(cfg.backend)
    sem = asyncio.Semaphore(args.concurrency)
    tasks = [_judge_one(backend, sem, r) for r in df.to_dict("records")]
    scored: list[dict] = []
    done = 0
    for coro in asyncio.as_completed(tasks):
        r = await coro
        done += 1
        if r:
            scored.append(r)
        if done % 20 == 0:
            print(f"  judged {done}/{len(tasks)} ({len(scored)} parsed)", file=sys.stderr)

    if not scored:
        print("no items scored (judge returned no parseable JSON)", file=sys.stderr)
        return 1

    with open(SCORES, "w", encoding="utf-8") as fh:
        for r in scored:
            fh.write(json.dumps(r) + "\n")

    n = len(scored)
    print("\n======= SHIPPED REQUEST HEAD on SWE-bench_Verified (LEAK-FREE) =======")
    print(f"  scored {n}/{len(df)}  ->  {SCORES}\n")
    print("  SHIPPED head titles:")
    _agg(scored, "model")
    print("  GOLD issue titles (same rubric, reference ceiling):")
    _agg(scored, "gold_scores")

    wins = {"model": 0, "gold": 0, "tie": 0}
    for r in scored:
        wins[r["winner"]] += 1
    print("\n  -- head-to-head (blinded A/B, shipped head vs gold issue title) --")
    print(
        f"  head wins {wins['model'] / n:.0%}  | tie {wins['tie'] / n:.0%}  | "
        f"gold wins {wins['gold'] / n:.0%}  (n={n})"
    )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cap", type=int, default=100, help="max issues judged (deterministic sample).")
    p.add_argument("--concurrency", type=int, default=6, help="max concurrent SDK judge calls.")
    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
