"""Distil a span's events into the compact context string the titler consumes.

This is the **serve-side** half of the title train/serve contract. The titler
was fine-tuned on ``distilled_context(window)`` strings built from the labelling
corpus, where each corpus row is a :class:`~tracemill.types.SessionEvent`
projected through :func:`tracemill.phase.event_rows.event_to_feature_row`. Live
inference projects the events of a freshly-closed activity/step span through the
**same** projection and the **same** distiller here, so there is no train/serve
skew by construction.

The distilled package is a handful of highest-signal, source-agnostic slots --
``intent`` (a stated ``report_intent`` gerund, gold-quality title text when
present), ``actions`` (tool sequence), ``files`` (touched source files, minus
learned boilerplate), ``symbols`` (salient code identifiers acted upon) and a
short free-text ``notes`` tail. Everything is mined from the raw payload /
classification columns, never from a source-specific schema, so the same code
runs across agent frameworks.

The boilerplate file set is **learned from the full training corpus** (files
present in the overwhelming majority of a corpus's sessions carry ~zero IDF and
leak from tool-doc / system-prompt example snippets) and frozen into a packaged
artifact, so it is applied source-agnostically at inference with no corpus
dependency. See :data:`_BOILER_FILE`.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

#: tool-call ids ("toolu_bdrk_01XW9..."), hex blobs and long high-entropy ids are
#: pure noise tokens that leak into narration; stripped from notes/symbols.
_IDJUNK_RE = re.compile(r"\btoolu_\w+|\b[a-fA-F0-9]{16,}\b|\b[A-Za-z0-9]{24,}\b")
#: web-fetched bundler assets ("monaco-cvufusc8.js", "index-bcttgcnd.css"): a
#: stem carrying a hash-like segment -> drop as noise, not a real source file.
_ASSET_RE = re.compile(r"[-_][a-z0-9]*\d[a-z0-9]*\.|[-_][bcdfghjklmnpqrstvwxz]{6,}\.")

#: A real filename = a >=2-char stem + a short alphabetic extension drawn from
#: the concrete code/doc/config extensions seen in agent traces.
_FILE_EXT = (
    "py|pyi|md|rst|txt|js|jsx|ts|tsx|mjs|cjs|json|jsonl|yaml|yml|toml|ini|cfg|"
    "conf|sh|bash|ps1|bat|sql|html|htm|css|scss|go|rs|java|kt|c|h|cpp|hpp|cc|"
    "rb|php|cs|swift|lock|xml|csv|tsv|env|gitignore|dockerfile|makefile")
_FILE_RE = re.compile(rf"^[a-z][\w\-]{{1,}}\.({_FILE_EXT})$")

#: Absolute/relative path -> trailing basename, so notes read cleanly.
_ABS_PATH = re.compile(r"(?:[A-Za-z]:\\|/)[\w\\/.\- ]*?([\w.\-]+\.\w{1,5})")

#: A code SYMBOL: a concrete identifier the agent acted on (function/class/const/
#: dotted path), distinct from a bare filename -- carries the segment's subject.
_SYM_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]+)*")
_BTICK_RE = re.compile(r"`([^`\n]{2,40})`")
#: "structured" = snake_case / camelCase / dotted.path -> almost always a real
#: domain entity; ranked ahead of bare ALL-CAPS keywords (NOT, NULL, WHERE).
_STRUCT_RE = re.compile(r"_|[a-z][A-Z]|\.[A-Za-z]")
_ACRONYM_RE = re.compile(r"^[A-Z][A-Z0-9]+$")

#: code-shaped entity test for payload-mined objects (dot-ext, snake/camel,
#: ALLCAPS>=2, digit, slash, class/function keyword).
_CODESHAPE = re.compile(
    r"(\.\w{1,5}\b)|(_)|([a-z][A-Z])|(\b[A-Z]{2,}\b)|(\d)|(/)|(\bclass\b|\bfunction\b)")
_IDENT = re.compile(r"[A-Za-z_][\w./-]{2,}")

#: Stopwords for object-side filtering (shared with the title composer).
STOP = set(
    "the a an of to and in for with on is are be this that it we our us you your i "
    "let lets now first then next also will should can use via into from as at".split())

#: Packaged set of boilerplate files learned from the full training corpus.
_BOILER_FILE = Path(__file__).resolve().parent / "data" / "boilerplate_files.json"


def _load_boilerplate() -> frozenset[str]:
    try:
        with open(_BOILER_FILE, encoding="utf-8") as fh:
            return frozenset(str(x).lower() for x in json.load(fh))
    except (OSError, ValueError):
        return frozenset()


_BOILER = _load_boilerplate()


# ----------------------------------------------------------------- payload text
def _payload_obj(row: dict):
    pj = row.get("payload_json")
    if not isinstance(pj, str):
        return None
    try:
        return json.loads(pj)
    except ValueError:
        return pj


def _iter_strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _iter_strings(v)
    elif isinstance(o, (list, tuple)):
        for v in o:
            yield from _iter_strings(v)


def payload_text(row: dict) -> str:
    """All string leaves of the serialized payload, flattened."""
    o = _payload_obj(row)
    if o is None:
        return ""
    if isinstance(o, str):
        return o
    return " ".join(_iter_strings(o))


def denoise(t: str) -> str:
    if not t:
        return ""
    t = _ABS_PATH.sub(r"\1", t)
    t = re.split(r"\b(?:function|end_of_edit)\b", t, flags=re.I)[0]
    t = re.sub(r"\breport_intent\b", "", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip()


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?\n])\s+", (text or "").strip()) if s.strip()]


# ---------------------------------------------------------------------- intent
def extract_intent(row: dict) -> str | None:
    """The gerund-form ``arguments.intent`` of a ``report_intent`` tool call --
    gold-quality title text when present, else ``None``."""
    if "report_intent" not in str(row.get("tool_name", "")).lower():
        pj = row.get("payload_json")
        if not isinstance(pj, str) or "report_intent" not in pj:
            return None
    o = _payload_obj(row)
    if isinstance(o, dict) and str(o.get("tool_name", "")).lower() == "report_intent":
        intent = (o.get("arguments") or {}).get("intent")
        if isinstance(intent, str) and len(intent.split()) >= 2:
            return intent.strip()
    return None


# ----------------------------------------------------------------------- slots
def tool_seq(rows: list[dict]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
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


def _payload_entities(row: dict) -> set[str]:
    """Concrete entities acted UPON: file/binary names, structures, code
    identifiers (not the tool/action verb side)."""
    ents: set[str] = set()
    for col in ("binaries", "structure"):
        v = row.get(col)
        if isinstance(v, (list, tuple)):
            ents |= {str(x).lower() for x in v if x is not None}
        elif v is not None and str(v) != "None":
            ents.add(str(v).lower())
    pj = row.get("payload_json")
    if isinstance(pj, str):
        for m in re.findall(r"[\w.\-]+\.\w{1,5}", pj):
            ents.add(os.path.basename(m).lower())
        for m in _IDENT.findall(pj):
            if _CODESHAPE.search(m):
                ents.add(m.lower())
    return {e for e in ents if len(e) > 1}


def files_touched(rows: list[dict]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for r in rows:
        for e in _payload_entities(r):
            if _FILE_RE.match(e) and not _ASSET_RE.search(e) and e not in seen:
                seen.add(e)
                out.append(e)
    return out


def clean_notes(narr: list[str]) -> list[str]:
    """Drop tool-call ids / hex blobs that leak into narration."""
    out: list[str] = []
    for s in narr:
        s = _IDJUNK_RE.sub("", s)
        s = re.sub(r"\s{2,}", " ", s).strip()
        if len(s.split()) >= 3:
            out.append(s)
    return out


def narration(rows: list[dict]) -> list[str]:
    out: list[str] = []
    for r in rows:
        k = str(r.get("kind"))
        if k.startswith("message.assistant") or k.startswith("message.user"):
            for s in sentences(denoise(payload_text(r))):
                if 3 <= len(s.split()) <= 40:
                    out.append(s)
    return out[:25]


def _sym_ok(m: str, drop: set[str]) -> bool:
    if len(m) < 3 or m.lower() in STOP or m.lower() in drop:
        return False
    if _IDJUNK_RE.search(m) or _ASSET_RE.search(m):
        return False
    return bool(_STRUCT_RE.search(m) or _ACRONYM_RE.match(m))


def salient_symbols(rows: list[dict], drop: frozenset[str] = frozenset(), cap: int = 5) -> list[str]:
    """Highest-signal code identifiers acted on in the segment, ranked by
    salience: backtick-quoted > structured (snake/camel/dotted) by frequency >
    bare ALL-CAPS keywords. Source-agnostic (mines raw payload text)."""
    import collections

    dropl = {d.lower() for d in drop} | {os.path.splitext(d)[0].lower() for d in drop}
    bt: collections.Counter = collections.Counter()
    struct: collections.Counter = collections.Counter()
    acro: collections.Counter = collections.Counter()
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
    seen: set[str] = set()
    out: list[str] = []
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


# ------------------------------------------------------------------- assemble
def distilled_context(rows: list[dict]) -> str:
    """The golden platter: the few highest-signal facts about a span.

    ``rows`` are :func:`event_to_feature_row` projections of the span's events,
    in sequence order. Returns the slot string the titler was trained on, or
    ``"(no signal)"`` when nothing useful is extractable.
    """
    parts: list[str] = []
    intent = next((extract_intent(r) for r in rows if extract_intent(r)), None)
    if intent:
        parts.append(f"intent: {intent}")
    tools = tool_seq(rows)
    if tools:
        parts.append("actions: " + ", ".join(tools[:6]))
    files = [f for f in files_touched(rows) if f not in _BOILER]
    if files:
        parts.append("files: " + ", ".join(files[:5]))
    syms = salient_symbols(rows, drop=frozenset(files) | _BOILER)
    if syms:
        parts.append("symbols: " + ", ".join(syms))
    narr = clean_notes(narration(rows))
    if narr:
        parts.append("notes: " + " ".join(narr[:2])[:240])
    return " | ".join(parts) if parts else "(no signal)"


__all__ = ["STOP", "distilled_context"]
