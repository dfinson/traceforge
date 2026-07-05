"""Zero-shot tiny-seq2seq titling: hand the model the MOST DISTILLED context
package we can build per boundary (the "golden platter") and see what it does
WITHOUT any fine-tuning. Establishes the floor + proves whether fine-tuning is
what's doing the work.

Models:
  google/t5-efficient-tiny  (~16M, span-corruption pretrain only, NOT instruction
                             tuned -> expected near-garbage zero-shot)
  google/flan-t5-small      (~80M, instruction tuned -> meaningful zero-shot ref)

Run: cd research; $env:OMP_NUM_THREADS=4; .venv\\Scripts\\python.exe -u -m scripts._title_t5
"""

from __future__ import annotations

import json
import os
import re
import sys

import pandas as pd

from scripts._title_compose import (  # noqa: E402
    CORPUS,
    SRC_DIR,
    TOC,
    narration,
    payload_entities,
    payload_text,
)
from scripts._title_object import STOP  # noqa: E402
from scripts._title_sent import extract_intent  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BOILER_CACHE = os.path.join(_ROOT, "data", "interim", "title-boilerplate-files.json")
# A "file" present in >=90% of a source's sessions carries ~zero discriminative
# signal (IDF~0): it's boilerplate leaking from tool-doc / system-prompt example
# snippets embedded in every trace (e.g. users.js, package.json appear in 54/54
# copilot sessions). LEARNED cutoff from session-frequency, not a hand-typed list.
_BOILER_SESS_FRAC = 0.90
# tool-call ids ("toolu_bdrk_01XW9..."), hex blobs, and long high-entropy ids are
# pure noise tokens that leak into narration; strip them from notes. (\w includes
# the underscores inside tool-call ids so the whole id is removed, not just a head.)
_IDJUNK_RE = re.compile(r"\btoolu_\w+|\b[a-fA-F0-9]{16,}\b|\b[A-Za-z0-9]{24,}\b")
# web-fetched bundler assets ("monaco-cvufusc8.js", "index-bcttgcnd.css"): a stem
# carrying a hash-like segment (hyphen/underscore + >=6 chars mixing letters with a
# digit, or a long vowelless run) -> drop as noise, not a real source file.
_ASSET_RE = re.compile(r"[-_][a-z0-9]*\d[a-z0-9]*\.|[-_][bcdfghjklmnpqrstvwxz]{6,}\.")

# A real filename = a >=2-char stem + a short ALPHABETIC file extension drawn
# from the concrete code/doc/config extensions seen in agent traces. This is an
# OFFLINE research-mining filter (this probe is uncommitted, never served), used
# to strip the payload-regex artifacts that masquerade as files: trailing-dot
# fragments ("time."), line-number refs ("n1.", "n223."), version strings
# ("1.0.57"), abbreviations ("e.g") and dotted JS identifiers ("date.now").
_FILE_EXT = (
    "py|pyi|md|rst|txt|js|jsx|ts|tsx|mjs|cjs|json|jsonl|yaml|yml|toml|ini|cfg|"
    "conf|sh|bash|ps1|bat|sql|html|htm|css|scss|go|rs|java|kt|c|h|cpp|hpp|cc|"
    "rb|php|cs|swift|lock|xml|csv|tsv|env|gitignore|dockerfile|makefile"
)
_FILE_RE = re.compile(rf"^[a-z][\w\-]{{1,}}\.({_FILE_EXT})$")


def tool_seq(rows):
    seen, out = set(), []
    for r in rows:
        tn = r.get("tool_name")
        if tn is None:
            continue
        t = str(tn).lower()
        if t in ("none", "nan", ""):
            continue
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def files_touched(rows):
    seen, out = set(), []
    for r in rows:
        for e in payload_entities(r):
            if _FILE_RE.match(e) and not _ASSET_RE.search(e) and e not in seen:
                seen.add(e)
                out.append(e)
    return out


def _compute_boilerplate():
    """Per-source set of files appearing in >= _BOILER_SESS_FRAC of sessions."""
    import collections

    toc = pd.read_parquet(TOC)
    toc = toc[toc.session_type == "agent"]
    out = {}
    for src, sub in toc.groupby("source"):
        d = SRC_DIR.get(src)
        if d is None:
            continue
        cnt, nsess = collections.Counter(), 0
        for sid in sub.session_id.unique():
            p = os.path.join(CORPUS, d, f"{sid}.parquet")
            if not os.path.exists(p):
                continue
            nsess += 1
            recs = list(pd.read_parquet(p).to_dict("records"))
            for f in set(files_touched(recs)):
                cnt[f] += 1
        thr = _BOILER_SESS_FRAC * max(1, nsess)
        out[src] = sorted(f for f, c in cnt.items() if c >= thr)
    os.makedirs(os.path.dirname(_BOILER_CACHE), exist_ok=True)
    with open(_BOILER_CACHE, "w") as fh:
        json.dump(out, fh, indent=1)
    return out


_BOILER = None


def boilerplate_files(src):
    global _BOILER
    if _BOILER is None:
        if os.path.exists(_BOILER_CACHE):
            with open(_BOILER_CACHE) as fh:
                _BOILER = json.load(fh)
        else:
            _BOILER = _compute_boilerplate()
    return set(_BOILER.get(src, []))


def clean_notes(narr):
    """drop tool-call ids / hex blobs that leak into narration."""
    out = []
    for s in narr:
        s = _IDJUNK_RE.sub("", s)
        s = re.sub(r"\s{2,}", " ", s).strip()
        if len(s.split()) >= 3:
            out.append(s)
    return out


# A code SYMBOL = a concrete identifier the agent acted on (function/class/const/
# dotted path), distinct from a bare filename. These carry the segment's true
# subject ("has_not_null_column", "AddIndexConcurrently") that tool NAMES and
# filenames alone omit. Extracted from the raw payload text so it is
# source-agnostic (works whether arguments are a dict, a string, or a command).
_SYM_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]+)*")
_BTICK_RE = re.compile(r"`([^`\n]{2,40})`")
# "structured" = snake_case / camelCase / dotted.path -> almost always a real
# domain entity; ranked ahead of bare ALL-CAPS keywords (NOT, NULL, WHERE) which
# are usually language tokens, not the subject.
_STRUCT_RE = re.compile(r"_|[a-z][A-Z]|\.[A-Za-z]")
_ACRONYM_RE = re.compile(r"^[A-Z][A-Z0-9]+$")


def _sym_ok(m, drop):
    if len(m) < 3 or m.lower() in STOP or m.lower() in drop:
        return False
    if _IDJUNK_RE.search(m) or _ASSET_RE.search(m):
        return False
    return bool(_STRUCT_RE.search(m) or _ACRONYM_RE.match(m))


def salient_symbols(rows, drop=frozenset(), cap=5):
    """Highest-signal code identifiers acted on in the segment, ranked by
    salience: backtick-quoted (the agent explicitly referenced) > structured
    identifiers (snake/camel/dotted) by frequency > bare ALL-CAPS keywords.
    Source-agnostic: mines the raw payload text, not a source-specific schema."""
    import collections

    dropl = {d.lower() for d in drop} | {os.path.splitext(d)[0].lower() for d in drop}
    bt, struct, acro = collections.Counter(), collections.Counter(), collections.Counter()
    for r in rows:
        txt = payload_text(r)
        if not txt:
            continue
        quoted = {q for span in _BTICK_RE.findall(txt) for q in _SYM_RE.findall(span)}
        for m in _SYM_RE.findall(txt):
            if not _sym_ok(m, dropl):
                continue
            if m in quoted:
                bt[m] += 1
            elif _STRUCT_RE.search(m):
                struct[m] += 1
            else:
                acro[m] += 1
    seen, out = set(), []
    for ctr in (bt, struct, acro):
        for w, _ in ctr.most_common():
            k = w.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(w)
            if len(out) >= cap:
                return out
    return out


def distilled_context(rows, src=None):
    """The golden platter: the few highest-signal facts about the segment."""
    parts = []
    intent = next((extract_intent(r) for r in rows if extract_intent(r)), None)
    if intent:
        parts.append(f"intent: {intent}")
    tools = tool_seq(rows)
    if tools:
        parts.append("actions: " + ", ".join(tools[:6]))
    drop = boilerplate_files(src) if src else set()
    files = [f for f in files_touched(rows) if f not in drop]
    if files:
        parts.append("files: " + ", ".join(files[:5]))
    syms = salient_symbols(rows, drop=set(files) | drop)
    if syms:
        parts.append("symbols: " + ", ".join(syms))
    # The free-text notes tail is the MAJORITY of the input (57% copilot / 67% swe
    # of words) and is the only free-text-comprehension burden -- the part a tiny
    # student parses worst (e.g. repeated swe issue-boilerplate). TITLE_CTX_NOTES
    # gates how much we keep so we can test "simplify the lesson to slot-
    # recombination": full (narration[:2][:240]) | trim (first sentence, <=100ch)
    # | none (pure structured slots). Source-agnostic, no magic numbers.
    notes_mode = os.environ.get("TITLE_CTX_NOTES", "full")
    if notes_mode != "none":
        narr = clean_notes(narration(rows))
        if narr:
            if notes_mode == "wide":
                # Feed the FULL extracted narration; the only bound is the
                # encoder's own MAX_SRC token budget (tokenizer truncation), not
                # a hand-picked sentence/char cap. Tests whether the tiny
                # student's ceiling is the starved 2-sentence context rather than
                # its capacity. No new constant: narration() already bounds the
                # sentence set; wide just stops discarding all but the first two.
                note = " ".join(narr)
            elif notes_mode == "trim":
                note = narr[0][:100]
            else:  # full (shipped default): first 2 sentences, 240-char cap
                note = " ".join(narr[:2])[:240]
            parts.append("notes: " + note)
    return " | ".join(parts) if parts else "(no signal)"


PROMPT = (
    "Write a short imperative title (3 to 6 words) summarizing this "
    "software agent step. {ctx} title:"
)


def main():
    import torch
    from transformers import AutoTokenizer, T5ForConditionalGeneration

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    model_names = sys.argv[1:] or ["google/t5-efficient-tiny", "google/flan-t5-small"]

    toc = pd.read_parquet(TOC)
    toc = toc[toc.session_type == "agent"]

    # pick a few multi-activity sessions per source (<=18 items) for readable trees
    picks = []
    for src in ("swe-agent-nebius", "copilot-cli-native"):
        sub = toc[toc.source == src]
        cand = []
        for sid, srows in sub.groupby("session_id"):
            nact = len(srows)
            nitem = sum(1 + len(a.steps) for _, a in srows.iterrows())
            if 2 <= nact <= 4 and nitem <= 18:
                cand.append((nitem, sid, srows))
        cand.sort(key=lambda x: x[0])
        picks += [(src, sid, srows) for _, sid, srows in cand[:1]]

    # build segments (context + gold) for the picked sessions only
    items = []  # (src, sid, aid, order, tier, gold, ctx)
    for src, sid, srows in picks:
        d = SRC_DIR[src]
        p = os.path.join(CORPUS, d, f"{sid}.parquet")
        if not os.path.exists(p):
            continue
        cdf = pd.read_parquet(p).sort_values("seq")
        seqmap = dict(zip(cdf.event_id, cdf.seq))
        recs = list(cdf.to_dict("records"))

        def window(s_id, e_id):
            s, e = seqmap.get(s_id, 0), seqmap.get(e_id, 0)
            return [r for r in recs if s <= r["seq"] <= e]

        for ai, (_, a) in enumerate(srows.iterrows()):
            aid = f"{sid}#{ai}"
            rowset = [(a.start_event_id, a.end_event_id, "activity", a.activity_title, 0)]
            rowset += [
                (st["start_event_id"], st["end_event_id"], "step", st["step_title"], si + 1)
                for si, st in enumerate(a.steps)
            ]
            for s_id, e_id, tier, gold, order in rowset:
                if not isinstance(gold, str) or not gold.strip():
                    continue
                rows = window(s_id, e_id)
                if not rows:
                    continue
                items.append([src, sid, aid, order, tier, gold, distilled_context(rows)])

    print(f"built {len(items)} segments across {len(picks)} sessions", file=sys.stderr)

    # show a couple of distilled contexts verbatim so we can judge the platter
    print("\n========== SAMPLE DISTILLED CONTEXT PACKAGES ==========")
    for it in items[:4]:
        print(f"\n[{it[4]}] GOLD: {it[5]!r}")
        print(f"  CTX: {it[6][:400]}")

    torch.set_num_threads(int(os.environ.get("OMP_NUM_THREADS", "4")))

    for mname in model_names:
        print(f"\n\n############### MODEL: {mname} (zero-shot) ###############")
        tok = AutoTokenizer.from_pretrained(mname)
        mdl = T5ForConditionalGeneration.from_pretrained(mname)
        mdl.eval()
        gen = {}
        with torch.no_grad():
            for it in items:
                prompt = PROMPT.format(ctx=it[6])
                enc = tok(prompt, return_tensors="pt", truncation=True, max_length=256)
                out = mdl.generate(
                    **enc,
                    max_new_tokens=12,
                    num_beams=4,
                    no_repeat_ngram_size=2,
                    early_stopping=True,
                )
                txt = tok.decode(out[0], skip_special_tokens=True).strip()
                gen[(it[1], it[2], it[3])] = txt

        # render trees
        by_sess = {}
        for it in items:
            by_sess.setdefault((it[0], it[1]), []).append(it)
        for (src, sid), rowz in by_sess.items():
            acts = {}
            for it in rowz:
                acts.setdefault(it[2], []).append(it)
            print(f"\n SESSION  [{src}]  {sid}")
            aids = sorted(acts, key=lambda a: min(x[3] for x in acts[a]))
            for ai, aid in enumerate(aids):
                grp = sorted(acts[aid], key=lambda x: x[3])
                act = next((x for x in grp if x[4] == "activity"), None)
                steps = [x for x in grp if x[4] == "step"]
                last_act = ai == len(aids) - 1
                abr = "└─" if last_act else "├─"
                if act:
                    g = gen[(act[1], act[2], act[3])]
                    print(f" {abr} ACTIVITY  GEN : {g!r}")
                    print(f" {'  ' if last_act else '│ '}            gold: {act[5]!r}")
                pad = "   " if last_act else "│  "
                for si, st in enumerate(steps):
                    sbr = "└─" if si == len(steps) - 1 else "├─"
                    g = gen[(st[1], st[2], st[3])]
                    print(f" {pad}{sbr} step  GEN : {g!r}")
                    cont = "   " if si == len(steps) - 1 else "│  "
                    print(f" {pad}{cont}        gold: {st[5]!r}")


if __name__ == "__main__":
    main()
