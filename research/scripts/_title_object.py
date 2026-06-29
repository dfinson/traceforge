"""Is the OBJECT slot recoverable AND specific? A good title needs a concrete,
salient object ("NOT NULL rule", "base.py"), not a generic placeholder ("the bug").

This spike measures, over gold activity/step titles:
  (1) GENERIC-via-rarity: do generic object nouns (bug/issue/fix/code) fall out as
      low-IDF automatically (so "avoid generic" is LEARNABLE, not a hand banlist)?
  (2) MEANINGFUL-RATE: what fraction of gold titles carry >=1 specific object token
      (code-shaped identifier OR high-IDF content word)?
  (3) RECOVERABILITY: for those, is the specific token present in the segment's
      extractable signal (binaries/structure/tool + payload identifiers/basenames)?

Run: cd research; $env:OMP_NUM_THREADS=4; .venv\\Scripts\\python.exe -u -m scripts._title_object
"""

from __future__ import annotations

import collections
import json
import math
import os
import re
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(ROOT, "data", "interim", "labeling-corpus")
TOC = os.path.join(ROOT, "data", "processed", "activity-step-toc.parquet")
SRC_DIR = {"swe-agent-nebius": "swe-agent-nebius", "copilot-cli-native": "copilot-cli-native"}

STOP = set(
    "the a an of to and in for with on is are be this that it we our us you your i "
    "let lets now first then next also will should can use via into from as at".split())

# code-shaped = concrete entity: has a dot-ext, snake/camel, ALLCAPS>=2, digit, slash
_CODESHAPE = re.compile(
    r"(\.\w{1,5}\b)|(_)|([a-z][A-Z])|(\b[A-Z]{2,}\b)|(\d)|(/)|(\bclass\b|\bfunction\b)")
_IDENT = re.compile(r"[A-Za-z_][\w./-]{2,}")


def toks(s):
    return [t for t in re.findall(r"[A-Za-z0-9_.\-/]+", (s or "").lower()) if len(t) > 1]


def content_toks(title):
    """object-side content tokens: drop the leading verb + stopwords."""
    ws = title.split()
    body = ws[1:] if ws else []
    return [t for t in toks(" ".join(body)) if t not in STOP]


def is_specific(tok, idf, idf_hi):
    return bool(_CODESHAPE.search(tok)) or idf.get(tok, idf_hi) >= idf_hi


def payload_entities(row):
    """concrete entities extractable from one event row. OBJECTS are things acted
    UPON -> file/binary names, structures, code identifiers. tool_name/action/
    capability are the ACTION (verb) side (fed to the verb classifier via
    cat_feats), so they're excluded here or they masquerade as objects
    ("Read view", view=the tool)."""
    ents = set()
    for col in ("binaries", "structure"):
        v = row.get(col)
        if isinstance(v, (list, tuple, np.ndarray)):
            ents |= {str(x).lower() for x in v if x is not None}
        elif v is not None and str(v) != "None":
            ents.add(str(v).lower())
    pj = row.get("payload_json")
    if isinstance(pj, str):
        # file basenames + code identifiers from the serialized payload
        for m in re.findall(r"[\w.\-]+\.\w{1,5}", pj):
            ents.add(os.path.basename(m).lower())
        for m in _IDENT.findall(pj):
            if _CODESHAPE.search(m):
                ents.add(m.lower())
    return {e for e in ents if len(e) > 1}


def main():
    toc = pd.read_parquet(TOC)
    toc = toc[toc.session_type == "agent"]

    # ---- IDF over gold title tokens (defines generic-by-rarity) ----
    all_titles = []
    for _, a in toc.iterrows():
        all_titles.append(a.activity_title)
        all_titles.extend(st["step_title"] for st in a.steps)
    all_titles = [t for t in all_titles if isinstance(t, str)]
    df = collections.Counter()
    for t in all_titles:
        df.update(set(toks(t)))
    N = len(all_titles)
    idf = {w: math.log(N / c) for w, c in df.items()}
    idf_hi = float(np.quantile(list(idf.values()), 0.5))  # median split generic/specific

    print("(1) lowest-IDF (most GENERIC) title nouns -- learned, not hand-typed:")
    generic = [w for w, _ in sorted(idf.items(), key=lambda kv: kv[1])
               if w not in STOP and not _CODESHAPE.search(w)][:30]
    print("   ", ", ".join(generic))

    # ---- build segments with corpus windows ----
    meaningful = 0
    total = 0
    recoverable = 0
    spec_total = 0
    per_src = collections.defaultdict(lambda: [0, 0, 0, 0, 0, 0, 0, 0])  # see cols below
    examples = []

    for src, sub in toc.groupby("source"):
        d = SRC_DIR.get(src)
        if d is None:
            continue
        for sid, srows in sub.groupby("session_id"):
            p = os.path.join(CORPUS, d, f"{sid}.parquet")
            if not os.path.exists(p):
                continue
            cdf = pd.read_parquet(p).sort_values("seq")
            seqmap = dict(zip(cdf.event_id, cdf.seq))
            recs = list(cdf.to_dict("records"))

            def window(s_id, e_id):
                s, e = seqmap.get(s_id, 0), seqmap.get(e_id, 0)
                return [r for r in recs if s <= r["seq"] <= e]

            for _, a in srows.iterrows():
                items = [(a.start_event_id, a.end_event_id, a.activity_title)]
                items += [(st["start_event_id"], st["end_event_id"], st["step_title"]) for st in a.steps]
                for s_id, e_id, gold in items:
                    if not isinstance(gold, str):
                        continue
                    total += 1
                    per_src[src][0] += 1
                    ct = content_toks(gold)
                    spec = [t for t in ct if is_specific(t, idf, idf_hi)]
                    concrete = [t for t in ct if _CODESHAPE.search(t)]
                    topical = [t for t in spec if not _CODESHAPE.search(t)]
                    if spec:
                        meaningful += 1
                        per_src[src][1] += 1
                        spec_total += len(spec)
                        ents = set()
                        for r in window(s_id, e_id):
                            ents |= payload_entities(r)
                        def _hit(tokens):
                            return any(
                                any(s == e or s in e or e in s for e in ents)
                                for s in tokens)
                        if _hit(spec):
                            recoverable += 1
                            per_src[src][3] += 1
                        elif len(examples) < 14:
                            examples.append((src, gold, spec, list(ents)[:8]))
                        if concrete:
                            per_src[src][4] += 1
                            if _hit(concrete):
                                per_src[src][5] += 1
                        if topical and not concrete:
                            per_src[src][6] += 1
                            if _hit(topical):
                                per_src[src][7] += 1
    print(f"\n(2) MEANINGFUL-RATE: gold titles w/ >=1 specific object token: "
          f"{meaningful}/{total} = {meaningful/total:.1%}")
    print(f"(3) RECOVERABILITY: specific token present in segment signal: "
          f"{recoverable}/{meaningful} = {recoverable/max(meaningful,1):.1%}")
    print("\nby source (total | meaningful% | recover%):")
    for src, c in per_src.items():
        tt, mm, _ss, rr = c[0], c[1], c[2], c[3]
        print(f"   {src:22s} {tt:5d} | {mm/tt:6.1%} | {rr/max(mm,1):6.1%}")
    print("\nCONCRETE (code-shape) vs TOPICAL object recoverability:")
    for src, c in per_src.items():
        conc, conc_hit, top, top_hit = c[4], c[5], c[6], c[7]
        print(f"   {src:22s} concrete {conc_hit}/{conc}={conc_hit/max(conc,1):.0%}"
              f"   topical {top_hit}/{top}={top_hit/max(top,1):.0%}")

    print("\n==== NON-recoverable examples (specific gold object not found in signal) ====")
    for src, gold, spec, ents in examples:
        print(f"  [{src}] GOLD={gold!r}\n     spec={spec}  seg_ents={ents}")


if __name__ == "__main__":
    main()
