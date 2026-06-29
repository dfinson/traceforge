"""Small OOD probe: can the served tracemill titler title CodePlane *jobs*?

CodePlane stores, per autonomous job, the initial user PROMPT and a short human
``title`` (e.g. prompt "...Save reverts to default, what's going on?" -> title
"Settings save reverts to defaults"). That is the same task our titler does, but
on a DIFFERENT input shape: the titler was trained on a distilled span context
(files/actions/notes), not a raw natural-language request. This script feeds the
raw prompts through the production TitleModel and scores the output against the
human gold, qualitatively (side-by-side) and quantitatively (content-token F1 +
ROUGE-1/L F, leading-verb gerund-vs-noun style split).

Run (repo root, CPU-only, torch-free root venv):
  cd research
  $env:CUDA_VISIBLE_DEVICES="-1"; $env:PYTHONIOENCODING="utf-8"
  ..\\.venv\\Scripts\\python.exe -u -m scripts._title_codeplane_test --n 30
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from tracemill.title.inference import TitleModel

_PAIRS = (Path(__file__).resolve().parent.parent
          / "data" / "interim" / "codeplane_title_pairs.json")
_STOP = {
    "the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "with", "from",
    "into", "by", "at", "as", "its", "their", "this", "that", "these", "those",
    "is", "are", "be", "it", "so", "when", "i", "my", "we", "you", "should",
}
_WORD = re.compile(r"[A-Za-z0-9_./\\-]+")


def _content(s: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(s) if w.lower() not in _STOP]


def _f1(pred: list[str], gold: list[str]) -> float:
    if not pred or not gold:
        return 0.0
    ps, gs = set(pred), set(gold)
    inter = len(ps & gs)
    if inter == 0:
        return 0.0
    p = inter / len(ps)
    r = inter / len(gs)
    return 2 * p * r / (p + r)


def _lcs(a: list[str], b: list[str]) -> int:
    dp = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) - 1, -1, -1):
        for j in range(len(b) - 1, -1, -1):
            dp[i][j] = (dp[i + 1][j + 1] + 1 if a[i] == b[j]
                        else max(dp[i + 1][j], dp[i][j + 1]))
    return dp[0][0]


def _rouge_l(pred: list[str], gold: list[str]) -> float:
    if not pred or not gold:
        return 0.0
    lcs = _lcs(pred, gold)
    if lcs == 0:
        return 0.0
    p, r = lcs / len(pred), lcs / len(gold)
    return 2 * p * r / (p + r)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=30, help="number of pairs to probe")
    ap.add_argument("--show", type=int, default=30, help="rows to print")
    ap.add_argument("--model-dir", default=None)
    args = ap.parse_args()

    pairs = json.loads(_PAIRS.read_text(encoding="utf-8"))
    if args.n:
        pairs = pairs[: args.n]
    model = TitleModel.load(args.model_dir, threads=1)

    f1s: list[float] = []
    rls: list[float] = []
    rows = []
    for x in pairs:
        pred = model.title(x["prompt"])
        gold = x["gold"]
        pc, gc = _content(pred), _content(gold)
        f1 = _f1(pc, gc)
        rl = _rouge_l(pc, gc)
        f1s.append(f1)
        rls.append(rl)
        rows.append((f1, gold, pred, x["prompt"]))

    print(f"\n=========  CODEPLANE JOB-TITLE PROBE  (served titler, n={len(pairs)})  =========")
    print(f"{'F1':>5}  {'GOLD (human)':38}  PREDICTED (ours)")
    print("-" * 92)
    for f1, gold, pred, _ in rows[: args.show]:
        print(f"{f1:5.2f}  {gold[:38]:38}  {pred}")

    n = len(f1s)
    mf1 = sum(f1s) / n if n else 0.0
    mrl = sum(rls) / n if n else 0.0
    any_overlap = sum(1 for f in f1s if f > 0) / n if n else 0.0
    strong = sum(1 for f in f1s if f >= 0.5) / n if n else 0.0
    print("-" * 92)
    print(f"mean content-token F1 : {mf1:.3f}")
    print(f"mean ROUGE-L F        : {mrl:.3f}")
    print(f"any content overlap   : {any_overlap:5.1%}")
    print(f">=0.5 F1 (close hit)  : {strong:5.1%}")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
