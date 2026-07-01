"""Dedicated request head: DIVERSE 12k synth + REAL CodePlane anchor. No rationale.

This is the run discussed all session: train the request titler on the domain-diverse
12k synthetic prompt->title corpus (request-title-pairs.12k.json) TOGETHER WITH the
477 real CodePlane anchor titles. NO rationale task (dropped by request).

Why this is the untested cell:
  * The synth-VOLUME sweep trained on the diverse 12k but SYNTH-ONLY (no anchor) ->
    30% peak, regressed at 12k. It never had the real anchor to ground it.
  * The prior 0.48 dedicated model had anchor + rationale but used a DIFFERENT
    (older) synth pool -- 0% overlap with the diverse 12k.
  * So "diverse 12k + real anchor" has NEVER been trained. This runs it.

Recipe: from-scratch google/t5-efficient-tiny, two sources (synth-request ~12k +
codeplane-prompt 477) under the validated equal-per-source sampler (single task ->
source-only balancing, each source 50% mass, so the 477 reals are upweighted to
anchor the model to the real distribution). Judged on the fixed 260 held-out
CodePlane reals vs shipped 0.512 and prior dedicated ~0.48. GPU train, threads=2,
resume-safe.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
INTERIM = ROOT / "data" / "interim"

DEDICATED = INTERIM / "t5-title-request-dedicated.parquet"  # source of the 477 real anchor rows
PAIRS = INTERIM / "request-title-pairs.12k.json"
EVAL_DS = INTERIM / "t5-title-codeplane.parquet"
OUT_DS = INTERIM / "t5-title-request-diverse.parquet"
MODEL_DIR = INTERIM / "t5-title-request-diverse-model"
SCORES = INTERIM / "title-judge-scores-diverse.jsonl"
YAML = (ROOT / "experiments" / "titler-request-dedicated.yaml").resolve()

REQUEST_PREFIX = "title task from request: "
# The ~260 CodePlane reals ARE essentially the eval set (233/260 also sit in the old
# dedicated anchor -> training on that anchor leaks the eval). There is no separate
# real anchor pool, so we PARTITION the reals: a portion becomes the train anchor,
# the rest is a clean held-out eval judged in this same run. REAL_HELDOUT sizes the
# eval to ~the judge's n=80 protocol. Synth is all-train fuel (never held out).
HELDOUT_FRAC = 0.15
REAL_HELDOUT = 0.30

BASE_ENV = {
    "CUDA_VISIBLE_DEVICES": "0",
    "OMP_NUM_THREADS": "2",
    "MKL_NUM_THREADS": "2",
    "TOKENIZERS_PARALLELISM": "false",
    "HF_HUB_OFFLINE": "1",
    "PYTHONIOENCODING": "utf-8",
    "TITLE_BASE_MODEL": "google/t5-efficient-tiny",
    "TITLE_EXPERIMENT": "titler-request-dedicated-v1",
    "TITLE_EXPERIMENT_YAML": str(YAML),
    "TITLE_SOURCES": "codeplane-prompt,synth-request",
    "EPOCHS": "12",
    "BS": "32",
    "LR": "3e-4",
}


def _split_of(sid: str, frac: float) -> str:
    h = int(hashlib.md5(sid.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return "heldout" if h < frac else "train"


def build_dataset() -> dict:
    import pandas as pd

    # Reals come from the eval file itself (the canonical 260 CodePlane reals with
    # gold). We RE-SPLIT them so a clean subset is held out for eval and the rest
    # anchors training -- no prompt is ever in both train and eval.
    reals = pd.read_parquet(EVAL_DS).copy()
    reals["split"] = [_split_of(c.strip(), REAL_HELDOUT) for c in reals.ctx]
    if "task" not in reals.columns:
        reals["task"] = "title"

    pairs = json.loads(PAIRS.read_text(encoding="utf-8"))
    synth = [p for p in pairs if p.get("origin") == "synth-distill"]
    synth.sort(key=lambda p: hashlib.md5(f"{p['prompt']}\x00{p['gold']}".encode()).hexdigest())

    rows = []
    for i, p in enumerate(synth):
        sid = "synthdiv-" + hashlib.md5(f"{p['prompt']}\x00{p['gold']}".encode()).hexdigest()[:16]
        rows.append(
            {
                "src": "synth-request",
                "sid": sid,
                "aid": sid,
                "order": i,
                "tier": "task",
                "gold": p["gold"],
                "ctx": p["prompt"],
                "split": "train",
                "prefix": REQUEST_PREFIX,
                "task": "title",
            }
        )
    synth_df = pd.DataFrame(rows)
    cols = [c for c in reals.columns if c in synth_df.columns]
    out = pd.concat([reals[cols], synth_df[cols]], ignore_index=True)
    for c in ("sid", "aid", "gold", "ctx", "prefix", "src", "tier", "task", "split"):
        if c in out.columns:
            out[c] = out[c].astype(str)
    if "order" in out.columns:
        out["order"] = out["order"].astype(int)
    out.to_parquet(OUT_DS, index=False)

    # Guard: assert zero train/eval leakage before we spend GPU.
    tr_ctx = {c.strip() for c in out[out.split == "train"].ctx}
    ev_ctx = {c.strip() for c in out[out.split == "heldout"].ctx}
    leak = tr_ctx & ev_ctx
    assert not leak, f"LEAK: {len(leak)} prompts in both train and eval"

    return {
        "total_rows": len(out),
        "by_src": out.src.value_counts().to_dict(),
        "by_split": out.split.value_counts().to_dict(),
        "eval_reals": int((out.split == "heldout").sum()),
        "anchor_reals": int(((out.split == "train") & (out.src == "codeplane-prompt")).sum()),
        "synth": len(synth_df),
        "leak_ctx": len(leak),
    }


def _run(cmd: list[str], env: dict, log: Path) -> int:
    full = {**os.environ, **BASE_ENV, **env}
    with open(log, "w", encoding="utf-8") as fh:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=full,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        for line in proc.stdout:  # type: ignore[union-attr]
            fh.write(line)
            fh.flush()
        proc.wait()
    return proc.returncode


def train() -> int:
    if (MODEL_DIR / "config.json").exists():
        print(f"model exists ({MODEL_DIR}) -- skip train", flush=True)
        return 0
    log = INTERIM / "diverse-train.log"
    print(f"training -> {MODEL_DIR} (log {log})", flush=True)
    rc = _run(
        [PY, "-u", "-m", "scripts._title_t5_train", "train"],
        {"TITLE_DATASET": str(OUT_DS), "TITLE_MODEL_DIR": str(MODEL_DIR)},
        log,
    )
    if rc != 0:
        print(f"TRAIN FAILED rc={rc} (see {log})", flush=True)
    return rc


def judge() -> dict | None:
    if not SCORES.exists():
        log = INTERIM / "diverse-judge.log"
        print(f"judging on held-out reals in {OUT_DS.name} (log {log})", flush=True)
        rc = _run(
            [
                PY,
                "-u",
                "-m",
                "scripts._title_judge",
                "--per-source-cap",
                "80",
                "--concurrency",
                "6",
            ],
            {"TITLE_DATASET": str(OUT_DS), "TITLE_MODEL_DIR": str(MODEL_DIR)},
            log,
        )
        written = OUT_DS.parent / "title-judge-scores.jsonl"
        if written.exists():
            written.replace(SCORES)
        if rc != 0 or not SCORES.exists():
            print(f"JUDGE FAILED rc={rc} (see {log})", flush=True)
            return None
    rows = [json.loads(x) for x in SCORES.read_text(encoding="utf-8").splitlines() if x.strip()]
    if not rows:
        return None
    n = len(rows)
    wins = {"model": 0, "gold": 0, "tie": 0}
    for r in rows:
        wins[r["winner"]] = wins.get(r["winner"], 0) + 1
    return {
        "coherent": sum(r["model"]["coherent"] for r in rows) / n,
        "faithful": sum(r["model"]["faithful"] for r in rows) / n,
        "gold_coherent": sum(r["gold_scores"]["coherent"] for r in rows) / n,
        "n": n,
        "h2h_model": wins["model"] / n,
        "h2h_gold": wins["gold"] / n,
        "h2h_tie": wins["tie"] / n,
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    assert PAIRS.exists() and EVAL_DS.exists()
    t0 = time.time()
    comp = build_dataset()
    print(f"DIVERSE dataset -> {OUT_DS.name}\n  {json.dumps(comp)}", flush=True)

    if train() != 0 and not (MODEL_DIR / "config.json").exists():
        return 1
    j = judge()
    if j is None:
        return 1

    print(
        "\n============ DIVERSE 12k + REAL ANCHOR (no rationale) vs BASELINES ============",
        flush=True,
    )
    print(
        f"  diverse+anchor: coherent {j['coherent']:.0%}  faithful {j['faithful']:.2f}/2  "
        f"gold {j['gold_coherent']:.0%}  h2h m/g/t {j['h2h_model']:.0%}/{j['h2h_gold']:.0%}/{j['h2h_tie']:.0%}  (n={j['n']})",
        flush=True,
    )
    print(
        "  baselines     : shipped request head ~0.512  |  prior dedicated ~0.48  "
        "(NOTE: baselines judged on the full 260 reals which were partly train-leaked -> APPROXIMATE refs only; "
        "this run's coherent is on a clean never-trained held-out partition)",
        flush=True,
    )
    v = (
        "WINS"
        if j["coherent"] > 0.512
        else ("ties/ambiguous" if j["coherent"] >= 0.48 else "LOSES")
    )
    print(f"  -> {v} (coherent {j['coherent']:.0%} vs 0.512 / 0.48)", flush=True)

    (INTERIM / "diverse-result.json").write_text(
        json.dumps(
            {
                "composition": comp,
                "result": j,
                "baselines": {"shipped": 0.512, "prior_dedicated": 0.48},
                "elapsed_s": round(time.time() - t0),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nresult -> {INTERIM / 'diverse-result.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
