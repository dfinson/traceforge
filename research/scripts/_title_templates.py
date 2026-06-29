"""Layer-1 template discovery: what is the SMALL set of syntactic templates the
gold activity/step titles actually use? POS-tag (offline nltk) the gold titles,
correct the imperative-verb mis-tag with a learned verb lexicon, collapse to
coarse slot sequences, and report the canonical templates + coverage.

Run: cd research; $env:OMP_NUM_THREADS=4; .venv\\Scripts\\python.exe -u -m scripts._title_templates
"""

from __future__ import annotations

import collections
import os

import nltk
import pandas as pd

for _p in ["averaged_perceptron_tagger", "averaged_perceptron_tagger_eng", "punkt", "punkt_tab"]:
    nltk.download(_p, quiet=True)
from nltk import pos_tag, word_tokenize  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOC = os.path.join(ROOT, "data", "processed", "activity-step-toc.parquet")

# coarse slot mapping over Penn tags
NOUNISH = {"NN", "NNS", "NNP", "NNPS", "JJ", "JJR", "JJS", "CD", "VBG", "VBN", "FW"}
VERBISH = {"VB", "VBP", "VBZ", "VBD"}
DROP = {"DT", "PRP$", "POS"}


def slot_seq(title, verbset):
    toks = word_tokenize(title)
    if not toks:
        return None
    tags = pos_tag(toks)
    out = []
    for i, (w, t) in enumerate(tags):
        lw = w.lower()
        if i == 0 and lw in verbset:  # imperative verb (tagger misses this)
            out.append("V")
            continue
        if t in DROP:
            continue
        if t == "CC" or lw in {"and", "&", "then"}:
            out.append("C")
        elif t in ("IN", "TO"):
            out.append("P")
        elif t in VERBISH or (lw in verbset and (not out or out[-1] in {"C", "P"})):
            out.append("V")
        else:  # NOUNISH and everything else -> object
            out.append("N")
    # collapse: runs of N -> OBJ; P followed by its N-run -> PP; V -> VERB; C -> CONJ
    coarse = []
    j = 0
    while j < len(out):
        s = out[j]
        if s == "N":
            coarse.append("OBJ")
            while j < len(out) and out[j] == "N":
                j += 1
            continue
        if s == "P":
            j += 1
            had = False
            while j < len(out) and out[j] == "N":
                had = True
                j += 1
            coarse.append("PP" if had else "P")
            continue
        coarse.append({"V": "VERB", "C": "CONJ"}.get(s, s))
        j += 1
    return " ".join(coarse)


def main():
    toc = pd.read_parquet(TOC)
    toc = toc[toc.session_type == "agent"]
    titles = []
    for _, a in toc.iterrows():
        titles.append(a.activity_title)
        titles.extend(st["step_title"] for st in a.steps)
    T = pd.Series([t for t in titles if isinstance(t, str) and t.strip()])
    print("titles:", len(T))

    # learned verb lexicon = frequent first-tokens (titles are imperative)
    first = T.str.split().str[0].str.lower()
    vc = collections.Counter(first)
    verbset = {w for w, c in vc.items() if c >= 5}
    print("verb lexicon size (first-token count>=5):", len(verbset))

    seqs = T.map(lambda t: slot_seq(t, verbset))
    tc = collections.Counter(seqs.dropna())
    n = len(seqs)
    print("\n==== canonical templates (coverage) ====")
    cum = 0
    for i, (k, c) in enumerate(tc.most_common(20), 1):
        cum += c
        print(f"  {c / n:6.2%}  (cum {cum / n:6.2%})  {k}")
    print(
        f"\ndistinct templates: {len(tc)}; top-10 cover {sum(c for _, c in tc.most_common(10)) / n:.1%}"
    )

    # show example titles for the top templates
    print("\n==== examples per top template ====")
    dfp = pd.DataFrame({"title": T.values, "tmpl": seqs.values})
    for k, _ in tc.most_common(8):
        ex = dfp[dfp.tmpl == k].title.head(5).tolist()
        print(f"-- {k}")
        for e in ex:
            print("    ", e)


if __name__ == "__main__":
    main()
