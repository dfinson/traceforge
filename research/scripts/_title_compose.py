"""End-to-end template+slot title composition vs gold and vs sentence-selection.

Pipeline (all learned, OOF GroupKFold(session), framework-agnostic, no LLM):
  L1 TEMPLATE classifier : segment structured+narration feats -> template label
                           (#clauses x has_PP), i.e. "when to use which".
  L2a VERB classifier     : feats -> verb over learned closed set (top-1/top-3).
  L2b OBJECT ranker       : per-candidate feats (idf/code-shape/segment-freq/
                            entity/centroid-cos) -> is-gold-object; rank, gate by
                            specificity. Candidates = concrete entities (structured)
                            UNION topical NPs (narration).
  COMPOSE                 : VERB OBJ [+ 2nd clause/PP per template].

Baselines on the SAME segments: sentence-selection (centroid-nearest narration
sentence, cleaned) and ORACLE sentence (gold-nearest, ceiling).

Run: cd research; $env:OMP_NUM_THREADS=4; .venv\\Scripts\\python.exe -u -m scripts._title_compose
"""

from __future__ import annotations

import collections
import math
import os
import re
import sys

import nltk
import numpy as np
import pandas as pd
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold

for _p in ["averaged_perceptron_tagger", "averaged_perceptron_tagger_eng", "punkt", "punkt_tab"]:
    nltk.download(_p, quiet=True)

from scripts._title_object import (  # noqa: E402
    _CODESHAPE, STOP, content_toks, payload_entities, toks)
from scripts._title_templates import slot_seq  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(ROOT, "data", "interim", "labeling-corpus")
TOC = os.path.join(ROOT, "data", "processed", "activity-step-toc.parquet")
# claude-cli per-session parquets live at the labeling-corpus ROOT (no subdir),
# so the empty string resolves os.path.join(CORPUS, "", "<sid>.parquet") to the
# top-level path. Organic Claude-Code gold, folded in as a 2nd real agent source.
SRC_DIR = {"swe-agent-nebius": "swe-agent-nebius", "copilot-cli-native": "copilot-cli-native",
           "claude-cli": ""}

_ABS_PATH = re.compile(r"(?:[A-Za-z]:\\|/)[\w\\/.\- ]*?([\w.\-]+\.\w{1,5})")


def denoise(t):
    if not t:
        return ""
    t = _ABS_PATH.sub(r"\1", t)
    t = re.split(r"\b(?:function|end_of_edit)\b", t, flags=re.I)[0]
    t = re.sub(r"\breport_intent\b", "", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip()


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?\n])\s+", (text or "").strip()) if s.strip()]


def payload_text(row):
    import json
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
    return " ".join(it(o))


def tool_arg_text(row):
    """text from the tool-call ARGUMENTS only (high-salience: what the agent
    chose to act on), not the tool output."""
    import json
    pj = row.get("payload_json")
    if not isinstance(pj, str):
        return ""
    try:
        o = json.loads(pj)
    except Exception:
        return ""
    args = o.get("arguments") if isinstance(o, dict) else None
    if not isinstance(args, dict):
        return ""
    vals = [str(v) for v in args.values() if isinstance(v, (str, int, float))]
    return " ".join(vals)


_NOUN = ("NN", "NNS", "NNP", "NNPS", "JJ", "JJR", "JJS", "VBG", "CD", "FW")


def _gerund_bases(w):
    """candidate base forms of an -ing gerund: creating->create/creat,
    running->run/runn. Used to test membership in the imperative-verb lexicon."""
    if not (w.endswith("ing") and len(w) > 5):
        return ()
    stem = w[:-3]
    return (stem, stem + "e", stem[:-1])  # drop, drope (no), + doubled-consonant


def _leading_verb(tok, verbs):
    """True if a chunk-leading token is a verb the POS tagger mis-placed into a
    noun phrase: a capitalized keyword (NNP/NNPS) or an -ing gerund (VBG) whose
    base form is a known imperative verb."""
    w, t = tok
    wl = w.lower()
    if t in ("NNP", "NNPS"):
        return wl in verbs
    if t == "VBG":
        return any(b in verbs for b in _gerund_bases(wl))
    return False


def extract_nps(text, verbs=frozenset(), _cache={}):
    """maximal noun-phrase chunks (1-4 words) via offline POS; gold objects are
    phrases ("NOT NULL rule", "global MCP config"), not single tokens.

    POS GOTCHA: nltk's Penn tagger tags capitalized SQL/keyword verbs as NNP out
    of grammatical context ("CREATE INDEX" -> NNP NNP), so they chunk into noun
    phrases. We strip a LEADING token only when it's a known verb lemma AND tagged
    NNP/NNPS -> "create index" -> "index". Genuine noun-modifiers ("test file" ->
    NN) are preserved; real verbs ("run","update") are tagged VB and never chunk.
    A leading verbal GERUND (VBG) whose base is a known imperative verb is also
    stripped ("creating codeplane config" -> "codeplane config"); genuine gerund
    heads ("logging config", log not an imperative verb) are preserved."""
    from nltk import pos_tag, word_tokenize
    text = (text or "")[:600]
    ck = (text, id(verbs))
    if ck in _cache:
        return _cache[ck]
    nps, cur = [], []
    try:
        tags = pos_tag(word_tokenize(text))
    except Exception:
        tags = []
    for w, t in tags:
        if t in _NOUN and re.match(r"^[\w.\-/]+$", w):
            cur.append((w, t))
        else:
            if cur:
                nps.append(cur[-4:])
            cur = []
    if cur:
        nps.append(cur[-4:])
    out = []
    for chunk in nps:
        # Sanitize a noun chunk against POS mis-tags of verbs (the tagger labels
        # verbs as NN/NNP/VBG out of grammatical context). Repairing fragments is
        # worse than dropping, so: strip mis-tagged verbs at the EDGES, dedup
        # repeated tokens, and REJECT the candidate if a verb survives in the
        # interior (e.g. "agent md view agent md") -> let the ranker pick cleaner.
        while chunk and _leading_verb(chunk[0], verbs):
            chunk = chunk[1:]
        # trailing -ing participle is never a head noun ("changes using",
        # "server adding") -> strip unconditionally; leading gerund-nouns
        # ("logging config") are handled by the verb-lexicon test above.
        while chunk and (_leading_verb(chunk[-1], verbs) or chunk[-1][1] == "VBG"):
            chunk = chunk[:-1]
        words, seen = [], set()
        for w, t in chunk:                       # dedup repeated tokens, keep tags
            wl = w.lower()
            if wl not in seen:
                seen.add(wl)
                words.append((w, t))
        words = words[:3]                        # gold objects are 1-3 content words
        # drop if a mis-tagged verb survives in the INTERIOR (real tag NNP/VBG);
        # a plain-NN verb-lexicon word ("test file") is a genuine noun -> keep.
        if any(_leading_verb(wt, verbs) for wt in words):
            continue
        pl = " ".join(w for w, _ in words).lower().strip()
        if 1 <= len(pl.split()) <= 3 and len(pl) > 2:
            out.append(pl)
    _cache[ck] = out
    return out


def struct_counts(rows):
    """segment structural counts -> drive clause-count (1 vs 2) prediction."""
    tools, acts, files = set(), set(), set()
    n_call = n_msg = 0
    effects = []
    for r in rows:
        tn = r.get("tool_name")
        if tn is not None and str(tn) != "None":
            tools.add(str(tn).lower())
            n_call += 1
        a = r.get("action")
        if isinstance(a, (list, tuple, np.ndarray)):
            acts |= {str(x).lower() for x in a if x is not None}
        b = r.get("binaries")
        if isinstance(b, (list, tuple, np.ndarray)):
            files |= {str(x).lower() for x in b if x is not None}
        e = r.get("effect")
        if e is not None and str(e) != "None":
            effects.append(str(e))
        if str(r["kind"]).startswith("message.assistant"):
            n_msg += 1
    eff_trans = sum(1 for i in range(1, len(effects)) if effects[i] != effects[i - 1])
    return {
        "n_events": float(len(rows)),
        "n_calls": float(n_call),
        "n_tools": float(len(tools)),
        "n_actions": float(len(acts)),
        "n_files": float(len(files)),
        "n_asst_msgs": float(n_msg),
        "n_eff_trans": float(eff_trans),
    }


def narration(rows):
    out = []
    for r in rows:
        k = str(r["kind"])
        if k.startswith("message.assistant") or k.startswith("message.user"):
            for s in sentences(denoise(payload_text(r))):
                if 3 <= len(s.split()) <= 40:
                    out.append(s)
    return out[:25]


def cat_feats(rows):
    """categorical bag over structured fields (tool/action/cap/mech/effect)."""
    f = collections.Counter()
    for r in rows:
        for col, pre in [("tool_name", "tool"), ("mechanism", "mech"),
                         ("effect", "eff")]:
            v = r.get(col)
            if v is not None and str(v) != "None":
                f[f"{pre}:{str(v).lower()}"] += 1
        for col, pre in [("action", "act"), ("capability", "cap")]:
            v = r.get(col)
            if isinstance(v, (list, tuple, np.ndarray)):
                for x in v:
                    if x is not None:
                        f[f"{pre}:{str(x).lower()}"] += 1
    return {k: float(v) for k, v in f.items()}


def template_label(title, verbset):
    seq = slot_seq(title, verbset) or ""
    nverb = seq.count("VERB")
    has_pp = ("PP" in seq) or (" P " in f" {seq} ")
    if nverb == 0:
        return "0c"
    nclause = "2c" if nverb >= 2 else "1c"
    return nclause + ("+pp" if has_pp else "")


def learn_preps(titles_all):
    """LEARNED per-verb preposition (offline POS over gold titles): which function
    word joins a verb's object to its target, e.g. add->to, fix->in, test->with.
    Prepositions are a closed grammatical class (tag IN/TO), not a domain phrase
    list -> docs/08-compliant. Returns (verb->prep, global_default)."""
    by_verb = collections.defaultdict(collections.Counter)
    glob = collections.Counter()
    for t in titles_all:
        ws = t.split()
        if len(ws) < 3:
            continue
        verb = ws[0].lower()
        tags = nltk.pos_tag(ws)
        # first preposition AFTER the verb that still has an object word following it
        for k in range(1, len(tags) - 1):
            tag = tags[k][1]
            if tag in ("IN", "TO"):
                prep = tags[k][0].lower()
                by_verb[verb][prep] += 1
                glob[prep] += 1
                break
    default = glob.most_common(1)[0][0] if glob else "for"
    prep_of = {v: c.most_common(1)[0][0] for v, c in by_verb.items()
               if sum(c.values()) >= 3}
    return prep_of, default


_JUNKTOK = re.compile(r"^(toolu_|tool_|hook_|shellid|toolcallid)|^[0-9a-f]{8,}$|^[0-9a-f-]{16,}$", re.I)


def clean_cand(c):
    """normalise a candidate object token: basename paths, drop ids/guids/slugs."""
    c = c.strip().strip("`\"'(),.;:")
    if "/" in c or "\\" in c:
        c = re.split(r"[\\/]", c)[-1]
    c = c.lower()
    if not c or len(c) > 22 or _JUNKTOK.search(c):
        return None
    if not re.search(r"[aeiou]", c) and not _CODESHAPE.search(c):
        return None
    return c


def clean_phrase(p):
    """normalise a candidate phrase: basename tokens, drop id/guid tokens, <=4 words."""
    toks_out = []
    for w in str(p).split():
        w = w.strip("`\"'(),.;:")
        if "/" in w or "\\" in w:
            w = re.split(r"[\\/]", w)[-1]
        wl = w.lower()
        if not wl or len(wl) > 24 or _JUNKTOK.search(wl):
            continue
        if not re.search(r"[aeiou]", wl) and not _CODESHAPE.search(wl):
            continue
        toks_out.append(wl)
    toks_out = [t for t in toks_out if t not in STOP] or toks_out
    phr = " ".join(toks_out[:4]).strip()
    return phr if len(phr) > 2 else None


def build_cands(rows, narr_text, verbs=frozenset()):
    """typed object candidates from 3 sources: concrete entities, narration NPs,
    tool-argument NPs (high salience)."""
    meta = {}  # phrase -> dict(is_entity, in_args, first_pos)
    n = max(len(rows), 1)
    ents = set()
    for r in rows:
        ents |= payload_entities(r)
    for c in list(ents)[:30]:
        p = clean_phrase(c)
        if p:
            meta.setdefault(p, dict(is_entity=1.0, in_args=0.0, first_pos=1.0))
    # NPs from narration
    for p in extract_nps(narr_text, verbs):
        cp = clean_phrase(p)
        if cp:
            meta.setdefault(cp, dict(is_entity=0.0, in_args=0.0, first_pos=1.0))
    # NPs from tool arguments (what the agent chose to act on)
    for i, r in enumerate(rows):
        at = tool_arg_text(r)
        if not at:
            continue
        for p in extract_nps(at, verbs):
            cp = clean_phrase(p)
            if cp:
                m = meta.setdefault(cp, dict(is_entity=0.0, in_args=0.0, first_pos=i / n))
                m["in_args"] = 1.0
                m["first_pos"] = min(m["first_pos"], i / n)
    return list(meta.keys())[:30], meta


def clean_obj(s):
    return re.sub(r"\s+", " ", re.sub(r"[`\"'(),.;:]+", " ", s or "")).strip().lower()


def main():
    from tracemill.phase.features import embed_texts

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    toc = pd.read_parquet(TOC)
    toc = toc[toc.session_type == "agent"]

    # IDF over gold titles -> generic-by-rarity
    titles_all = []
    for _, a in toc.iterrows():
        titles_all.append(a.activity_title)
        titles_all.extend(st["step_title"] for st in a.steps)
    titles_all = [t for t in titles_all if isinstance(t, str)]
    dfc = collections.Counter()
    for t in titles_all:
        dfc.update(set(toks(t)))
    N = len(titles_all)
    idf = {w: math.log(N / c) for w, c in dfc.items()}
    idf_hi = float(np.quantile(list(idf.values()), 0.5))
    first = pd.Series([t.split()[0].lower() for t in titles_all if t.split()])
    vcnt = collections.Counter(first)
    verbset = {w for w, c in vcnt.items() if c >= 5}
    VERBS = [w for w, _ in vcnt.most_common(60)]
    VERBSET_TOP = set(VERBS)
    prep_of, prep_default = learn_preps(titles_all)
    print(f"  learned preps for {len(prep_of)} verbs, default={prep_default!r}",
          file=sys.stderr)

    # ---- build segments ----
    # LEARNED action vocabulary: every tool_name seen in the corpus. Tool names
    # are the ACTION (verb) side, so a candidate equal to a tool name is a
    # mis-tagged action, not an object ("view"/"read" in narration prose) -> drop.
    tool_vocab = set()
    segs = []
    for src, sub in toc.groupby("source"):
        d = SRC_DIR.get(src)
        if d is None:
            continue
        for sid, srows in sub.groupby("session_id"):
            p = os.path.join(CORPUS, d, f"{sid}.parquet")
            if not os.path.exists(p):
                continue
            cdf = pd.read_parquet(p).sort_values("seq")
            for tn in cdf.get("tool_name", pd.Series(dtype=object)).dropna().unique():
                if str(tn) != "None":
                    tool_vocab.add(str(tn).lower())
            seqmap = dict(zip(cdf.event_id, cdf.seq))
            recs = list(cdf.to_dict("records"))

            def window(s_id, e_id):
                s, e = seqmap.get(s_id, 0), seqmap.get(e_id, 0)
                return [r for r in recs if s <= r["seq"] <= e]

            for ai, (_, a) in enumerate(srows.iterrows()):
                aid = f"{sid}#{ai}"
                items = [(a.start_event_id, a.end_event_id, "activity", a.activity_title, 0)]
                items += [(st["start_event_id"], st["end_event_id"], "step", st["step_title"], si + 1)
                          for si, st in enumerate(a.steps)]
                for s_id, e_id, tier, gold, order in items:
                    if not isinstance(gold, str) or not gold.strip():
                        continue
                    rows = window(s_id, e_id)
                    if not rows:
                        continue
                    narr = narration(rows)
                    ntext = " ".join(narr)
                    cand, cmeta = build_cands(rows, ntext, verbset)
                    gobj = set(content_toks(gold))
                    gverbs = gold.split()
                    gverb = gverbs[0].lower() if gverbs else ""
                    ptext = {c: 0 for c in cand}
                    low_rows = [str(payload_text(r)).lower() for r in rows]
                    for c in cand:
                        ptext[c] = sum(1 for t in low_rows if c in t)
                    segs.append(dict(
                        session=sid, source=src, tier=tier, gold=gold,
                        aid=aid, order=order,
                        cat={**cat_feats(rows), **struct_counts(rows)},
                        narr=narr, cand=cand, cmeta=cmeta,
                        gobj=gobj, gverb=gverb,
                        tmpl=template_label(gold, verbset),
                        seg_freq=ptext))
    print(f"segments: {len(segs)}", file=sys.stderr)

    sessions = np.array([s["session"] for s in segs])

    # ---- embeddings: narration centroid per seg, gold, candidates ----
    narr_join = [" ".join(s["narr"]) if s["narr"] else s["gold"] for s in segs]
    Eseg = embed_texts(narr_join).astype(np.float32)
    Eseg /= np.linalg.norm(Eseg, axis=1, keepdims=True) + 1e-9
    Eg = embed_texts([s["gold"] for s in segs]).astype(np.float32)
    Eg /= np.linalg.norm(Eg, axis=1, keepdims=True) + 1e-9

    vocab = list({c for s in segs for c in s["cand"]})
    vidx = {c: i for i, c in enumerate(vocab)}
    Ev = embed_texts(vocab).astype(np.float32) if vocab else np.zeros((0, Eseg.shape[1]), np.float32)
    Ev /= np.linalg.norm(Ev, axis=1, keepdims=True) + 1e-9

    # ===== L1 TEMPLATE + L2a VERB: shared structured+narration features =====
    dv = DictVectorizer(sparse=False)
    Xcat = dv.fit_transform([s["cat"] for s in segs]).astype(np.float32)
    Xshared = np.hstack([Xcat, Eseg])
    tmpl_y = np.array([s["tmpl"] for s in segs])
    verb_y = np.array([s["gverb"] if s["gverb"] in VERBSET_TOP else "other" for s in segs])

    gkf = GroupKFold(n_splits=5)
    tmpl_pred = np.empty(len(segs), dtype=object)
    verb_top1 = np.zeros(len(segs), bool)
    verb_top3 = np.zeros(len(segs), bool)
    verb_pred = np.empty(len(segs), dtype=object)
    verb_pred2 = np.empty(len(segs), dtype=object)   # 2nd-clause verb for conjunctions
    for tr, te in gkf.split(Xshared, tmpl_y, sessions):
        ct = LogisticRegression(max_iter=1000, C=1.0, class_weight="balanced")
        ct.fit(Xshared[tr], tmpl_y[tr])
        tmpl_pred[te] = ct.predict(Xshared[te])
        cv = LogisticRegression(max_iter=1000, C=1.0, class_weight="balanced")
        cv.fit(Xshared[tr], verb_y[tr])
        proba = cv.predict_proba(Xshared[te])
        cls = cv.classes_
        order = np.argsort(-proba, axis=1)
        for k, i in enumerate(te):
            ranked = [cls[o] for o in order[k] if cls[o] != "other"]
            top = cls[order[k][:3]]
            verb_pred[i] = ranked[0] if ranked else "update"
            verb_pred2[i] = next((v for v in ranked[1:] if v != verb_pred[i]), "")
            verb_top1[i] = verb_y[i] == top[0]
            verb_top3[i] = verb_y[i] in set(top)

    print("\n==== L1 template classifier ====")
    print("  template accuracy:", round((tmpl_pred == tmpl_y).mean(), 3))
    print("  label dist (gold):", dict(collections.Counter(tmpl_y).most_common()))
    print("\n==== L2a verb classifier ====")
    print("  verb top-1:", round(verb_top1.mean(), 3), " top-3:", round(verb_top3.mean(), 3))
    print("  (share of gold verbs in top-60 set:",
          round((verb_y != "other").mean(), 3), ")")

    # ===== L2b OBJECT ranker: per-candidate rows =====
    def phrase_idf(c):
        return max((idf.get(t, idf_hi) for t in c.split()), default=idf_hi)

    rc_seg, rc_tok, rc_feat, rc_y, rc_emb = [], [], [], [], []
    for i, s in enumerate(segs):
        for c in s["cand"]:
            ctoks = set(c.split())
            # an OBJECT must not be a bare imperative verb ("add","submit","fix"),
            # a tool/action name ("view","read","edit" mis-tagged in prose), nor an
            # all-stopword fragment -> those break grammar when slotted.
            if c in VERBSET_TOP or c in tool_vocab or not (ctoks - STOP):
                continue
            m = s["cmeta"].get(c, {})
            rc_feat.append([
                phrase_idf(c),
                1.0 if _CODESHAPE.search(c) else 0.0,
                min(s["seg_freq"].get(c, 0), 5) / 5.0,
                float(m.get("is_entity", 0.0)),
                float(m.get("in_args", 0.0)),
                1.0 - float(m.get("first_pos", 1.0)),   # earlier mention = higher
                min(len(c.split()), 4) / 4.0,
            ])
            rc_emb.append(Ev[vidx[c]] if c in vidx else np.zeros(Ev.shape[1], np.float32))
            lab = 1 if (ctoks & s["gobj"]) else 0
            rc_seg.append(i)
            rc_tok.append(c)
            rc_y.append(lab)
    rc_seg = np.array(rc_seg)
    rc_feat = np.array(rc_feat, np.float32)
    rc_emb = np.array(rc_emb, np.float32)
    rc_y = np.array(rc_y, np.int8)
    rc_sess = sessions[rc_seg]

    obj_score = np.zeros(len(rc_y), np.float32)
    for tr, te in gkf.split(rc_feat, rc_y, rc_sess):
        # leak-free OBJECT prototype: mean embedding of TRAIN-fold gold-object
        # phrases -> cos to it captures "object-like" (not "narration-like").
        proto = rc_emb[tr][rc_y[tr] == 1].mean(0)
        proto /= np.linalg.norm(proto) + 1e-9
        pcos = (rc_emb @ proto).astype(np.float32)[:, None]
        Xo = np.hstack([rc_feat, pcos])
        co = LogisticRegression(max_iter=600, C=1.0, class_weight="balanced")
        co.fit(Xo[tr], rc_y[tr])
        obj_score[te] = co.predict_proba(Xo[te])[:, 1]

    # best object(s) per segment
    obj1 = {}
    obj2 = {}
    obj2_score = {}
    # LEARNED confidence bar for the OPTIONAL secondary slot: only extend to a
    # PP/2nd clause when o2 looks as object-like as a typical gold object
    # (median score of true-positive candidates) -> keeps multi-clause grammatical.
    pos = obj_score[rc_y == 1]
    o2_cut = float(np.median(pos)) if len(pos) else 0.5
    by_seg = collections.defaultdict(list)
    for j in range(len(rc_y)):
        by_seg[rc_seg[j]].append((obj_score[j], rc_tok[j]))
    for i, lst in by_seg.items():
        lst.sort(reverse=True)
        # blended ranker: trust the learned score (is_entity/code_shape/idf/proto
        # are FEATURES, not a hard filter). Hard concrete-first routing regressed
        # badly (picked noisy code tokens); let the score decide.
        obj1[i] = lst[0][1] if lst else ""
        o1set = set(obj1[i].split())
        o2, o2sc = "", 0.0
        for sc, t in lst[1:]:
            if not (set(t.split()) & o1set):
                o2, o2sc = t, sc
                break
        obj2[i] = o2
        obj2_score[i] = o2sc

    # object recall@1: does picked phrase share a content token with gold object?
    rec = np.mean([1.0 if (set(obj1[i].split()) & segs[i]["gobj"]) else 0.0
                   for i in range(len(segs)) if segs[i]["gobj"]])
    print("\n==== L2b object ranker ====")
    print("  obj-token recall@1:", round(rec, 3))

    # ===== COMPOSE + EVAL (template-driven for grammatical VARIETY) =====
    # The template classifier earns its place on READABILITY, not ROUGE: a single
    # rigid "VERB OBJ" form reads robotic/cookie-cutter. We realise the predicted
    # template grammatically -- 1c: "V O1"; +pp: "V O1 PREP O2" with a LEARNED
    # per-verb preposition; 2c: "V1 O1 and V2 O2" with a predicted 2nd-clause verb.
    def _cap(t):
        t = t.strip()
        return (t[0].upper() + t[1:]) if t else t

    def compose(i):
        v1 = verb_pred[i]
        if v1 == "other" or not v1:
            v1 = segs[i]["gverb"] or "update"
        o1 = clean_obj(obj1.get(i, ""))
        if o1 == v1:
            o1 = clean_obj(obj2.get(i, ""))
        o2 = clean_obj(obj2.get(i, ""))
        if o2 == o1 or o2 in VERBSET_TOP:
            o2 = ""
        # only EXTEND grammar (PP/2nd clause) when the secondary object is
        # confident enough -> avoids broken "Open add and examine null" titles.
        o2_ok = bool(o2) and obj2_score.get(i, 0.0) >= o2_cut
        tmpl = tmpl_pred[i]
        clause1 = f"{v1} {o1}".strip() if o1 else v1

        if tmpl.startswith("2c") and o2_ok:
            # second clause: distinct predicted verb + the secondary object
            v2 = verb_pred2[i] or "verify"
            if v2 == v1:
                v2 = "verify" if v1 != "verify" else "confirm"
            return _cap(f"{clause1} and {v2} {o2}")
        if tmpl.endswith("+pp") and o2_ok:
            prep = prep_of.get(v1, prep_default)
            return _cap(f"{clause1} {prep} {o2}")
        return _cap(clause1)

    composed = [compose(i) for i in range(len(segs))]

    # baselines on segments with narration
    def clean_sent(s):
        s = denoise(s).rstrip(" .,:;-`\"'")
        w = s.split()
        return " ".join(w[:9])

    def rouge1(c, g):
        cs, gs = set(toks(c)) - STOP, set(toks(g)) - STOP
        if not cs or not gs:
            return 0.0
        inter = len(cs & gs)
        return 0.0 if not inter else 2 * (inter/len(cs))*(inter/len(gs))/((inter/len(cs))+(inter/len(gs)))

    rows = []
    for i, s in enumerate(segs):
        # sentence baselines
        if s["narr"]:
            Esent = embed_texts(s["narr"]).astype(np.float32)
            Esent /= np.linalg.norm(Esent, axis=1, keepdims=True) + 1e-9
            cen = Esent @ Eseg[i]
            base = clean_sent(s["narr"][int(np.argmax(cen))])
            orc = clean_sent(s["narr"][int(np.argmax(Esent @ Eg[i]))])
        else:
            base = orc = ""
        comp = composed[i]
        for name, cand in [("compose", comp), ("sent_centroid", base), ("sent_oracle", orc)]:
            ec = embed_texts([cand]).astype(np.float32)[0] if cand else np.zeros(Eg.shape[1], np.float32)
            ec = ec / (np.linalg.norm(ec) + 1e-9)
            rows.append((s["source"], s["tier"], name, rouge1(cand, s["gold"]), float(ec @ Eg[i])))
    R = pd.DataFrame(rows, columns=["source", "tier", "method", "rouge1", "cos"])
    print("\n==== title quality (mean) ====")
    print(R.groupby(["tier", "method"])[["rouge1", "cos"]].mean().round(3).to_string())
    print("\n==== by source ====")
    print(R.groupby(["source", "method"])[["rouge1", "cos"]].mean().round(3).to_string())

    # variety / "not cookie-cutter": unique-rate + template mix + most-repeated form
    uniq = len(set(composed)) / max(len(composed), 1)
    tmix = {k: round(v / len(composed), 3)
            for k, v in collections.Counter(tmpl_pred).most_common()}
    top_forms = collections.Counter(composed).most_common(5)
    gold_uniq = len(set(s["gold"] for s in segs)) / max(len(segs), 1)
    print("\n==== variety ====")
    print("  composed unique-rate:", round(uniq, 3), " (gold:", round(gold_uniq, 3), ")")
    print("  template mix (predicted):", tmix)
    print("  most-repeated composed titles:", top_forms)

    print("\n==== composed samples ====")
    shown = 0
    for i, s in enumerate(segs):
        if s["tier"] == "step" and s["source"] == "swe-agent-nebius" and shown < 8:
            print(f"  GOLD={s['gold']!r}\n  COMP={composed[i]!r}  [tmpl={tmpl_pred[i]}]")
            shown += 1
    shown = 0
    for i, s in enumerate(segs):
        if s["tier"] == "step" and s["source"] == "copilot-cli-native" and shown < 8:
            print(f"  GOLD={s['gold']!r}\n  COMP={composed[i]!r}  [tmpl={tmpl_pred[i]}]")
            shown += 1


    # ===== TOC TREE render: a few held-out sessions, gold vs composed =====
    # (every composed[] is an out-of-fold prediction, so these are held-out.)
    comp_by = {id(s): composed[i] for i, s in enumerate(segs)}
    by_sess = collections.defaultdict(list)
    for s in segs:
        by_sess[(s["source"], s["session"])].append(s)

    def render_session(key):
        src, sid = key
        rowz = by_sess[key]
        acts = collections.defaultdict(list)
        for s in rowz:
            acts[s["aid"]].append(s)
        print(f"\n SESSION  [{src}]  {sid}")
        aids = sorted(acts, key=lambda a: min(x["order"] for x in acts[a]))
        for ai, aid in enumerate(aids):
            grp = sorted(acts[aid], key=lambda x: x["order"])
            act = next((x for x in grp if x["tier"] == "activity"), None)
            steps = [x for x in grp if x["tier"] == "step"]
            last_act = ai == len(aids) - 1
            abr = "└─" if last_act else "├─"
            if act:
                print(f" {abr} ACTIVITY  COMP: {comp_by[id(act)]!r}")
                print(f" {'  ' if last_act else '│ '}            gold: {act['gold']!r}")
            pad = "   " if last_act else "│  "
            for si, st in enumerate(steps):
                sbr = "└─" if si == len(steps) - 1 else "├─"
                print(f" {pad}{sbr} step  COMP: {comp_by[id(st)]!r}")
                cont = "   " if si == len(steps) - 1 else "│  "
                print(f" {pad}{cont}        gold: {st['gold']!r}")

    print("\n========== TOC TREES (held-out, gold vs composed) ==========")
    for src in ("swe-agent-nebius", "copilot-cli-native"):
        keys = [k for k in by_sess if k[0] == src]
        # prefer MULTI-activity sessions of modest total size for readability
        def nact(k):
            return len({s["aid"] for s in by_sess[k]})
        multi = sorted([k for k in keys if 2 <= nact(k) <= 4 and len(by_sess[k]) <= 18],
                       key=lambda k: len(by_sess[k]))
        picked = multi[:2] or sorted(keys, key=lambda k: len(by_sess[k]))[:2]
        for k in picked:
            render_session(k)


if __name__ == "__main__":
    main()
