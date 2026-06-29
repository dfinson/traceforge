"""Does a small, learned *title-bearing sentence* classifier beat the regex
sentence-picker — and generalise across sessions/frameworks?

Self-supervised: for each segment we take its narration sentences (assistant/user
messages) and embed them (model2vec, same stack as phase/boundary). The gold
title gives a weak label — the sentence most similar to the gold title is the
positive "title-bearing" sentence, the rest are negatives. We train a logistic
regression on [embedding + cheap surface features] and evaluate OOF with
GroupKFold(session) so nothing leaks across sessions.

Reports:
  * ORACLE  — pick the gold-nearest sentence (ceiling for sentence selection)
  * REGEX   — the intent/result cue heuristic (current baseline)
  * LEARNED — OOF logistic-regression sentence picker

Run:  cd research; $env:OMP_NUM_THREADS=4; .venv\\Scripts\\python.exe -u -m scripts._title_sent
"""

from __future__ import annotations

import json
import os
import re
import sys

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(ROOT, "data", "interim", "labeling-corpus")
TOC = os.path.join(ROOT, "data", "processed", "activity-step-toc.parquet")
SRC_DIR = {"swe-agent-nebius": "swe-agent-nebius", "copilot-cli-native": "copilot-cli-native"}

STOP = set(
    "the a an of to and in for with on is are be this that it we our us you your i "
    "let lets now first then next also will should can add code file files use".split()
)
_JUNK = re.compile(r"\btoolu_[\w]+\b|\b[0-9a-f]{12,}\b", re.I)
_INTENT = re.compile(
    r"\b(let'?s|let me|i'?ll|i will|we'?ll|we will|we need to|i need to|we should|"
    r"i should|going to|i want to|we want to|in order to|now i|now we)\b", re.I)
_RESULT = re.compile(
    r"\b(has been|have been|appears|confirms?|confirmed|succeeded|successfully|"
    r"the output|the result|now that|great|perfect|it seems|this (?:means|shows|confirms))\b",
    re.I)
_LEAD = re.compile(r"^(first|next|then|now|also|finally|so|ok|okay|alright)[,:]?\s+", re.I)
_OPENER = re.compile(
    r"^(let'?s|lets|we'?ll|we will|we need to|we should|we can|we are going to|"
    r"i'?ll|i will|i'?m going to|let me|i need to|i should|i want to|we want to|to)\s+", re.I)


def tokset(s):
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t not in STOP and len(t) > 1}


def rouge1(c, g):
    cs, gs = tokset(c), tokset(g)
    if not cs or not gs:
        return 0.0
    inter = len(cs & gs)
    return 0.0 if not inter else 2 * (inter / len(cs)) * (inter / len(gs)) / (inter / len(cs) + inter / len(gs))


def lst(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple, np.ndarray)):
        return [x for x in list(v) if x is not None]
    return [v]


def payload_strings(row):
    pj = row.get("payload_json")
    if not isinstance(pj, str):
        return ""
    try:
        o = json.loads(pj)
    except Exception:
        return pj

    def it(o):
        if isinstance(o, str):
            yield o
        elif isinstance(o, dict):
            for v in o.values():
                yield from it(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                yield from it(v)

    return _JUNK.sub(" ", " ".join(it(o)))


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?\n])\s+", (text or "").strip()) if s.strip()]


_ABS_PATH = re.compile(r"(?:[A-Za-z]:\\|/)[\w\\/.\- ]*?([\w.\-]+\.\w{1,5})")  # -> basename
_TOOLSER = re.compile(r"\b(function|report_intent|toolu_\w+|risk-v\d|end_of_edit)\b", re.I)
# imperative tool/command tokens that signal the prose has ended and a command begins
_TOOLWORD = re.compile(
    r"\b(view|edit|str_replace|powershell|bash|sh|cat|run|grep|sed|curl|python|"
    r"node|npm|pip|git|ls|cd|mkdir|rm|cp|mv|echo|read|write|create|apply_patch)\b", re.I)


def denoise(text: str) -> str:
    """Strip the tool-call serialization Copilot interleaves into assistant prose
    (absolute paths -> basename, 'function'/'report_intent' markers, ids)."""
    if not text:
        return ""
    text = _ABS_PATH.sub(r"\1", text)        # collapse long paths to basename
    # cut a sentence at the first tool-serialization marker (prose precedes it)
    parts = re.split(r"\b(?:function|end_of_edit)\b", text, flags=re.I)
    text = parts[0]
    text = re.sub(r"\breport_intent\b", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def _cut_command_tail(s: str) -> str:
    """Drop a trailing command after a colon/dash when the tail looks like a tool
    invocation ("Check its contents: view mcp-config.json" -> "Check its contents")."""
    m = re.search(r"\s*[:\-\u2014]\s+", s)
    if m:
        head, tail = s[: m.start()], s[m.end():]
        # only cut if head is real prose and tail begins like a command
        if len(head.split()) >= 3 and _TOOLWORD.match(tail.strip()):
            return head
    # also cut an inline command that starts mid-sentence ("...json: edit foo bar")
    m2 = _TOOLWORD.search(s)
    if m2 and m2.start() > 0:
        before = s[: m2.start()].rstrip(" :,-`\"'")
        if len(before.split()) >= 3 and re.search(r"[:`]", s[: m2.start()]):
            return before
    return s


def clean_headline(sent, max_words=9):
    s = denoise(sent)
    s = _cut_command_tail(s)
    s = _OPENER.sub("", _LEAD.sub("", s).strip()).strip()
    s = re.sub(r"^(that |which |where |so that )", "", s, flags=re.I).strip()
    s = s.rstrip(" ,.:;-`\"'")
    if not s:
        return None
    w = s.split()
    s = " ".join(w[:max_words]).rstrip(" ,.:;-`\"'")
    return (s[0].upper() + s[1:]) if len(s) >= 3 else None


def extract_intent(row):
    """Return the gerund-form intent string from a report_intent tool call
    (arguments.intent), which is gold-quality title text, else None."""
    if "report_intent" not in str(row.get("tool_name", "")).lower():
        pj = row.get("payload_json")
        if not isinstance(pj, str) or "report_intent" not in pj:
            return None
    pj = row.get("payload_json")
    if not isinstance(pj, str):
        return None
    try:
        o = json.loads(pj)
    except Exception:
        return None
    if isinstance(o, dict) and str(o.get("tool_name", "")).lower() == "report_intent":
        intent = (o.get("arguments") or {}).get("intent")
        if isinstance(intent, str) and len(intent.split()) >= 2:
            return intent.strip()
    return None


def seg_sentences(rows):
    """Candidate title sentences in the segment, each tagged with whether it is a
    stated report_intent (gerund-form, near-gold) vs ordinary narration."""
    out = []  # (sentence, is_intent_call)
    for r in rows:
        intent = extract_intent(r)
        if intent is not None:
            out.append((denoise(intent), 1))
        k = str(r["kind"])
        if k.startswith("message.assistant") or k.startswith("message.user"):
            for s in sentences(payload_strings(r)):
                s = denoise(s)
                wc = len(s.split())
                if 3 <= wc <= 40:
                    out.append((s, 0))
    return out[:25]


N_SURF = 12


def surface_feats(sent, idx, n, cent_sim=0.0, cent_rank=0.0, is_central=0.0,
                  neigh_max=0.0, is_intent_call=0.0):
    wc = len(sent.split())
    return np.array([
        1.0 if _INTENT.search(sent) else 0.0,
        1.0 if _RESULT.search(sent) else 0.0,
        1.0 if idx == 0 else 0.0,
        idx / max(n - 1, 1),
        min(wc, 20) / 20.0,
        1.0 if "`" in sent or re.search(r"\.\w{1,4}\b", sent) else 0.0,
        # centrality: the title sentence is usually the most representative one
        cent_sim,
        cent_rank,
        is_central,
        neigh_max,
        # very short / very long sentences are rarely good titles
        1.0 if 4 <= wc <= 14 else 0.0,
        # stated report_intent: gerund-form, near-gold title text
        is_intent_call,
    ], dtype=np.float32)


def main():
    from tracemill.phase.features import embed_texts

    toc = pd.read_parquet(TOC)
    toc = toc[toc.session_type == "agent"]

    # Collect per-segment candidate sentences + gold title.
    segs = []  # dict(session, source, tier, gold, sents[list])
    for src, sub in toc.groupby("source"):
        d = SRC_DIR.get(src)
        if d is None:
            continue
        for sid, srows in sub.groupby("session_id"):
            p = os.path.join(CORPUS, d, f"{sid}.parquet")
            if not os.path.exists(p):
                continue
            df = pd.read_parquet(p).sort_values("seq")
            seqmap = dict(zip(df.event_id, df.seq))
            recs = list(df.to_dict("records"))

            def window(s_id, e_id):
                s, e = seqmap.get(s_id, 0), seqmap.get(e_id, 0)
                return [r for r in recs if s <= r["seq"] <= e]

            for _, a in srows.iterrows():
                for s_id, e_id, tier, gold in [
                    (a.start_event_id, a.end_event_id, "activity", a.activity_title),
                    *[(st["start_event_id"], st["end_event_id"], "step", st["step_title"]) for st in a.steps],
                ]:
                    sents = seg_sentences(window(s_id, e_id))
                    if sents:
                        segs.append(dict(session=sid, source=src, tier=tier, gold=gold, sents=sents))
    print(f"segments with narration: {len(segs)}", file=sys.stderr)

    # Embed all sentences + golds once.
    all_sents, owner, is_intent = [], [], []
    for i, sg in enumerate(segs):
        for s, flag in sg["sents"]:
            all_sents.append(s)
            owner.append(i)
            is_intent.append(flag)
    is_intent = np.array(is_intent, np.float32)
    Es = embed_texts(all_sents).astype(np.float32)
    Eg = embed_texts([sg["gold"] for sg in segs]).astype(np.float32)
    En = Es / (np.linalg.norm(Es, axis=1, keepdims=True) + 1e-9)
    Eng = Eg / (np.linalg.norm(Eg, axis=1, keepdims=True) + 1e-9)

    owner = np.array(owner)
    # per-sentence cosine to its segment's gold (for weak labels + oracle)
    sim = (En * Eng[owner]).sum(1)

    # surface feats + design matrix
    surf = np.zeros((len(all_sents), N_SURF), np.float32)
    # weak label: argmax-sim sentence within each segment is positive
    y = np.zeros(len(all_sents), np.int8)
    groups = np.empty(len(all_sents), dtype=object)
    for i, sg in enumerate(segs):
        idxs = np.where(owner == i)[0]
        n = len(idxs)
        # segment centroid (mean of normalized embeddings) -> centrality signal
        cent = En[idxs].mean(0)
        cent /= np.linalg.norm(cent) + 1e-9
        csim = En[idxs] @ cent                       # each sentence vs centroid
        order = np.argsort(-csim)                     # high sim first
        rank = np.empty(n, np.float32)
        rank[order] = np.arange(n) / max(n - 1, 1)    # 0 = most central
        central_local = int(np.argmax(csim))
        # max cosine to any OTHER sentence in the segment (redundancy/centrality)
        G = En[idxs] @ En[idxs].T
        np.fill_diagonal(G, -1.0)
        neigh = G.max(1) if n > 1 else np.zeros(n, np.float32)
        for j, gi in enumerate(idxs):
            surf[gi] = surface_feats(
                all_sents[gi], j, n,
                cent_sim=float(csim[j]),
                cent_rank=1.0 - float(rank[j]),       # 1 = most central
                is_central=1.0 if j == central_local else 0.0,
                neigh_max=float(neigh[j]),
                is_intent_call=float(is_intent[gi]),
            )
            groups[gi] = sg["session"]
        best = idxs[np.argmax(sim[idxs])]
        y[best] = 1
    X = np.hstack([En, surf])

    # OOF learned picker
    from sklearn.preprocessing import StandardScaler

    oof = np.zeros(len(all_sents), np.float32)
    gkf = GroupKFold(n_splits=5)
    for tr, te in gkf.split(X, y, groups):
        # leak-free "title prototype": mean embedding of TRAIN-fold title sentences;
        # cosine to it is a learned title-likeness signal on the unit sphere.
        pos = tr[y[tr] == 1]
        proto = En[pos].mean(0)
        proto /= np.linalg.norm(proto) + 1e-9
        proto_feat = (En @ proto).astype(np.float32)[:, None]
        Xtr = np.hstack([X[tr], proto_feat[tr]])
        Xte = np.hstack([X[te], proto_feat[te]])
        sc = StandardScaler().fit(Xtr)
        clf = LogisticRegression(max_iter=2000, C=1.0, class_weight="balanced")
        clf.fit(sc.transform(Xtr), y[tr])
        oof[te] = clf.predict_proba(sc.transform(Xte))[:, 1]

    # Evaluate title quality per segment for ORACLE / REGEX / LEARNED.
    def regex_pick(idxs):
        best, bs = idxs[0], -1e9
        for j, gi in enumerate(idxs):
            s = all_sents[gi]
            sc = (3 if _INTENT.search(s) else 0) - (3 if _RESULT.search(s) else 0) - 0.15 * j
            if sc > bs:
                best, bs = gi, sc
        return best

    rows = []
    for i, sg in enumerate(segs):
        idxs = np.where(owner == i)[0]
        oracle = idxs[np.argmax(sim[idxs])]
        regex = regex_pick(idxs)
        learned = idxs[np.argmax(oof[idxs])]
        for name, gi in [("oracle", oracle), ("regex", regex), ("learned", learned)]:
            cand = clean_headline(all_sents[gi]) or all_sents[gi]
            rows.append((sg["source"], sg["tier"], name,
                         rouge1(cand, sg["gold"]),
                         float((En[gi] * Eng[i]).sum())))
    R = pd.DataFrame(rows, columns=["source", "tier", "picker", "rouge1", "cos"])
    print("\n==== title quality by picker (mean) ====")
    print(R.groupby(["tier", "picker"])[["rouge1", "cos"]].mean().round(3).to_string())
    print("\n==== by source/tier ====")
    print(R.groupby(["source", "tier", "picker"])[["rouge1", "cos"]].mean().round(3).to_string())

    # sample learned picks
    print("\n==== learned-picker samples ====")
    shown = 0
    for i, sg in enumerate(segs):
        if sg["tier"] != "step":
            continue
        idxs = np.where(owner == i)[0]
        gi = idxs[np.argmax(oof[idxs])]
        print(f"  GOLD={sg['gold']!r}\n  PICK={clean_headline(all_sents[gi])!r}")
        shown += 1
        if shown >= 12:
            break


if __name__ == "__main__":
    main()
