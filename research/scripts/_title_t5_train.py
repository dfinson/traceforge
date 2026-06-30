"""Fine-tune a tiny seq2seq titler on (distilled context -> gold title) pairs.

The pivot from extraction: the template+slot extractor caps out at its extractive
oracle (swe-agent ROUGE-1 .211 / copilot .147) because gold titles are ABSTRACTIVE
("has_not_null_column" -> "NOT NULL rule"). A tiny fine-tuned generator can learn
that normalization. We reuse the existing context-narrowing machinery (intent /
tool sequence / files / narration) as the "golden platter" input.

Subcommands:
  build  -- build + cache ALL (context -> gold) pairs, session-grouped 85/15 split
  train  -- plain-torch fine-tune of t5-efficient-tiny on GPU (falls back to CPU)
  eval   -- generate on held-out sessions, ROUGE-1 vs gold + tree render

Run (GPU, CUDA_VISIBLE_DEVICES already 0):
  cd research; $env:PYTHONIOENCODING="utf-8"
  .venv\\Scripts\\python.exe -u -m scripts._title_t5_train build
  .venv\\Scripts\\python.exe -u -m scripts._title_t5_train train
  .venv\\Scripts\\python.exe -u -m scripts._title_t5_train eval
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path

import pandas as pd

import mlflow  # noqa: E402

from scripts._title_compose import CORPUS, SRC_DIR, TOC  # noqa: E402
from scripts._title_object import STOP, toks  # noqa: E402
from scripts._title_t5 import distilled_context  # noqa: E402
from tracemill_research.mlflow_utils import log_yaml_params, start_run  # noqa: E402
from tracemill_research.paths import EXPERIMENTS_DIR  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET = os.environ.get(
    "TITLE_DATASET", os.path.join(ROOT, "data", "interim", "t5-title-dataset.parquet")
)
# MODEL_DIR / BASE_MODEL are env-overridable so capacity probes (e.g. t5-small)
# can train + judge a separate checkpoint without clobbering the shipped tiny one.
MODEL_DIR = os.environ.get(
    "TITLE_MODEL_DIR", os.path.join(ROOT, "data", "interim", "t5-title-model")
)
# Default base = plain 4/4 t5-efficient-tiny. The deeper nl6 (6/6) won the
# in-distribution depth sweep, but once the corpus was rebalanced with diverse
# organic copilot gold it lost its edge: nl6 and tiny tie in-distribution while
# tiny generalizes markedly better out-of-distribution (Claude OOD h2h vs the
# baseline-tiny anchor: tiny net -8 / near-parity vs nl6 net -29) and is lighter
# (16M, ~66ms, +100MB vs 19M, ~89ms, +127MB). Tiny ships under near-zero footprint.
BASE_MODEL = os.environ.get("TITLE_BASE_MODEL", "google/t5-efficient-tiny")

# task prefix for the seq2seq input (t5 convention; learned during fine-tune).
# Kept SOURCE-AGNOSTIC on purpose: the model must generalize to unseen trace
# families (e.g. Claude, zero training data) without a source token to lean on.
PREFIX = "summarize agent step: "
HELDOUT_FRAC = 0.15
MAX_SRC = 192
# Target cap. Titles are short (<=~20 tok); rationale-distillation auxiliary
# targets are ~30-word teacher sentences (~40-50 tok), so the ceiling is raised
# to 48 to avoid truncating them. Harmless to title-only datasets (their golds
# never reach the old ceiling). Env-overridable.
MAX_TGT = int(os.environ.get("TITLE_MAX_TGT", "48"))

# This script trains + evaluates the served titler; its home MLflow experiment is
# the domain-diverse organic retrain. Runs are tagged with base_model so a depth
# probe (e.g. t5-small) stays self-describing under the same experiment.
# TITLE_EXPERIMENT / TITLE_EXPERIMENT_YAML are env-overridable (same pattern as
# TITLE_DATASET / TITLE_MODEL_DIR / TITLE_BASE_MODEL) so a sibling head (e.g. the
# prompt->task-title multitask fold-in) can log to its own experiment + yaml
# without forking this trainer.
EXPERIMENT = os.environ.get("TITLE_EXPERIMENT", "titler-domain-diverse-retrain-v1")
EXPERIMENT_YAML = Path(
    os.environ.get(
        "TITLE_EXPERIMENT_YAML", str(EXPERIMENTS_DIR / "titler-domain-diverse-retrain.yaml")
    )
)


def _utf8():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _split_of(sid: str) -> str:
    h = int(hashlib.md5(sid.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return "heldout" if h < HELDOUT_FRAC else "train"


# ----------------------------------------------------------------------------- build
def build():
    toc = pd.read_parquet(TOC)
    toc = toc[toc.session_type == "agent"]
    rows_out = []
    n_sess = 0
    for (src, sid), srows in toc.groupby(["source", "session_id"]):
        d = SRC_DIR.get(src)
        if d is None:
            continue
        p = os.path.join(CORPUS, d, f"{sid}.parquet")
        if not os.path.exists(p):
            continue
        cdf = pd.read_parquet(p).sort_values("seq")
        seqmap = dict(zip(cdf.event_id, cdf.seq))
        recs = list(cdf.to_dict("records"))
        n_sess += 1
        split = _split_of(sid)

        def window(s_id, e_id):
            s, e = seqmap.get(s_id, 0), seqmap.get(e_id, 0)
            return [r for r in recs if s <= r["seq"] <= e]

        for ai, (_, a) in enumerate(srows.iterrows()):
            aid = f"{sid}#{ai}"
            segs = [(a.start_event_id, a.end_event_id, "activity", a.activity_title, 0)]
            segs += [
                (st["start_event_id"], st["end_event_id"], "step", st["step_title"], si + 1)
                for si, st in enumerate(a.steps)
            ]
            for s_id, e_id, tier, gold, order in segs:
                if not isinstance(gold, str) or not gold.strip():
                    continue
                w = window(s_id, e_id)
                if not w:
                    continue
                ctx = distilled_context(w, src=src)
                if ctx == "(no signal)":
                    continue
                rows_out.append(
                    dict(
                        src=src,
                        sid=sid,
                        aid=aid,
                        order=order,
                        tier=tier,
                        gold=gold.strip(),
                        ctx=ctx,
                        split=split,
                    )
                )
    df = pd.DataFrame(rows_out)
    os.makedirs(os.path.dirname(DATASET), exist_ok=True)
    df.to_parquet(DATASET, index=False)
    print(f"built {len(df)} pairs from {n_sess} sessions -> {DATASET}")
    print(df.groupby(["split", "src"]).size())
    print(
        f"\nheld-out sessions: {df[df.split == 'heldout'].sid.nunique()} | "
        f"train sessions: {df[df.split == 'train'].sid.nunique()}"
    )


# ----------------------------------------------------------------------------- train
def train():
    import torch
    from torch.utils.data import DataLoader, Dataset, WeightedRandomSampler
    from transformers import (
        AutoTokenizer,
        T5ForConditionalGeneration,
        get_linear_schedule_with_warmup,
    )

    epochs = int(os.environ.get("EPOCHS", "12"))
    bs = int(os.environ.get("BS", "32"))
    lr = float(os.environ.get("LR", "3e-4"))

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"device={dev} epochs={epochs} bs={bs} lr={lr}")

    df = pd.read_parquet(DATASET)
    tr = df[df.split == "train"].reset_index(drop=True)
    # Source whitelist. copilot + claude are the real deploy targets; swe-agent is
    # a scaffolded corpus we don't ship against, so it can be excluded here. With
    # the whitelist down to the two priority organic sources, the validated
    # equal-per-source sampler (alpha=0) gives each its canonical 50% mass.
    _srcs = os.environ.get("TITLE_SOURCES", "").strip()
    if _srcs:
        keep = {s.strip() for s in _srcs.split(",") if s.strip()}
        before = len(tr)
        tr = tr[tr.src.isin(keep)].reset_index(drop=True)
        print(f"source whitelist {sorted(keep)}: {before} -> {len(tr)} train rows")
    tok = AutoTokenizer.from_pretrained(BASE_MODEL)

    class DS(Dataset):
        def __init__(self, frame):
            # Per-row task prefix (T5 multitask): rows may carry a `prefix`
            # column (e.g. the task-title-from-request head). Fall back to the
            # default span prefix when absent so single-task datasets are
            # unchanged.
            if "prefix" in frame.columns:
                pref = frame.prefix.fillna(PREFIX)
                self.x = (pref + frame.ctx).tolist()
            else:
                self.x = (PREFIX + frame.ctx).tolist()
            self.y = frame.gold.tolist()

        def __len__(self):
            return len(self.x)

        def __getitem__(self, i):
            return self.x[i], self.y[i]

    def collate(batch):
        xs, ys = zip(*batch)
        enc = tok(list(xs), padding=True, truncation=True, max_length=MAX_SRC, return_tensors="pt")
        lab = tok(
            list(ys), padding=True, truncation=True, max_length=MAX_TGT, return_tensors="pt"
        ).input_ids
        lab[lab == tok.pad_token_id] = -100
        enc["labels"] = lab
        return enc

    # Balance the gradient across SOURCES so the 5.9x-dominant swe corpus does
    # not drown the organic copilot signal. Base case (alpha=0): each source
    # contributes equal total sampling mass (weight = 1 / source_frequency).
    # NOTE: per-title inverse-frequency detox was evaluated and REJECTED -- it
    # lifts copilot a hair but drops swe ~18pt coherent, because swe scaffolding
    # titles are often genuinely faithful (the activity truly recurs); aggregate
    # optimum is source-only balancing. Parameter-free: derived from counts alone.
    #
    # TITLE_SRC_ALPHA (default 0.0) is a single source-mass TEMPERATURE: per-row
    # weight = freq^(alpha-1), so total per-source mass ~ freq^alpha.
    #   alpha=0  -> equal per source (the validated default; freq^-1).
    #   alpha<0  -> the LARGER (swe) corpus drops BELOW parity toward the organic
    #               source, since organic frameworks are under-represented among
    #               sources (swe-agent is the lone scaffolded outlier). e.g.
    #               alpha=-0.5 -> swe ~29% mass, alpha=-1 -> swe ~14%.
    # Still parameter-free in spirit: one principled knob, no per-title tuning.
    src_alpha = float(os.environ.get("TITLE_SRC_ALPHA", "0.0"))
    policy = os.environ.get("TITLE_BALANCE", "alpha")

    def _source_weights(freq: dict) -> tuple[dict, dict]:
        """Per-source row weight + intended mass fraction, by policy, within a
        homogeneous group. organic-parity (>=3 sources): cap the lone dominant
        source at 0.5 vs the rest combined, splitting the remainder by natural
        frequency; else freq^(alpha-1) temperature (alpha=0 = equal-per-source).
        Parameter-free: derived from counts alone (no source tag, no tuned knob)."""
        if policy == "organic-parity" and len(freq) >= 3:
            big = max(freq, key=freq.get)
            rest = [s for s in freq if s != big]
            rest_total = sum(freq[s] for s in rest)
            target = {big: 0.5}
            for s in rest:
                target[s] = 0.5 * freq[s] / rest_total
            return ({s: target[s] / freq[s] for s in freq}, {s: round(target[s], 3) for s in freq})
        mass = {s: freq[s] ** src_alpha for s in freq}
        tot = sum(mass.values())
        return (
            {s: freq[s] ** (src_alpha - 1.0) for s in freq},
            {s: round(mass[s] / tot, 3) for s in freq},
        )

    # (src, task)-parity. Distilling Step-by-Step weights the label and rationale
    # losses EQUALLY; we realize that by giving each TASK equal total sampling mass
    # (1/T from the data's own distinct task count), then applying the source
    # policy WITHIN each task. With a single task (no `task` column, e.g. the
    # title-only datasets) this collapses to the validated source-only sampler --
    # identical weights, backward compatible. Parameter-free throughout.
    tasks = sorted(tr["task"].dropna().unique().tolist()) if "task" in tr.columns else []
    n_task = max(1, len(tasks))
    _frac: dict = {}
    if tasks:
        srcs = tr.src.tolist()
        tcol = tr["task"].tolist()
        weights = [0.0] * len(tr)
        for t in tasks:
            freq_t = tr.src[tr["task"] == t].value_counts().to_dict()
            _, sf = _source_weights(freq_t)
            # Each task gets equal TOTAL mass (1/n_task) -- DSS weights the label
            # and rationale objectives equally. Within a task, sources follow the
            # policy fraction `sf` (sums to 1). Per-row weight sf/freq normalizes a
            # source to its fraction; /n_task normalizes the task to 1/n_task. Using
            # the FRACTION (not the raw freq^(alpha-1) weight) is what keeps task
            # mass independent of how many sources a task happens to contain.
            for i in range(len(tr)):
                if tcol[i] == t:
                    weights[i] = sf[srcs[i]] / freq_t[srcs[i]] / n_task
            _frac[t] = sf
    else:
        sw, _frac = _source_weights(tr.src.value_counts().to_dict())
        weights = [sw[s] for s in tr.src]
    sampler = WeightedRandomSampler(weights, num_samples=len(tr), replacement=True)
    uniq = tr.groupby("src").gold.nunique().to_dict()
    print(
        f"source mix (raw): {tr.src.value_counts().to_dict()} | distinct titles: {uniq} "
        f"-> policy={policy} alpha={src_alpha} tasks={tasks or ['(none)']} "
        f"mass fraction: {_frac}"
    )

    dl = DataLoader(DS(tr), batch_size=bs, sampler=sampler, collate_fn=collate)
    mdl = T5ForConditionalGeneration.from_pretrained(BASE_MODEL).to(dev)
    opt = torch.optim.AdamW(mdl.parameters(), lr=lr)
    steps = len(dl) * epochs
    sched = get_linear_schedule_with_warmup(opt, int(0.06 * steps), steps)

    with start_run(
        EXPERIMENT,
        run_name=f"train:{os.path.basename(BASE_MODEL)}",
        tags={"phase": "train", "base_model": BASE_MODEL},
    ):
        log_yaml_params(EXPERIMENT_YAML)
        mlflow.log_params(
            {
                "base_model": BASE_MODEL,
                "sources": _srcs or "all",
                "src_alpha": src_alpha,
                "balance_policy": policy,
                "epochs": epochs,
                "bs": bs,
                "lr": lr,
                "n_train": len(tr),
                "max_src": MAX_SRC,
                "max_tgt": MAX_TGT,
                "dataset": os.path.basename(DATASET),
            }
        )
        for _k, _v in _frac.items():
            if isinstance(_v, dict):  # (src,task)-parity: {task: {src: frac}}
                for _s, _f in _v.items():
                    mlflow.log_param(f"source_mass_fraction.{_k}.{_s}", _f)
            else:  # source-only: {src: frac}
                mlflow.log_param(f"source_mass_fraction.{_k}", _v)

        mdl.train()
        t0 = time.time()
        last_loss = 0.0
        for ep in range(epochs):
            tot, nb = 0.0, 0
            for batch in dl:
                batch = {k: v.to(dev) for k, v in batch.items()}
                out = mdl(**batch)
                out.loss.backward()
                torch.nn.utils.clip_grad_norm_(mdl.parameters(), 1.0)
                opt.step()
                sched.step()
                opt.zero_grad()
                tot += out.loss.item()
                nb += 1
            last_loss = tot / nb
            mlflow.log_metric("train_loss", last_loss, step=ep + 1)
            print(
                f"epoch {ep + 1}/{epochs}  loss {last_loss:.4f}  [{time.time() - t0:.0f}s]",
                flush=True,
            )

        mlflow.log_metric("final_train_loss", last_loss)
        mlflow.log_metric("train_seconds", time.time() - t0)
        os.makedirs(MODEL_DIR, exist_ok=True)
        mdl.save_pretrained(MODEL_DIR)
        tok.save_pretrained(MODEL_DIR)
        print(f"saved -> {MODEL_DIR}")


# ----------------------------------------------------------------------------- eval
def _rouge1(pred: str, gold: str) -> float:
    """stopword-stripped token-set F1 (matches the compose baseline metric)."""
    p = {t for t in toks(pred) if t not in STOP}
    g = {t for t in toks(gold) if t not in STOP}
    if not p or not g:
        return 0.0
    inter = len(p & g)
    if not inter:
        return 0.0
    prec, rec = inter / len(p), inter / len(g)
    return 2 * prec * rec / (prec + rec)


def evaluate():
    _utf8()
    import torch
    from transformers import AutoTokenizer, T5ForConditionalGeneration

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    df = pd.read_parquet(DATASET)
    ho = df[df.split == "heldout"].reset_index(drop=True)

    tok = AutoTokenizer.from_pretrained(MODEL_DIR)
    mdl = T5ForConditionalGeneration.from_pretrained(MODEL_DIR).to(dev).eval()

    NB = 5  # beams == returned sequences; alt beams feed sibling-dedup at render
    preds, alts = [], []
    with torch.no_grad():
        for i in range(0, len(ho), 64):
            chunk = ho.iloc[i : i + 64]
            pref = chunk.prefix if "prefix" in chunk.columns else None
            xs = [
                (pref.iloc[j] if pref is not None and isinstance(pref.iloc[j], str) else PREFIX) + c
                for j, c in enumerate(chunk.ctx)
            ]
            enc = tok(
                xs, padding=True, truncation=True, max_length=MAX_SRC, return_tensors="pt"
            ).to(dev)
            out = mdl.generate(
                **enc,
                max_new_tokens=MAX_TGT,
                num_beams=NB,
                num_return_sequences=NB,
                no_repeat_ngram_size=2,
                repetition_penalty=1.3,
                length_penalty=0.8,
                early_stopping=True,
            )
            dec = [s.strip() for s in tok.batch_decode(out, skip_special_tokens=True)]
            for j in range(len(chunk)):
                cand = dec[j * NB : (j + 1) * NB]
                preds.append(cand[0])
                alts.append(cand)
    ho = ho.assign(pred=preds, alts=alts)
    ho["r1"] = [_rouge1(p, g) for p, g in zip(ho.pred, ho.gold)]

    with start_run(EXPERIMENT, run_name="eval", tags={"phase": "eval"}):
        log_yaml_params(EXPERIMENT_YAML)
        mlflow.log_param("base_model", BASE_MODEL)
        mlflow.log_metric("rouge1_overall", float(ho.r1.mean()))
        mlflow.log_metric("n_heldout", len(ho))
        for _src, _g in ho.groupby("src"):
            mlflow.log_metric(f"rouge1_{_src}", float(_g.r1.mean()))
        mlflow.log_metric("unique_pred_rate", ho.pred.str.lower().nunique() / max(1, len(ho)))
        mlflow.log_metric("empty_preds", int((ho.pred.str.len() == 0).sum()))

    print("\n================= HELD-OUT ROUGE-1 (stopword-stripped F1) =================")
    print(f"  overall            : {ho.r1.mean():.3f}  (n={len(ho)})")
    for src, g in ho.groupby("src"):
        print(f"  {src:<20}: {g.r1.mean():.3f}  (n={len(g)})")
    for tier, g in ho.groupby("tier"):
        print(f"  tier={tier:<14}: {g.r1.mean():.3f}  (n={len(g)})")
    uniq = ho.pred.str.lower().nunique() / max(1, len(ho))
    print(
        f"  unique-pred rate   : {uniq:.3f}  (gold {ho.gold.str.lower().nunique() / len(ho):.3f})"
    )
    empties = (ho.pred.str.len() == 0).sum()
    print(f"  empty preds        : {empties}")
    print("  extractive baseline: swe-agent .211 / copilot .147 (compose oracle)")
    print("  prior fine-tune    : overall .407 / swe .445 / copilot .151")

    # which source's trees to render for the human test (default: copilot help-out)
    render_src = os.environ.get("RENDER_SRC", "copilot-cli-native")

    def dedup_pick(used, cands):
        """sibling-dedup: first beam alternate not already used in this session."""
        for c in cands:
            if c.lower() not in used:
                used.add(c.lower())
                return c
        return cands[0]

    print(f"\n========== SAMPLE HELD-OUT TREES [{render_src}] (GEN vs gold) ==========")
    print("  (GEN uses sibling-dedup across alternate beams within a session;")
    print("   activities capped per session for readability)")
    ACT_CAP = int(os.environ.get("ACT_CAP", "6"))
    shown = 0
    sub = ho[ho.src == render_src]
    for sid, rowz in sub.groupby("sid"):
        acts = {}
        for _, it in rowz.iterrows():
            acts.setdefault(it.aid, []).append(it)
        if len(acts) < 2:
            continue
        used = set()
        aids = sorted(acts, key=lambda a: min(x.order for x in acts[a]))
        ntot = len(aids)
        aids = aids[:ACT_CAP]
        print(f"\n SESSION  [{render_src}]  {sid}  ({ntot} activities, showing {len(aids)})")
        for ai, aid in enumerate(aids):
            grp = sorted(acts[aid], key=lambda x: x.order)
            act = next((x for x in grp if x.tier == "activity"), None)
            steps = [x for x in grp if x.tier == "step"]
            last = ai == len(aids) - 1
            abr = "└─" if last else "├─"
            if act is not None:
                g = dedup_pick(used, list(act.alts))
                print(f" {abr} ACTIVITY  GEN : {g!r}")
                print(f" {'  ' if last else '│ '}            gold: {act.gold!r}")
            pad = "   " if last else "│  "
            for si, st in enumerate(steps):
                sbr = "└─" if si == len(steps) - 1 else "├─"
                g = dedup_pick(used, list(st.alts))
                print(f" {pad}{sbr} step  GEN : {g!r}")
                cont = "   " if si == len(steps) - 1 else "│  "
                print(f" {pad}{cont}        gold: {st.gold!r}")
        shown += 1
        if shown >= 8:
            break


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    {"build": build, "train": train, "eval": evaluate}[cmd]()
