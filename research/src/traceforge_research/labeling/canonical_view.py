"""Render a canonical session view from a per-session parquet.

The canonical view is the markdown the LLM labeler / red-teamer sees. It is
deterministic, ordered by ``seq``, and exposes only the fields the production
classifier will consume — no raw JSONL, no fields the classifier cannot see.

Truncation is content-budgeted from ``labeling-runtime.yaml``; when a session
exceeds the character budget we keep the first and last halves and elide the
middle with a single ``[...elided N events...]`` marker so the LLM still sees
session shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field

from ..config import CanonicalViewConfig

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class CanonicalEventView(BaseModel):
    """One event as the LLM sees it. Frozen, JSON-serialisable.

    Mirrors the parquet schema exactly so a labeler that names ``event_id``
    is naming the same identifier the eventual training join will key on.
    """

    model_config = _FROZEN

    event_id: str
    seq: int
    kind: str
    tool_name: str | None = None
    mechanism: str | None = None
    effect: str | None = None
    scope: tuple[str, ...] = Field(default_factory=tuple)
    role: tuple[str, ...] = Field(default_factory=tuple)
    action: tuple[str, ...] = Field(default_factory=tuple)
    capability: tuple[str, ...] = Field(default_factory=tuple)
    phase_signals: tuple[str, ...] = Field(default_factory=tuple)
    motivation: str | None = None
    intent: str | None = None
    user_message: str | None = None
    assistant_message: str | None = None
    payload_preview: str | None = None


@dataclass(frozen=True)
class CanonicalSessionView:
    session_id: str
    events: tuple[CanonicalEventView, ...]
    elided_count: int
    total_chars: int


def _coerce_list(value) -> tuple[str, ...]:  # noqa: ANN001
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if v is not None)
    return (str(value),)


def _coerce_optional(value) -> str | None:  # noqa: ANN001
    if value is None:
        return None
    s = str(value)
    return s if s and s != "None" else None


def _summarise_payload(
    kind: str,
    payload: dict,
    cfg: CanonicalViewConfig,
) -> tuple[str | None, str | None, str | None]:
    """Return (user_message, assistant_message, payload_preview).

    The first two fields surface the textual signal the boundary classifier
    relies on (assistant micro-task phrases, user goal-change phrases). The
    third is a generic preview of any other event payload.
    """
    if not payload:
        return None, None, None
    user_msg = None
    assistant_msg = None
    if kind == "message.user":
        user_msg = payload.get("content")
    elif kind == "message.assistant":
        assistant_msg = payload.get("content")
    preview_src = json.dumps(payload, ensure_ascii=False)
    budget = cfg.event_payload_preview_chars.value
    preview = (preview_src[:budget] + "…") if len(preview_src) > budget else preview_src
    return user_msg, assistant_msg, preview


def load_session_view(
    parquet_paths: Path | list[Path] | tuple[Path, ...],
    cfg: CanonicalViewConfig,
) -> CanonicalSessionView:
    """Read per-session parquet(s) and build a :class:`CanonicalSessionView`.

    Accepts a single path OR a list of shard paths for the same session_id
    (``ParquetSink`` rolls files on session_ended/paused events, so one
    session can span ``{sid}.parquet`` + ``{sid}.1.parquet`` + ...). Shards
    are concatenated and re-sorted by the per-session monotonic ``seq``.
    """

    if isinstance(parquet_paths, Path):
        parquet_paths = [parquet_paths]
    paths = list(parquet_paths)
    if not paths:
        raise ValueError("load_session_view requires at least one parquet path")

    rows: list[dict] = []
    for p in paths:
        rows.extend(pq.read_table(p).to_pylist())
    rows.sort(key=lambda r: int(r.get("seq") or 0))

    events: list[CanonicalEventView] = []
    for row in rows:
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except json.JSONDecodeError:
            payload = {}
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except json.JSONDecodeError:
            metadata = {}
        intent = metadata.get("tool_intent") or metadata.get("intent")
        kind = str(row.get("kind") or "")
        user_msg, assistant_msg, preview = _summarise_payload(kind, payload, cfg)
        events.append(
            CanonicalEventView(
                event_id=str(row.get("event_id") or ""),
                seq=int(row.get("seq") or 0),
                kind=kind,
                tool_name=_coerce_optional(row.get("tool_name")),
                mechanism=_coerce_optional(row.get("mechanism")),
                effect=_coerce_optional(row.get("effect")),
                scope=_coerce_list(row.get("scope")),
                role=_coerce_list(row.get("role")),
                action=_coerce_list(row.get("action")),
                capability=_coerce_list(row.get("capability")),
                phase_signals=_coerce_list(row.get("phase_signals")),
                motivation=_coerce_optional(row.get("motivation")),
                intent=_coerce_optional(intent),
                user_message=user_msg,
                assistant_message=assistant_msg,
                payload_preview=preview,
            )
        )

    return CanonicalSessionView(
        session_id=str(rows[0].get("session_id")) if rows else paths[0].stem,
        events=tuple(events),
        elided_count=0,
        total_chars=0,
    )


def _format_event(ev: CanonicalEventView) -> str:
    lines = [f"### Event {ev.seq} — `{ev.kind}`  (event_id `{ev.event_id}`)"]
    if ev.tool_name:
        lines.append(f"- tool: `{ev.tool_name}`")
    dims: list[str] = []
    if ev.mechanism:
        dims.append(f"mechanism={ev.mechanism}")
    if ev.effect:
        dims.append(f"effect={ev.effect}")
    if ev.scope:
        dims.append(f"scope={list(ev.scope)}")
    if ev.role:
        dims.append(f"role={list(ev.role)}")
    if ev.action:
        dims.append(f"action={list(ev.action)}")
    if ev.capability:
        dims.append(f"capability={list(ev.capability)}")
    if dims:
        lines.append("- classification: " + ", ".join(dims))
    if ev.phase_signals:
        lines.append(f"- phase_signals: {list(ev.phase_signals)}")
    if ev.motivation:
        lines.append(f"- motivation: {ev.motivation}")
    if ev.intent:
        lines.append(f"- intent: {ev.intent}")
    if ev.user_message:
        lines.append(f"- user_message: {ev.user_message}")
    if ev.assistant_message:
        lines.append(f"- assistant_message: {ev.assistant_message}")
    if ev.payload_preview and not ev.user_message and not ev.assistant_message:
        lines.append(f"- payload: {ev.payload_preview}")
    return "\n".join(lines)


def render_markdown(
    view: CanonicalSessionView,
    cfg: CanonicalViewConfig,
) -> tuple[str, CanonicalSessionView]:
    """Render the session as the markdown the LLM sees, applying the budget.

    Returns the rendered text and a possibly-trimmed view (with elided_count
    and total_chars filled in). Truncation keeps the head and tail and elides
    the middle.
    """

    blocks = [_format_event(ev) for ev in view.events]
    full = "\n\n".join(blocks)
    budget = cfg.max_session_chars.value
    if len(full) <= budget:
        return full, CanonicalSessionView(
            session_id=view.session_id,
            events=view.events,
            elided_count=0,
            total_chars=len(full),
        )

    # Greedy two-sided truncation by event count until under budget.
    head_count = hi // 2
    tail_count = hi - head_count
    while head_count + tail_count > 1:
        head = view.events[:head_count]
        tail = view.events[-tail_count:]
        rendered = (
            "\n\n".join(_format_event(e) for e in head)
            + f"\n\n_[…elided {hi - head_count - tail_count} events…]_\n\n"
            + "\n\n".join(_format_event(e) for e in tail)
        )
        if len(rendered) <= budget:
            return rendered, CanonicalSessionView(
                session_id=view.session_id,
                events=tuple(head) + tuple(tail),
                elided_count=hi - head_count - tail_count,
                total_chars=len(rendered),
            )
        # Drop one from whichever half is bigger.
        if head_count >= tail_count:
            head_count -= 1
        else:
            tail_count -= 1

    truncated = view.events[:1]
    rendered = _format_event(truncated[0])[:budget]
    return rendered, CanonicalSessionView(
        session_id=view.session_id,
        events=truncated,
        elided_count=hi - 1,
        total_chars=len(rendered),
    )


__all__ = [
    "CanonicalEventView",
    "CanonicalSessionView",
    "load_session_view",
    "render_markdown",
]
