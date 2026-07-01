"""Request-titler data-VOLUME learning curve (find the synth knee).

Question this answers: does scaling the synthetic (prompt -> task-title) corpus
BEYOND the current mass keep improving generalization to REAL messy user prompts,
or has coherence plateaued? If it has plateaued, more synthesis is wasted GPU and
the lever is elsewhere (bigger model / live LLM for the once-per-session call).

Design (why it is a clean curve, not flat-by-construction):
  * TRAIN SYNTH-ONLY. The eval set is the fixed 260 held-out CodePlane REAL prompts
    (t5-title-codeplane.parquet). Synth and real are disjoint by construction, so
    there is zero train/eval leakage and the only thing that varies across budgets
    is how much synthetic coverage the model saw.
  * NESTED subsets by a stable content hash (2k subset of 4k subset of 8k of 12k),
    so a larger budget strictly ADDS data -- no resampling churn between points.
  * CONSTANT hyperparameters (from-scratch google/t5-efficient-tiny, EPOCHS/BS/LR
    fixed). More data at fixed epochs => more gradient steps, the honest "more data"
    regime. A single synth source makes the trainer's source sampler a no-op (one
    source, weight 1.0), so there is no equal-per-source mass balancing to flatten
    the curve.
  * SAME held-out reals judged at every budget (per-source-cap 80, seed 17, the
    proven CodePlane eval) so coherence is comparable point to point.

Footprint / safety: training uses the GPU (CUDA_VISIBLE_DEVICES=0); everything is
capped to 2 CPU threads. The harness runs the four budgets SEQUENTIALLY (one GPU),
checkpoints each budget to sqlite, and RESUMES (skips budgets already 'done'). It
never kills processes; each train/judge is a plain subprocess it waits on.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # research/
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
INTERIM = ROOT / "data" / "interim"
SWEEP = INTERIM / "sweep"
SWEEP.mkdir(parents=True, exist_ok=True)

PAIRS = INTERIM / "request-title-pairs.12k.json"
EVAL_DS = INTERIM / "t5-title-codeplane.parquet"
YAML = (ROOT / "experiments" / "titler-request-dedicated.yaml").resolve()
DB = SWEEP / "request-sweep.db"

REQUEST_PREFIX = "title task from request: "
BUDGETS = [2000, 4000, 8000, 12000]

BASE_ENV = {
    "CUDA_VISIBLE_DEVICES": "0",
    "OMP_NUM_THREADS": "2",
    "MKL_NUM_THREADS": "2",
    "TOKENIZERS_PARALLELISM": "false",
    "HF_HUB_OFFLINE": "1",
    "PYTHONIOENCODING": "utf-8",
    "TITLE_BASE_MODEL": "google/t5-efficient-tiny",
    "TITLE_EXPERIMENT": "titler-request-volume-sweep",
    "TITLE_EXPERIMENT_YAML": str(YAML),
    "EPOCHS": "12",
    "BS": "32",
    "LR": "3e-4",
}


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.execute(
        "CREATE TABLE IF NOT EXISTS req_sweep (budget INTEGER PRIMARY KEY, status TEXT, "
        "n_train INTEGER, loss REAL, coherent REAL, gold_coherent REAL, n_judged INTEGER, "
        "h2h_model REAL, h2h_gold REAL, updated TEXT)"
    )
    for b in BUDGETS:
        c.execute("INSERT OR IGNORE INTO req_sweep (budget, status) VALUES (?, 'pending')", (b,))
    c.commit()
    return c


def _mark(c: sqlite3.Connection, budget: int, **kw) -> None:
    kw["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    cols = ", ".join(f"{k}=?" for k in kw)
    c.execute(f"UPDATE req_sweep SET {cols} WHERE budget=?", (*kw.values(), budget))
    c.commit()


def _load_synth() -> list[dict]:
    """Synth pairs ONLY, in a stable content-hash order (deterministic, no seed)."""
    pairs = json.loads(PAIRS.read_text(encoding="utf-8"))
    synth = [p for p in pairs if p.get("origin") == "synth-distill"]

    def key(p: dict) -> str:
        return hashlib.md5(f"{p['prompt']}\x00{p['gold']}".encode()).hexdigest()

    synth.sort(key=key)
    return synth


def _build_dataset(synth: list[dict], budget: int) -> Path:
    """First `budget` synth pairs -> request-only train parquet (all split=train).

    Columns mirror the codeplane eval parquet so the trainer/judge read identical
    shapes: src, sid, aid, order, tier, gold, ctx, split, prefix.
    """
    import pandas as pd

    subset = synth[:budget]
    rows = []
    for i, p in enumerate(subset):
        sid = hashlib.md5(f"{p['prompt']}\x00{p['gold']}".encode()).hexdigest()[:16]
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
            }
        )
    df = pd.DataFrame(rows)
    out = SWEEP / f"req-ds-{budget}.parquet"
    df.to_parquet(out, index=False)
    return out


def _run(cmd: list[str], env: dict, log: Path) -> int:
    """Run a child to completion, tee-ing combined output to `log`. utf-8 safe."""
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


def _train(ds: Path, model_dir: Path, budget: int) -> float | None:
    env = {"TITLE_DATASET": str(ds), "TITLE_MODEL_DIR": str(model_dir)}
    log = SWEEP / f"train-{budget}.log"
    rc = _run([PY, "-u", "-m", "scripts._title_t5_train", "train"], env, log)
    if rc != 0:
        print(f"[{budget}] TRAIN FAILED rc={rc} (see {log})", flush=True)
        return None
    m = re.findall(
        r"final[_ ]?train[_ ]?loss[^0-9]*([0-9.]+)", log.read_text(encoding="utf-8"), re.I
    )
    if not m:
        m = re.findall(r"loss ([0-9.]+)", log.read_text(encoding="utf-8"))
    return float(m[-1]) if m else None


def _judge(model_dir: Path, budget: int) -> dict | None:
    """Judge the fixed real held-out set; read coherence straight from the scores."""
    env = {"TITLE_DATASET": str(EVAL_DS), "TITLE_MODEL_DIR": str(model_dir)}
    log = SWEEP / f"judge-{budget}.log"
    rc = _run(
        [PY, "-u", "-m", "scripts._title_judge", "--per-source-cap", "80", "--concurrency", "6"],
        env,
        log,
    )
    # judge writes scores next to TITLE_DATASET; move to a per-budget file.
    written = EVAL_DS.parent / "title-judge-scores.jsonl"
    dest = SWEEP / f"scores-{budget}.jsonl"
    if written.exists():
        written.replace(dest)
    if rc != 0 or not dest.exists():
        print(f"[{budget}] JUDGE FAILED rc={rc} (see {log})", flush=True)
        return None
    rows = [json.loads(x) for x in dest.read_text(encoding="utf-8").splitlines() if x.strip()]
    if not rows:
        return None
    n = len(rows)
    coh = sum(r["model"]["coherent"] for r in rows) / n
    gcoh = sum(r["gold_scores"]["coherent"] for r in rows) / n
    wins = {"model": 0, "gold": 0, "tie": 0}
    for r in rows:
        wins[r["winner"]] = wins.get(r["winner"], 0) + 1
    return {
        "coherent": coh,
        "gold_coherent": gcoh,
        "n_judged": n,
        "h2h_model": wins["model"] / n,
        "h2h_gold": wins["gold"] / n,
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    assert PAIRS.exists(), f"missing {PAIRS}"
    assert EVAL_DS.exists(), f"missing {EVAL_DS}"
    synth = _load_synth()
    print(f"loaded {len(synth)} synth pairs (nested budgets {BUDGETS})", flush=True)
    c = _db()

    for budget in BUDGETS:
        st = c.execute("SELECT status FROM req_sweep WHERE budget=?", (budget,)).fetchone()[0]
        if st == "done":
            print(f"[{budget}] already done -- skip", flush=True)
            continue

        print(f"\n===== BUDGET {budget} =====", flush=True)
        model_dir = SWEEP / f"req-model-{budget}"
        ds = _build_dataset(synth, budget)
        _mark(c, budget, status="training", n_train=budget)

        t0 = time.time()
        loss = _train(ds, model_dir, budget)
        if loss is None and not (model_dir / "config.json").exists():
            _mark(c, budget, status="train_failed")
            print(f"[{budget}] abort budget (train failed)", flush=True)
            continue
        _mark(c, budget, status="judging", loss=loss)
        print(f"[{budget}] trained loss={loss} [{time.time() - t0:.0f}s]", flush=True)

        j = _judge(model_dir, budget)
        if j is None:
            _mark(c, budget, status="judge_failed", loss=loss)
            continue
        _mark(c, budget, status="done", loss=loss, **j)
        print(
            f"[{budget}] DONE coherent={j['coherent']:.0%} gold={j['gold_coherent']:.0%} "
            f"h2h model/gold={j['h2h_model']:.0%}/{j['h2h_gold']:.0%} (n={j['n_judged']})",
            flush=True,
        )

    print("\n================ REQUEST-VOLUME LEARNING CURVE ================", flush=True)
    rows = c.execute(
        "SELECT budget, n_train, loss, coherent, gold_coherent, h2h_model, h2h_gold, n_judged, status "
        "FROM req_sweep ORDER BY budget"
    ).fetchall()
    print(
        f"{'budget':>7} {'loss':>6} {'coherent':>9} {'gold':>6} {'h2h m/g':>10} {'n':>4}  delta",
        flush=True,
    )
    prev = None
    for b, nt, loss, coh, gcoh, hm, hg, nj, status in rows:
        if coh is None:
            print(f"{b:>7} {'-':>6} {status:>9}", flush=True)
            continue
        d = "" if prev is None else f"{(coh - prev) * 100:+.0f}pt"
        print(
            f"{b:>7} {loss or 0:>6.2f} {coh:>8.0%} {gcoh:>5.0%} "
            f"{(hm or 0):>4.0%}/{(hg or 0):<4.0%} {nj:>4}  {d}",
            flush=True,
        )
        prev = coh

    summary = SWEEP / "request-sweep-curve.json"
    summary.write_text(
        json.dumps(
            [
                dict(
                    zip(
                        (
                            "budget",
                            "n_train",
                            "loss",
                            "coherent",
                            "gold_coherent",
                            "h2h_model",
                            "h2h_gold",
                            "n_judged",
                            "status",
                        ),
                        r,
                    )
                )
                for r in rows
            ],
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\ncurve -> {summary}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
