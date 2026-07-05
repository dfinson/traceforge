"""Deterministic, zero-cost session-title heuristics (the default floor).

Session naming turns the first substantive user message into a session title. It
deliberately does **not** use a learned model: the distilled request head was
proven weak (~9% coherent on the honest CodePlane heldout), whereas a heuristic
over the user's *own words* is coherent by construction. These heuristics are
therefore *extractive* -- their ceiling is the phrasing already present in the
message -- so an opt-in LLM API tier (:mod:`tracemill.title.naming`) exists for
abstractive titles when a key is configured.

Four methods, dispatched by :func:`heuristic_title`:

* ``clip``       -- strip conversational preamble, take the first sentence, clip
  to a word/char budget.
* ``imperative`` -- anchor on the leading action verb ("Fix login token refresh").
* ``keyphrase``  -- a vendored, zero-dependency RAKE picks the single most salient
  phrase; best for long, rambling inputs where the ask is buried past the preamble.
* ``hybrid``     -- cascade (DEFAULT): lead with a salient identifier when present,
  else the imperative title, else the plain clip.

Everything here is pure Python with no third-party dependency.
"""

from __future__ import annotations

import re

# ── Preamble the user rarely means as part of the title ──────────────────────
#: Leading conversational scaffolding, stripped iteratively from the front. Order
#: is longest-first within a pass so "i would like to" wins over "i".
_PREAMBLE = (
    "i would like to",
    "i'd like you to",
    "i'd like to",
    "i am trying to",
    "i'm trying to",
    "i need you to",
    "i want you to",
    "i need to",
    "i want to",
    "we need to",
    "we should",
    "could you please",
    "can you please",
    "would you please",
    "could you",
    "can you",
    "would you",
    "will you",
    "please help me",
    "help me",
    "let's",
    "lets",
    "let us",
    "i think we should",
    "i think",
    "i need",
    "i want",
    "hey there",
    "hey",
    "hi there",
    "hello",
    "okay",
    "ok",
    "so",
    "well",
    "just",
    "now",
    "then",
    "also",
    "actually",
    "basically",
    "quick question",
    "quick one",
    "please",
    "pls",
)

#: Common developer imperatives; the leading one anchors an "action" title.
_IMPERATIVES = frozenset(
    {
        "add",
        "fix",
        "refactor",
        "debug",
        "implement",
        "update",
        "remove",
        "delete",
        "create",
        "write",
        "build",
        "run",
        "test",
        "investigate",
        "change",
        "rename",
        "move",
        "migrate",
        "upgrade",
        "downgrade",
        "install",
        "configure",
        "setup",
        "set",
        "enable",
        "disable",
        "optimize",
        "improve",
        "handle",
        "support",
        "replace",
        "extract",
        "split",
        "merge",
        "revert",
        "review",
        "check",
        "verify",
        "generate",
        "parse",
        "render",
        "deploy",
        "document",
        "clean",
        "wire",
        "integrate",
        "expose",
        "cache",
        "log",
        "validate",
        "sanitize",
        "format",
        "lint",
        "benchmark",
        "profile",
        "mock",
        "stub",
        "patch",
        "port",
        "vendor",
        "bump",
        "pin",
        "rollback",
        "resolve",
        "diagnose",
        "reproduce",
        "trace",
        "audit",
        "refine",
        "tune",
        "tweak",
        "drop",
        "ship",
        "make",
        "get",
        "convert",
        "connect",
        "download",
        "upload",
        "fetch",
        "load",
        "save",
        "store",
        "print",
        "show",
        "list",
        "find",
        "search",
        "count",
        "compute",
        "calculate",
        "sort",
        "filter",
        "map",
        "reduce",
        "group",
        "join",
        "index",
        "hash",
        "encode",
        "decode",
        "encrypt",
        "decrypt",
        "compress",
        "unpack",
        "extend",
        "shrink",
        "scale",
        "restart",
        "stop",
        "start",
        "kill",
        "spawn",
        "throttle",
        "retry",
        "batch",
        "stream",
    }
)

#: A token carrying an identifier signal: a path/dotted-extension, snake_case,
#: kebab or internal capital, or a ``backticked`` code span.
_IDENT_RE = re.compile(
    r"`[^`]+`"  # `backticked code`
    r"|[\w./\\-]*[\w]\.[A-Za-z]{1,5}\b"  # file.ext, pkg.mod.fn
    r"|[A-Za-z]+(?:[_/\\][\w./\\-]+)+"  # snake_case / a/path / kebab-ish
    r"|[a-z]+[A-Z]\w*"  # camelCase / internalCap
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD = re.compile(r"\S+")
_LEADING_JUNK = re.compile(r"^[\s,;:.\-–—*>#`\"'()\[\]]+")
_TRAILING_JUNK = re.compile(r"[\s,;:.\-–—>]+$")

#: Weak trailing tokens that make a clipped title look cut off mid-thought.
_TAIL_STOP = frozenset(
    """the a an to for of and or in on with from into by at as so because whenever
    when while that this but if then than is are was were my your his her its their
    our we they it he she about over under up down out off""".split()
)


def _trim_tail(words: list[str]) -> list[str]:
    """Drop trailing function words so a clip doesn't end mid-thought."""
    while len(words) > 2 and re.sub(r"[^\w']", "", words[-1]).lower() in _TAIL_STOP:
        words = words[:-1]
    return words


def _first_sentence(text: str) -> str:
    for part in _SENTENCE_SPLIT.split(text.strip()):
        s = part.strip()
        if s:
            return s
    return text.strip()


def _strip_preamble(text: str) -> str:
    """Iteratively drop leading conversational scaffolding (case-insensitive)."""
    s = _LEADING_JUNK.sub("", text.strip())
    changed = True
    while changed and s:
        changed = False
        low = s.lower()
        for p in _PREAMBLE:
            # Match the phrase only at a word boundary at the very start.
            if low == p or low.startswith(p + " ") or low.startswith(p + ","):
                s = _LEADING_JUNK.sub("", s[len(p) :])
                changed = True
                break
    return s


def _cap_first(s: str) -> str:
    """Capitalize the first character without touching the rest, and leave a
    leading identifier (``auth.py``, ``getUser``, ``a/b/c``) untouched."""
    if not s:
        return s
    first = s.split(maxsplit=1)[0]
    if any(c in first for c in "._/\\") or re.search(r"\w[A-Z]", first):
        return s
    return s[:1].upper() + s[1:]


def _clip_words(text: str, max_words: int, max_chars: int) -> str:
    words = _WORD.findall(text)
    clipped = len(words) > max_words
    if clipped:
        words = _trim_tail(words[:max_words])
    out = " ".join(words)
    if len(out) > max_chars:
        # Trim to the last whole word within the char budget.
        cut = out[:max_chars]
        if " " in cut:
            cut = cut[: cut.rfind(" ")]
        out = " ".join(_trim_tail(cut.split()))
    return _TRAILING_JUNK.sub("", out)


def clip(text: str, max_words: int = 8, max_chars: int = 60) -> str:
    """Strip preamble, take the first sentence, clip to the word/char budget."""
    core = _strip_preamble(_first_sentence(text))
    core = _LEADING_JUNK.sub("", core)
    return _cap_first(_clip_words(core, max_words, max_chars))


def imperative(text: str, max_words: int = 8, max_chars: int = 60) -> str | None:
    """Anchor the title on the first leading action verb, or ``None`` if the
    (preamble-stripped) message does not start with a recognized imperative
    within its first few words."""
    core = _strip_preamble(_first_sentence(text))
    words = _WORD.findall(core)
    for i, w in enumerate(words[:4]):
        if re.sub(r"[^\w]", "", w).lower() in _IMPERATIVES:
            phrase = " ".join(words[i:])
            return _cap_first(_clip_words(phrase, max_words, max_chars))
    return None


def _salient_identifier(text: str) -> str | None:
    """The first identifier-shaped token in the message's opening, or ``None``."""
    head = " ".join(_WORD.findall(text)[:16])
    m = _IDENT_RE.search(head)
    if not m:
        return None
    tok = m.group(0).strip("`").strip()
    # Ignore trivial/absurd matches (bare word with a trailing period sentence).
    if len(tok) < 3 or " " in tok:
        return None
    return tok


def hybrid(text: str, max_words: int = 8, max_chars: int = 60) -> str:
    """Cascade: salient identifier lead -> imperative -> plain clip."""
    base = imperative(text, max_words, max_chars) or clip(text, max_words, max_chars)
    ident = _salient_identifier(text)
    if ident and ident.lower() not in base.lower():
        combined = f"{ident}: {base}"
        if len(combined) > max_chars:
            # Keep the identifier; trim the gist to fit.
            room = max_chars - len(ident) - 2
            gist = _TRAILING_JUNK.sub("", base[: max(room, 0)])
            combined = f"{ident}: {gist}".rstrip(": ").strip()
        return _cap_first(combined)
    return base


# ── Vendored zero-dependency RAKE (keyphrase extraction) ─────────────────────
_RAKE_STOP = frozenset(
    """a an the and or but if then else for to of in on at by with from into over
    under again further is are was were be been being do does did doing have has
    had having i you he she it we they me him her us them my your his its our their
    this that these those as it's i'm can could would should will shall may might
    must not no nor so than too very just about above below up down out off can't
    won't don't please help need want make get set use using used which who whom
    what when where why how all any both each few more most other some such only own
    same me my""".split()
)
_RAKE_TOKEN = re.compile(r"[A-Za-z0-9_./\\-]+")


def keyphrase(text: str, max_words: int = 8, max_chars: int = 60) -> str:
    """RAKE: split on stopwords/punctuation into candidate phrases, score words by
    degree/frequency, and return the single highest-scoring phrase (clipped)."""
    core = _strip_preamble(text)
    tokens = _RAKE_TOKEN.findall(core)
    if not tokens:
        return clip(text, max_words, max_chars)

    # Build candidate phrases: runs of non-stopword tokens.
    phrases: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if tok.lower() in _RAKE_STOP:
            if cur:
                phrases.append(cur)
                cur = []
        else:
            cur.append(tok)
    if cur:
        phrases.append(cur)
    if not phrases:
        return clip(text, max_words, max_chars)

    # Word scores: degree (co-occurrence incl. self) / frequency.
    freq: dict[str, int] = {}
    degree: dict[str, int] = {}
    for ph in phrases:
        d = len(ph) - 1
        for w in ph:
            wl = w.lower()
            freq[wl] = freq.get(wl, 0) + 1
            degree[wl] = degree.get(wl, 0) + d
    score = {w: (degree[w] + freq[w]) / freq[w] for w in freq}

    def phrase_score(ph: list[str]) -> float:
        return sum(score[w.lower()] for w in ph)

    # Prefer the highest score; break ties toward earlier, longer phrases.
    best = max(
        range(len(phrases)),
        key=lambda i: (phrase_score(phrases[i]), len(phrases[i]), -i),
    )
    out = " ".join(phrases[best])
    return _cap_first(_clip_words(out, max_words, max_chars))


_METHODS = {
    "clip": clip,
    "imperative": lambda t, mw, mc: imperative(t, mw, mc) or clip(t, mw, mc),
    "keyphrase": keyphrase,
    "hybrid": hybrid,
}


def heuristic_title(
    text: str,
    method: str = "hybrid",
    max_words: int = 8,
    max_chars: int = 60,
) -> str:
    """Dispatch to the named heuristic; empty input yields an empty title."""
    if not text or not text.strip():
        return ""
    fn = _METHODS.get(method, hybrid)
    return fn(text, max_words, max_chars)


__all__ = ["heuristic_title", "clip", "imperative", "keyphrase", "hybrid"]
