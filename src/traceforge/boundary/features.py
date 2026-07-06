"""Per-gap featurisation for the boundary classifier (shared train/serve).

A *gap* is the transition point after event ``t``; it is featurised from the
canonical fields of ``t`` and its successor ``t+1`` (so the classifier sees the
transition), plus the causal segmentation-detector outputs at ``t``. This is the
single featuriser used by both leave-session-out training (via the research
label loader) and runtime/e2e inference, so there is no train/serve skew.

All features are **causal**: ``t+1`` has arrived by the time the gap after ``t``
is scored, and the segmentation features come from the online BOCPD / trailing
majority-vote state. The acausal ``position = seq / n`` feature used by the
earliest boundary baselines is intentionally **not** emitted here — ``n`` (total
session length) is unknowable mid-stream.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from traceforge.phase.features import (
    MAX_TEXT_CHARS,
    _extract_text,
    _numeric_dict,
    _symbolic_dict,
)
from traceforge.phase.segmentation import SegmentationParams, session_segmentation_features

#: Canonical fields whose change across the gap is an explicit transition signal.
_CHANGE_FIELDS = ("mechanism", "effect", "tool_name")


@dataclass
class GapExample:
    """One gap (the transition after ``after_event_id``), ready for vectorisation.

    Field names mirror :class:`traceforge.phase.features.EventExample` so the
    shared ``merged_symbolic`` / design-matrix machinery applies unchanged.
    """

    session_id: str
    source: str
    after_event_id: str
    symbolic: dict[str, float]
    numeric: dict[str, float]
    text: str
    seg: dict[str, float] = field(default_factory=dict)
    nbr: dict[str, float] = field(default_factory=dict)
    label: str = ""

    @property
    def event_id(self) -> str:
        """Alias so predictions can be keyed identically to the phase path."""
        return self.after_event_id


def _gap_symbolic(cur: dict, nxt: dict | None) -> dict[str, float]:
    """Symbolic features of the current event plus ``next_``-prefixed successor
    features and explicit change indicators across the gap."""

    sym = _symbolic_dict(cur)
    if nxt is not None:
        for k, v in _symbolic_dict(nxt).items():
            sym[f"next_{k}"] = v
        for fld in _CHANGE_FIELDS:
            if cur.get(fld) != nxt.get(fld):
                sym[f"changed_{fld}"] = 1.0
    return sym


def _gap_text(cur: dict, nxt: dict | None) -> str:
    text = _extract_text(cur)
    if nxt is not None:
        text = (text + " \n " + _extract_text(nxt))[: MAX_TEXT_CHARS * 2]
    return text


def build_gap_example(
    session_id: str,
    source: str,
    cur: dict,
    nxt: dict | None,
    seg: dict[str, float] | None = None,
) -> GapExample:
    """Build one :class:`GapExample` from a current feature row, its successor
    (or ``None`` for the final gap), and the causal seg features computed at
    ``cur``. Shared by the batch featuriser and the live streaming inferencer so
    there is no train/serve skew."""

    return GapExample(
        session_id=session_id,
        source=source,
        after_event_id=cur["event_id"],
        symbolic=_gap_symbolic(cur, nxt),
        numeric=_numeric_dict(cur),  # causal: no acausal position=seq/n
        text=_gap_text(cur, nxt),
        seg=seg or {},
    )


def featurize_session_gaps(
    session_id: str,
    source: str,
    events: dict[str, dict],
    seg_params: SegmentationParams | None = None,
) -> list[GapExample]:
    """Featurise every gap of one session causally, in ``seq`` order.

    ``events`` maps ``event_id -> feature-row dict`` (each from
    :func:`traceforge.phase.event_rows.event_to_feature_row`). Returns one
    :class:`GapExample` per event (the gap *after* it); the final event's gap has
    no successor. Labels are left empty — the research loader overlays them; the
    inference path uses the examples directly.
    """

    if not events:
        return []
    seq = {eid: int(ev.get("seq") or 0) for eid, ev in events.items()}
    ordered = sorted(events, key=lambda e: seq.get(e, 0))
    next_of = {ordered[i]: ordered[i + 1] for i in range(len(ordered) - 1)}

    seg_feats: dict[str, dict[str, float]] = {}
    if seg_params is not None:
        seg_feats = session_segmentation_features(
            [events[e].get("phase_signals") for e in ordered],
            ordered,
            seg_params,
        )

    out: list[GapExample] = []
    for aid in ordered:
        cur = events[aid]
        nxt = events.get(next_of.get(aid)) if next_of.get(aid) else None
        out.append(build_gap_example(session_id, source, cur, nxt, seg_feats.get(aid, {})))
    return out


__all__ = ["GapExample", "build_gap_example", "featurize_session_gaps"]
