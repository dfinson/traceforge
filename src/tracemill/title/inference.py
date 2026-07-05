"""Torch-free, CPU-only title generation for activity/step spans.

Serves the tiny T5 titler via :mod:`onnxruntime` + :mod:`tokenizers` + numpy
only -- no torch / transformers at inference (RSS ~250MB vs ~1GB with torch).
Single-pass beam search: the model emits ``num_beams`` candidates per span and
:func:`tracemill.title.hygiene.best_of` picks the first non-degenerate one.

The candidate-fusion 2-pass variant was evaluated and dropped: it overfit the
torch beam-search candidate distribution and degraded on the ORT generator, so
the robust shipped path is single-pass (see research notes / experiments).

Inputs are *distilled context* strings (intent / actions / files / symbols /
notes slots) produced upstream by the span feature builder. Generation params
mirror the training/eval config (no_repeat_ngram_size=2, repetition_penalty=1.3,
length_penalty=0.8) so served titles match offline evaluation.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import numpy as np

from .hygiene import best_of

#: T5 special ids: pad doubles as the decoder start token; eos == </s>.
_PAD, _EOS = 0, 1
#: Task prefix the titler was fine-tuned with (learned; must match training).
_PREFIX = "summarize agent step: "
#: Encoder truncation; the saved tokenizer.json otherwise bakes in max_length=20.
_MAX_SRC = 512


def _logsoftmax(x: np.ndarray) -> np.ndarray:
    x = x - x.max(-1, keepdims=True)
    return x - np.log(np.exp(x).sum(-1, keepdims=True))


#: Surface-level grounding. A tiny seq2seq model under noisy contexts invents
#: identifier-shaped tokens that the span never mentions (e.g. ``_init_admissment``,
#: ``github-mcp-server-ample``). These are, by definition, ungrounded: a faithful
#: title can only name identifiers that appear in its own distilled context. The
#: rule is structural (no source tags, no thresholds, no tuned magic): a content
#: word carrying an identifier signal -- snake/path separator, hyphenation, an
#: internal capital, a digit, or a dotted extension -- must occur verbatim in the
#: context or the candidate that emitted it is demoted below grounded siblings.
#: Gated by ``TITLE_GROUND`` (default on) purely so the effect can be A/B measured.
_GROUND_DEFAULT = os.environ.get("TITLE_GROUND", "1") != "0"
#: Beam width / candidate pool. Wider gives the grounding gate more grounded
#: specifics to promote over a collapsed hallucination, at linear decode cost;
#: env-overridable purely so the footprint/quality trade can be measured.
_BEAMS_DEFAULT = int(os.environ.get("TITLE_BEAMS", "5"))
#: Adaptive escalation ceiling. When *every* base-width beam invents an
#: out-of-context identifier (a decode collapse), and only then, the pool is
#: re-decoded at this width so grounding has a faithful candidate to promote.
#: The grounded majority never pays it. Equal to base width disables escalation.
_BEAMS_MAX = int(os.environ.get("TITLE_BEAMS_MAX", str(2 * _BEAMS_DEFAULT)))
_WORD_RE = re.compile(r"[A-Za-z0-9_./\\-]+")
_ID_RE = re.compile(r"[_/\\-]|[a-z][A-Z]|\d|\.[A-Za-z]")
_GROUND_STOP = frozenset(
    {
        "the",
        "a",
        "an",
        "to",
        "of",
        "in",
        "on",
        "for",
        "and",
        "or",
        "with",
        "from",
        "into",
        "by",
        "at",
        "as",
        "its",
        "their",
        "this",
        "that",
        "these",
        "those",
    }
)


def _identifier_words(title: str) -> list[str]:
    """Identifier-shaped content words of a title (skipping the leading verb)."""
    ws = _WORD_RE.findall(title)
    return [w for w in ws[1:] if w.lower() not in _GROUND_STOP and _ID_RE.search(w)]


def _is_grounded(title: str, ctx_lower: str) -> bool:
    """True iff every identifier-shaped content word appears in the context."""
    return all(w.lower() in ctx_lower for w in _identifier_words(title))


def _ground_order(cands: list[str], context: str) -> list[str]:
    """Stable-reorder candidates so context-grounded ones come first.

    Ungrounded candidates are appended rather than dropped, so selection never
    returns empty when *every* beam hallucinates -- the best-scoring (still
    ungrounded) title is then surfaced, and downstream hygiene cleans it.
    """
    ctx_lower = context.lower()
    grounded: list[str] = []
    ungrounded: list[str] = []
    for c in cands:
        (grounded if _is_grounded(c, ctx_lower) else ungrounded).append(c)
    return grounded + ungrounded


class TitleModel:
    """Loaded ORT titler. Construct via :meth:`load`; call :meth:`title`."""

    def __init__(self, enc, dec, tok, prefix: str = _PREFIX) -> None:
        self._enc = enc
        self._dec = dec
        self._tok = tok
        self._prefix = prefix

    # ----------------------------------------------------------------- loading
    @classmethod
    def load(
        cls, model_dir: str | os.PathLike[str] | None = None, threads: int = 1
    ) -> "TitleModel":
        """Load the packaged int8 titler (or a custom ``model_dir``).

        ``threads`` caps onnxruntime intra-op threads to keep the live CPU
        footprint near-zero (fan-spin / RAM starvation is a hard failure). The
        default of ``1`` matches what the live :class:`TitleInferencer` serves, so
        direct callers and footprint benchmarks measure the shipped config.
        """
        import onnxruntime as ort
        from tokenizers import Tokenizer

        from ._resolve import INSTALL_HINT, span_dir

        if model_dir is not None:
            d = Path(model_dir)
        else:
            resolved = span_dir()
            if resolved is None:
                raise FileNotFoundError(INSTALL_HINT)
            d = resolved
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        providers = ["CPUExecutionProvider"]
        enc = ort.InferenceSession(str(d / "encoder.onnx"), so, providers=providers)
        dec = ort.InferenceSession(str(d / "decoder.onnx"), so, providers=providers)
        tok = Tokenizer.from_file(str(d / "tokenizer.json"))
        tok.enable_truncation(max_length=_MAX_SRC)
        return cls(enc, dec, tok)

    # -------------------------------------------------------------- generation
    def _encode(self, text: str):
        e = self._tok.encode(self._prefix + text)
        ids = np.asarray([e.ids], dtype=np.int64)
        mask = np.asarray([e.attention_mask], dtype=np.int64)
        h = self._enc.run(None, {"input_ids": ids, "attention_mask": mask})[0]
        return h, mask

    def _beams(
        self,
        text: str,
        num_beams: int,
        num_return: int,
        max_new: int,
        no_repeat: int,
        rep_pen: float,
        len_pen: float,
    ) -> list[str]:
        h, mask = self._encode(text)
        B = num_beams
        hB = np.repeat(h, B, axis=0)
        mB = np.repeat(mask, B, axis=0)
        beams: list[list[int]] = [[_PAD]]
        scores = np.zeros(1, dtype=np.float64)
        done: list[tuple[float, list[int]]] = []
        for _ in range(max_new):
            n = len(beams)
            ids = np.asarray(beams, dtype=np.int64)
            raw = self._dec.run(
                None,
                {
                    "input_ids": ids,
                    "encoder_hidden_states": hB[:n],
                    "encoder_attention_mask": mB[:n],
                },
            )[0][:, -1, :].astype(np.float64)
            for i, toks in enumerate(beams):
                for t in set(toks):  # HF repetition penalty on raw logits
                    raw[i, t] = raw[i, t] / rep_pen if raw[i, t] > 0 else raw[i, t] * rep_pen
                if no_repeat and len(toks) >= no_repeat:  # ban repeated n-grams
                    pref = tuple(toks[-(no_repeat - 1) :]) if no_repeat > 1 else ()
                    for k in range(len(toks) - no_repeat + 1):
                        if tuple(toks[k : k + no_repeat - 1]) == pref:
                            raw[i, toks[k + no_repeat - 1]] = -1e9
            lp = _logsoftmax(raw)
            cand = (scores[:n, None] + lp).reshape(-1)
            V = lp.shape[1]
            order = np.argpartition(cand, -2 * B)[-2 * B :]
            order = order[np.argsort(cand[order])[::-1]]
            new_beams: list[list[int]] = []
            new_scores: list[float] = []
            for idx in order:
                bi, tok = idx // V, int(idx % V)
                toks = beams[bi] + [tok]
                if tok == _EOS:
                    done.append((cand[idx] / (len(toks) ** len_pen), toks))
                else:
                    new_beams.append(toks)
                    new_scores.append(cand[idx])
                if len(new_beams) >= B:
                    break
            if not new_beams:
                break
            beams, scores = new_beams, np.asarray(new_scores)
            if len(done) >= B:  # early_stopping=True: B finished hypotheses
                break
        # Prefer FINISHED hypotheses; only fall back to unfinished active beams
        # if too few finished (avoids truncated titles).
        done.sort(key=lambda x: x[0], reverse=True)
        if len(done) < num_return:
            done = done + sorted(
                ((s / (len(t) ** len_pen), t) for s, t in zip(scores, beams)),
                key=lambda x: x[0],
                reverse=True,
            )
        outs: list[str] = []
        seen: set[str] = set()
        for _score, toks in done:
            t = self._tok.decode([x for x in toks if x not in (_PAD, _EOS)]).strip()
            if t not in seen:
                seen.add(t)
                outs.append(t)
            if len(outs) >= num_return:
                break
        return outs

    def _decode_grounded(
        self,
        context: str,
        num_beams: int,
        max_new: int,
        no_repeat: int,
        rep_pen: float,
        len_pen: float,
        ground: bool,
    ) -> list[str]:
        """Decode candidates, grounding-ordered, with adaptive escalation.

        Base-width decode first. Only if grounding is on and *no* base candidate
        is grounded (every beam invented an out-of-context identifier) is the
        pool re-decoded at :data:`_BEAMS_MAX` -- so the faithful majority keeps
        the base footprint and only the collapsing minority pays for more beams.
        """
        cands = self._beams(context, num_beams, num_beams, max_new, no_repeat, rep_pen, len_pen)
        if not ground:
            return cands
        ctx_low = context.lower()
        if not any(_is_grounded(c, ctx_low) for c in cands) and _BEAMS_MAX > num_beams:
            cands = self._beams(
                context, _BEAMS_MAX, _BEAMS_MAX, max_new, no_repeat, rep_pen, len_pen
            )
        return _ground_order(cands, context)

    def title(
        self,
        context: str,
        num_beams: int = _BEAMS_DEFAULT,
        max_new: int = 32,
        no_repeat: int = 2,
        rep_pen: float = 1.3,
        len_pen: float = 0.8,
        ground: bool | None = None,
    ) -> str:
        """Return a short imperative title for a distilled span ``context``.

        Generates ``num_beams`` candidates and returns the first non-degenerate
        one after decode hygiene. When ``ground`` (default :data:`_GROUND_DEFAULT`)
        is on, candidates that name identifiers absent from ``context`` are
        demoted below grounded ones first (with adaptive beam escalation on a
        full collapse).
        """
        if not context or not context.strip():
            return ""
        g = _GROUND_DEFAULT if ground is None else ground
        cands = self._decode_grounded(context, num_beams, max_new, no_repeat, rep_pen, len_pen, g)
        return best_of(cands)

    def candidates(
        self,
        context: str,
        num_beams: int = _BEAMS_DEFAULT,
        max_new: int = 32,
        no_repeat: int = 2,
        rep_pen: float = 1.3,
        len_pen: float = 0.8,
        ground: bool | None = None,
    ) -> list[str]:
        """Return the cleaned beam candidates (for sibling de-dup via hygiene).

        Grounded candidates are ordered first (see :meth:`title`) so both
        :func:`best_of` and :func:`pick_distinct` prefer faithful titles.
        """
        if not context or not context.strip():
            return []
        g = _GROUND_DEFAULT if ground is None else ground
        return self._decode_grounded(context, num_beams, max_new, no_repeat, rep_pen, len_pen, g)
