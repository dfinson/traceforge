"""Decode/render hygiene for generated titles (pure stdlib, no ML deps).

The tiny seq2seq titler is clean in-distribution but degenerates in the
zero-shot regime (unseen trace families, intent-less context): verbatim
repetition, beams that end mid-subword ("...restarte"), and child titles that
duplicate their parent. These are decode/render defects, not data defects, so
they are fixed here post-generation rather than by retraining.
"""

from __future__ import annotations

import re

_WS = re.compile(r"\s+")
_WORD = re.compile(r"[A-Za-z]+")
_STOP = {"the", "a", "an", "to", "for", "of", "and", "in", "on", "with", "from"}


def norm_key(s: str) -> frozenset[str]:
    """Plural/stopword-insensitive token-set key for parent/child/sibling de-dup."""
    toks = re.findall(r"[a-z0-9]+", s.lower())
    toks = [t[:-1] if t.endswith("s") and len(t) > 3 else t for t in toks]
    return frozenset(t for t in toks if t not in _STOP)


def collapse_repeats(s: str) -> str:
    """Remove adjacent duplicate tokens, mirrored 'X and X', and whole doubling."""
    w = s.split()
    out: list[str] = []
    for t in w:
        if out and out[-1].lower() == t.lower():
            continue
        out.append(t)
    w = out
    low = [x.lower() for x in w]
    for conn in ("and", "then", "to", "&"):
        if conn in low:
            i = low.index(conn)
            if low[:i] and low[:i] == low[i + 1 :]:
                w = w[:i]
                low = [x.lower() for x in w]
    n = len(w)
    if n >= 2 and n % 2 == 0 and low[: n // 2] == low[n // 2 :]:
        w = w[: n // 2]
    return " ".join(w)


def _degenerate(s: str) -> bool:
    """Beam looks truncated/garbled: a long vowelless alphabetic run."""
    for t in _WORD.findall(s):
        low = t.lower()
        if len(low) >= 5 and not re.search(r"[aeiouy]", low):
            return True
    return False


def clean_title(s: str) -> str:
    s = _WS.sub(" ", str(s)).strip().strip("-:,. ")
    s = collapse_repeats(s)
    if s:
        s = s[0].upper() + s[1:]
    return s


def best_of(cands: list[str]) -> str:
    """Pick the first non-degenerate beam alternate, cleaned."""
    cleaned = [clean_title(c) for c in cands if c and c.strip()]
    for c in cleaned:
        if not _degenerate(c):
            return c
    return cleaned[0] if cleaned else ""


def pick_distinct(used: set[frozenset[str]], cands: list[str]) -> str:
    """First non-degenerate alternate whose token-set is new this session.

    Used to keep a child/step title distinct from its parent activity title and
    its siblings; falls back to ``best_of`` if all alternates collide.
    """
    cleaned = [clean_title(c) for c in cands if c and c.strip()]
    for c in cleaned:
        if _degenerate(c):
            continue
        k = norm_key(c)
        if k and k not in used:
            used.add(k)
            return c
    chosen = best_of(cands)
    used.add(norm_key(chosen))
    return chosen
