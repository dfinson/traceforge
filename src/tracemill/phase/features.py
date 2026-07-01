"""Portable per-event feature extraction for the phase classifier.

Implements the feature design in ``research/docs/03-feature-design.md``. Every
feature is portable across agent frameworks by construction. Three sources only:

1. Canonical classification fields from tracemill's enricher (symbolic dicts).
2. A static, framework-agnostic text embedding (model2vec) of the event payload.
3. Position / timing features derived from (1).

The functions here operate on the **feature-row dict** produced by
:func:`tracemill.phase.event_rows.event_to_feature_row` — the same projection
the labelling corpus was built from — so training and inference featurise
identically. The model2vec embedding is frozen (no fitting); the symbolic
features are emitted as ``dict[str, float]`` for a :class:`DictVectorizer`.

``model2vec`` (text embedding) and ``scikit-learn`` (vectorisation) are core
dependencies; they are imported lazily so importing this module stays cheap.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable, Sequence

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (logged as MLflow params by callers; model choice, not a threshold)
# ---------------------------------------------------------------------------

#: Static distilled embedder. 256-d, CPU-only, framework-agnostic. See
#: research/docs/03-feature-design.md Block 5.
MODEL2VEC_NAME = "minishlab/potion-base-8M"
MODEL2VEC_DIM = 256

#: Payload text is truncated before embedding. model2vec mean-pools tokens, so
#: this only bounds tokenisation cost; it is not a semantic cutoff.
MAX_TEXT_CHARS = 2000

#: Gated phase vocabulary. ``review`` is intentionally excluded and folded into
#: ``verification`` (surfaced separately as the ``is_review`` modifier).
PHASES: tuple[str, ...] = (
    "planning",
    "implementation",
    "verification",
    "exploration",
)

#: The raw label demoted to a modifier and remapped onto a gated phase.
REVIEW_MODIFIER = "review"
REVIEW_REMAPS_TO = "verification"

# Canonical event fields fed to the symbolic featuriser.
_SCALAR_FIELDS = ("kind", "tool_name", "mechanism", "effect", "shell_dialect")
_LIST_FIELDS = (
    "scope",
    "role",
    "action",
    "capability",
    "structure",
    "phase_signals",
)

# ---------------------------------------------------------------------------
# Feature-set composition (single source of truth, shared by CV + inference)
# ---------------------------------------------------------------------------

#: Feature-set identifiers understood by the matrix builders.
FEATURE_SETS = (
    "symbolic",
    "embedding",
    "combined",
    "combined-seg",
    "combined-seg-nbrcos",
    "combined-seg-nbrcentroid",
    "combined-seg-nbr",
)

#: Which neighbor (Block 5b) feature-key prefixes each feature-set includes.
_NBR_PREFIXES: dict[str, tuple[str, ...]] = {
    "combined-seg-nbrcos": ("nbr_cos_",),
    "combined-seg-nbrcentroid": ("nbr_centroid_",),
    "combined-seg-nbr": ("nbr_cos_", "nbr_centroid_"),
}
_SYMBOLIC_SETS = frozenset(FEATURE_SETS) - {"embedding"}
_EMBEDDING_SETS = frozenset(FEATURE_SETS) - {"symbolic"}
_SEG_SETS = frozenset(s for s in FEATURE_SETS if s.startswith("combined-seg"))


def feature_set_blocks(feature_set: str) -> tuple[bool, bool, bool, tuple[str, ...]]:
    """Resolve a feature-set id into ``(use_symbolic, use_embedding, use_seg,
    nbr_prefixes)`` — the single source of truth shared by cross-validation and
    the fit-on-all persistence/inference path."""

    return (
        feature_set in _SYMBOLIC_SETS,
        feature_set in _EMBEDDING_SETS,
        feature_set in _SEG_SETS,
        _NBR_PREFIXES.get(feature_set, ()),
    )


@dataclass(frozen=True)
class NeighborParams:
    """Windows for the neighbor model2vec similarity features (from YAML)."""

    windows: tuple[int, ...]


@dataclass(frozen=True)
class EventExample:
    """One event, featurised and ready for vectorisation."""

    session_id: str
    source: str
    event_id: str
    symbolic: dict[str, float]
    numeric: dict[str, float]
    text: str
    phase_signals: tuple[str, ...]
    phases: tuple[str, ...] = ()  # phase task target (gated 4-class); empty at inference
    is_review: bool = False
    seg: dict[str, float] = field(default_factory=dict)  # Block 6 segmentation
    nbr: dict[str, float] = field(default_factory=dict)  # Block 5b neighbor sim
    content_bearing: bool = True  # phase-decision target? plumbing inherits instead


# ---------------------------------------------------------------------------
# Per-event featurisers (operate on the event-row dict)
# ---------------------------------------------------------------------------


def _extract_text(event: dict) -> str:
    """Concatenate the human-meaningful text on an event.

    Uses ``motivation`` (the agent's stated intent) plus any string values in
    ``payload_json`` (tool output, command, error text). Framework-agnostic.
    """

    parts: list[str] = []
    motivation = event.get("motivation")
    if motivation:
        parts.append(str(motivation))
    payload = event.get("payload_json")
    if payload:
        try:
            obj = json.loads(payload)
            parts.extend(_iter_strings(obj))
        except (json.JSONDecodeError, TypeError):
            parts.append(str(payload))
    text = " ".join(p for p in parts if p).strip()
    return text[:MAX_TEXT_CHARS]


def _iter_strings(obj: object) -> Iterable[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _iter_strings(v)


#: Canonical (framework-normalised) event-kind prefixes that carry a workflow
#: phase. Everything else — session/turn/hook lifecycle markers, agent spawn
#: bookkeeping, permission/telemetry plumbing — has no intrinsic phase and is
#: made to *inherit* the prevailing phase rather than be classified. Adapters
#: normalise raw framework types onto these canonical kinds, so the predicate is
#: framework-agnostic by construction.
_CONTENT_KIND_PREFIXES = (
    "message.",
    "tool.call.",
    "tool.result.",
    "tool.output",
    "reasoning.",
    "planning.",
)


def is_content_bearing(event: dict) -> bool:
    """Whether an event is a phase-*decision* target.

    Plumbing events (lifecycle/turn/hook/permission markers) carry no workflow
    phase: in the labelling corpus they were uniformly stamped ``planning`` and
    they expose no separable features (empty text, framework-specific kind), so
    classifying them independently only echoes the majority prior. They instead
    inherit the prevailing content-bearing phase. An event is content-bearing if
    its canonical kind is a message/tool/reasoning kind, or — as a fallback for
    unmapped/raw events — it carries a tool action or a canonical classification.
    """

    kind = str(event.get("kind") or "")
    if kind.startswith(_CONTENT_KIND_PREFIXES):
        return True
    return bool(event.get("tool_name") or event.get("action") or event.get("mechanism"))


def _symbolic_dict(event: dict) -> dict[str, float]:
    """Canonical classification one-hots / multi-hots (Blocks 1-3)."""

    feats: dict[str, float] = {}
    for f in _SCALAR_FIELDS:
        v = event.get(f)
        if v:
            feats[f"{f}={v}"] = 1.0
    for f in _LIST_FIELDS:
        for v in event.get(f) or ():
            feats[f"{f}={v}"] = 1.0
    activity = event.get("activity")
    if activity:
        root = str(activity).split(".", 1)[0]
        feats[f"activity_root={root}"] = 1.0
    return feats


def _numeric_dict(event: dict, position: float | None = None) -> dict[str, float]:
    """Timing features (Block 4), all derived from canonical fields.

    ``position`` (``seq / n``, normalised by *total* session length) is
    intentionally omitted from the phase path: it is the only non-causal
    feature — ``n`` is unknowable mid-stream, so the streaming serve path
    could never reproduce it without skew. It is still emitted for the
    separate boundary system, which passes an explicit ``position``.
    """

    feats: dict[str, float] = {}
    if position is not None:
        feats["position_in_session"] = position
    dur = event.get("duration_ms")
    if dur:
        feats["log_duration_ms"] = float(np.log1p(max(0, int(dur))))
    return feats


def _l2norm(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def _neighbor_features(
    ordered_ids: list[str], ordered_texts: list[str], windows: tuple[int, ...]
) -> dict[str, dict[str, float]]:
    """Windowed neighbor model2vec similarity features (Block 5b, docs/03 §5).

    For each event and window ``w`` we emit two scalars:

    * ``nbr_cos_w{w}``      — cosine between the mean-pooled past-window centroid
      and the mean-pooled future-window centroid (semantic shift). 1.0 at edges.
      **Non-causal** (uses the future window).
    * ``nbr_centroid_w{w}`` — 1 - cosine between the event embedding and its
      trailing-window centroid (drift from recent context). **Causal**.

    The production contract (``combined-seg-nbrcentroid``) uses only the causal
    ``nbr_centroid_*`` family.
    """

    vecs = embed_texts(ordered_texts)
    n = len(vecs)
    out: dict[str, dict[str, float]] = {}
    for i, eid in enumerate(ordered_ids):
        ev_unit = _l2norm(vecs[i])
        d: dict[str, float] = {}
        for w in windows:
            past = vecs[max(0, i - w) : i]
            fut = vecs[i : min(n, i + w)]
            if len(past) and len(fut):
                pc = _l2norm(past.mean(axis=0))
                fc = _l2norm(fut.mean(axis=0))
                d[f"nbr_cos_w{w}"] = float(np.dot(pc, fc))
            else:
                d[f"nbr_cos_w{w}"] = 1.0
            trail = vecs[max(0, i - w + 1) : i + 1]
            tc = _l2norm(trail.mean(axis=0))
            d[f"nbr_centroid_w{w}"] = float(1.0 - np.dot(ev_unit, tc))
        out[eid] = d
    return out


class IncrementalNeighbor:
    """Online, exactly-equivalent form of the causal ``nbr_centroid_*`` family.

    Maintains the last ``max(windows)`` event embeddings and, per :meth:`push`,
    returns the same ``nbr_centroid_w{w}`` (1 - cosine to the trailing-window
    centroid) that :func:`_neighbor_features` computes for that event. Only the
    causal centroid family is produced — the non-causal ``nbr_cos_*`` (which
    needs the future window) is not part of the production contract.
    """

    def __init__(self, windows: tuple[int, ...]) -> None:
        self._windows = windows
        self._buf: deque[np.ndarray] = deque(maxlen=max(windows, default=1))

    def push(self, text: str) -> dict[str, float]:
        v = embed_texts([text])[0]
        self._buf.append(v)
        ev_unit = _l2norm(v)
        recent = list(self._buf)
        d: dict[str, float] = {}
        for w in self._windows:
            trail = np.asarray(recent[-w:])
            tc = _l2norm(trail.mean(axis=0))
            d[f"nbr_centroid_w{w}"] = float(1.0 - np.dot(ev_unit, tc))
        return d


class StreamingSessionFeaturizer:
    """Featurise one session **event by event**, exactly as the batch path.

    Drives :class:`~tracemill.phase.segmentation.IncrementalSegmentation` and
    :class:`IncrementalNeighbor` so each :meth:`push` returns the
    :class:`EventExample` that :func:`featurize_session_events` would emit for
    that event over the full prefix — but online and in bounded state, so phases
    can be stamped live as events arrive instead of at the session boundary.
    """

    def __init__(self, session_id: str, source: str, seg_params, neighbor_params) -> None:
        from .segmentation import IncrementalSegmentation

        self._session_id = session_id
        self._source = source
        self._seg = IncrementalSegmentation(seg_params) if seg_params is not None else None
        self._nbr = (
            IncrementalNeighbor(neighbor_params.windows) if neighbor_params is not None else None
        )

    def push(self, event_id: str, event: dict) -> EventExample:
        text = _extract_text(event)
        seg = self._seg.push(event.get("phase_signals")) if self._seg is not None else {}
        nbr = self._nbr.push(text) if self._nbr is not None else {}
        return EventExample(
            session_id=self._session_id,
            source=self._source,
            event_id=event_id,
            symbolic=_symbolic_dict(event),
            numeric=_numeric_dict(event),
            text=text,
            phase_signals=tuple(event.get("phase_signals") or ()),
            seg=seg,
            nbr=nbr,
            content_bearing=is_content_bearing(event),
        )


@lru_cache(maxsize=1)
def _embedder():
    """Load the frozen model2vec embedder once.

    ``lru_cache`` memoises the *result*, including a failed load (returned as
    ``None``): if the artifact is unavailable — e.g. an offline host with no
    cached copy — we degrade once and never re-attempt the fetch. Without this,
    the raising call was not cached, so every event on the live path re-tried the
    network fetch and stalled the loop on connection timeout.
    """
    try:
        from model2vec import StaticModel

        return StaticModel.from_pretrained(MODEL2VEC_NAME)
    except Exception as exc:  # noqa: BLE001 - any load failure degrades, once
        logger.warning(
            "model2vec embedder %r unavailable (%s); phase/boundary text features "
            "fall back to zeros for this process",
            MODEL2VEC_NAME,
            exc,
        )
        return None


def embed_texts(texts: Sequence[str]) -> np.ndarray:
    """Frozen model2vec embedding of a list of texts → (n, 256) float32.

    Returns zero vectors if the embedder could not be loaded (see
    :func:`_embedder`), so callers never raise on a missing artifact.
    """

    if not texts:
        return np.zeros((0, MODEL2VEC_DIM), dtype=np.float32)
    model = _embedder()
    if model is None:
        return np.zeros((len(texts), MODEL2VEC_DIM), dtype=np.float32)
    vecs = model.encode(list(texts))
    return np.asarray(vecs, dtype=np.float32)


def merged_symbolic(
    example: EventExample, include_seg: bool = False, nbr_prefixes: tuple[str, ...] = ()
) -> dict[str, float]:
    """Symbolic + numeric (+ optional segmentation / neighbor) dict for a DictVectorizer."""

    d = dict(example.symbolic)
    d.update(example.numeric)
    if include_seg:
        d.update(getattr(example, "seg", {}) or {})
    if nbr_prefixes:
        for k, v in (getattr(example, "nbr", {}) or {}).items():
            if k.startswith(nbr_prefixes):
                d[k] = v
    return d


# ---------------------------------------------------------------------------
# Session featurisation (shared by training and runtime inference)
# ---------------------------------------------------------------------------


def featurize_session_events(
    session_id: str,
    source: str,
    events: dict[str, dict],
    seg_params=None,
    neighbor_params=None,
) -> list[EventExample]:
    """Featurise every event of one session into ordered, label-free examples.

    ``events`` maps ``event_id -> event-row dict`` (each from
    :func:`tracemill.phase.event_rows.event_to_feature_row`). Returns
    :class:`EventExample` objects in ``seq`` order with empty ``phases`` — the
    training path overlays the target; inference uses them directly.

    All features are **causal** when ``neighbor_params`` only drives the
    trailing-only centroid family (the production contract).
    """

    if not events:
        return []
    seq = {eid: int(ev.get("seq") or 0) for eid, ev in events.items()}
    ordered = sorted(events, key=lambda e: seq.get(e, 0))

    seg_feats: dict[str, dict[str, float]] = {}
    if seg_params is not None:
        from .segmentation import session_segmentation_features

        seg_feats = session_segmentation_features(
            [events[e].get("phase_signals") for e in ordered],
            ordered,
            seg_params,
        )

    nbr_feats: dict[str, dict[str, float]] = {}
    if neighbor_params is not None:
        nbr_feats = _neighbor_features(
            ordered,
            [_extract_text(events[e]) for e in ordered],
            neighbor_params.windows,
        )

    out: list[EventExample] = []
    for eid in ordered:
        ev = events[eid]
        out.append(
            EventExample(
                session_id=session_id,
                source=source,
                event_id=eid,
                symbolic=_symbolic_dict(ev),
                numeric=_numeric_dict(ev),
                text=_extract_text(ev),
                phase_signals=tuple(ev.get("phase_signals") or ()),
                seg=seg_feats.get(eid, {}),
                nbr=nbr_feats.get(eid, {}),
                content_bearing=is_content_bearing(ev),
            )
        )
    return out
