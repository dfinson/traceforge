"""Ingest nebius/SWE-agent-trajectories into traceforge enriched parquets.

SWE-agent trajectory format (per trajectory row):

    [
      {role: "system",  text: ""},     # often empty
      {role: "user",    text: <issue>},
      {role: "ai",      text: "<reasoning>\\n```\\n<command> [args]\\n```"},
      {role: "user",    text: <tool observation>},
      ... repeated pairs ...
      {role: "ai",      text: "...\\n```\\nsubmit\\n```"},
    ]

Each ``ai`` entry contains free-form reasoning followed by exactly one
fenced code block holding the command. We split the two and emit:

* ``message.user``  — for the initial issue and every observation
* ``message.assistant``  — for the reasoning prose
* ``tool.call.started`` + ``tool.call.completed`` — for the action

All events flow through traceforge's :class:`~traceforge.enricher.Enricher`
and :class:`~traceforge.sinks.parquet.ParquetSink` so the on-disk schema
matches the existing copilot-cli ingest exactly. The only added column is
``source`` which goes on the manifest, not the event row.

Filtering: per :doc:`research/docs/10-mixed-corpus-v2-plan`, we keep only
resolved (target=True) trajectories whose enriched event count is within
``max_events_per_session`` (set from yaml at the call site).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field

from ..paths import DATA_INTERIM, DATA_RAW

logger = logging.getLogger(__name__)

_FROZEN = ConfigDict(frozen=True, extra="forbid")

# A SWE-agent command block looks like:
#     ```
#     open file.py
#     ```
# or with bash language tag:
#     ```bash
#     ls -la
#     ```
_FENCE_RE = re.compile(r"```(?:[a-zA-Z0-9_+-]*)?\n(.*?)\n```", re.DOTALL)


class SweAgentIngestConfig(BaseModel):
    """YAML-loadable config for the SWE-agent ingest."""

    model_config = _FROZEN

    shard_paths: tuple[Path, ...] = Field(
        ...,
        description="Parquet shards to read (nebius/SWE-agent-trajectories format).",
    )
    output_dir: Path = Field(
        ...,
        description="Per-session parquet output. Files named <instance_id>.parquet.",
    )
    only_resolved: bool = Field(
        True,
        description="If True, keep only target=True (resolved) trajectories.",
    )
    max_sessions: int | None = Field(
        None,
        ge=1,
        description="Optional hard cap (debug / smoke).",
    )
    max_events_per_session: int | None = Field(
        None,
        ge=1,
        description=(
            "If set, skip any session whose enriched event count exceeds this. "
            "Should match labeling-runtime.yaml canonical_view.max_events_per_call."
        ),
    )


def default_shard_dir() -> Path:
    return DATA_RAW / "swe-agent-nebius" / "data"


def default_output_dir() -> Path:
    return DATA_INTERIM / "labeling-corpus" / "swe-agent-nebius"


# ---------------------------------------------------------------------------
# Trajectory → event extraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedAi:
    reasoning: str
    command: str  # full command string (first line after fence)
    args: str  # remainder of the fenced block (may be empty)
    tool_name: str  # first token of command
    raw: str


def _split_ai_entry(text: str) -> ParsedAi:
    """Split an ``ai`` entry into prose and the trailing command block.

    If no fence is found, treat the whole text as reasoning with an empty
    command (this happens on degenerate trajectories — rare but real).
    """

    m = list(_FENCE_RE.finditer(text or ""))
    if not m:
        return ParsedAi(reasoning=text or "", command="", args="", tool_name="", raw=text or "")
    last = m[-1]
    reasoning = (text[: last.start()] or "").strip()
    body = last.group(1).strip()
    head, _, rest = body.partition("\n")
    head = head.strip()
    tool_name = head.split()[0] if head else ""
    return ParsedAi(
        reasoning=reasoning, command=head, args=rest.strip(), tool_name=tool_name, raw=text
    )


# ---------------------------------------------------------------------------
# Main ingest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IngestStats:
    sessions_seen: int
    sessions_emitted: int
    sessions_skipped_too_large: int
    sessions_skipped_unresolved: int
    sessions_failed: int
    events_emitted: int
    failures: tuple[tuple[str, str], ...]


async def ingest(config: SweAgentIngestConfig) -> IngestStats:
    from traceforge.enricher import Enricher
    from traceforge.sinks.parquet import ParquetSink

    config.output_dir.mkdir(parents=True, exist_ok=True)

    sessions_seen = 0
    sessions_emitted = 0
    sessions_skipped_too_large = 0
    sessions_skipped_unresolved = 0
    sessions_failed = 0
    events_emitted = 0
    failures: list[tuple[str, str]] = []

    sink = ParquetSink(path=str(config.output_dir / "{session_id}.parquet"))

    try:
        for shard_path in config.shard_paths:
            logger.info("reading shard %s", shard_path)
            table = pq.read_table(shard_path)
            df = table.to_pandas()
            # Deterministic per-(instance, model) suffix so duplicate attempts
            # get unique session ids without collisions.
            df = df.reset_index(drop=True)
            seen_attempt: dict[tuple[str, str], int] = {}

            for _, row in df.iterrows():
                if config.max_sessions is not None and sessions_emitted >= config.max_sessions:
                    break

                sessions_seen += 1
                instance_id = str(row["instance_id"])
                model_name = str(row["model_name"])
                key = (instance_id, model_name)
                attempt_idx = seen_attempt.get(key, 0)
                seen_attempt[key] = attempt_idx + 1
                sid = f"{instance_id}__{model_name}__a{attempt_idx:03d}"

                if config.only_resolved and not bool(row["target"]):
                    sessions_skipped_unresolved += 1
                    continue

                try:
                    events = list(
                        _events_from_trajectory(
                            sid=sid,
                            trajectory=row["trajectory"],
                            model_name=str(row["model_name"]),
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    failures.append((sid, repr(exc)))
                    sessions_failed += 1
                    logger.exception("parse failed for %s", sid)
                    continue

                if (
                    config.max_events_per_session is not None
                    and len(events) > config.max_events_per_session
                ):
                    sessions_skipped_too_large += 1
                    logger.info(
                        "skip %s: %d events > cap %d",
                        sid,
                        len(events),
                        config.max_events_per_session,
                    )
                    continue

                # Enrich and emit.
                try:
                    enricher = Enricher()
                    out_count = 0
                    for ev in events:
                        enriched = enricher.process(ev)
                        for emitted in _iter_enriched(enriched):
                            await sink.on_event(emitted)
                            out_count += 1
                    sessions_emitted += 1
                    events_emitted += out_count
                    logger.info(
                        "ingested %s: %d input -> %d enriched events",
                        sid,
                        len(events),
                        out_count,
                    )
                except Exception as exc:  # noqa: BLE001
                    failures.append((sid, repr(exc)))
                    sessions_failed += 1
                    logger.exception("enrich failed for %s", sid)
    finally:
        await sink.close()

    return IngestStats(
        sessions_seen=sessions_seen,
        sessions_emitted=sessions_emitted,
        sessions_skipped_too_large=sessions_skipped_too_large,
        sessions_skipped_unresolved=sessions_skipped_unresolved,
        sessions_failed=sessions_failed,
        events_emitted=events_emitted,
        failures=tuple(failures),
    )


def _events_from_trajectory(
    sid: str,
    trajectory: Iterable[Any],
    model_name: str,
) -> Iterable[Any]:
    """Yield :class:`SessionEvent` objects from a SWE-agent trajectory list.

    SWE-agent doesn't carry timestamps. We synthesize a monotonic clock
    starting at a fixed epoch so seq ordering is stable.
    """

    from traceforge.types import EventKind, EventMetadata, SessionEvent

    base = datetime(2026, 1, 1, tzinfo=UTC)
    step = timedelta(seconds=1)
    tick = 0

    def _next_ts() -> datetime:
        nonlocal tick
        tick += 1
        return base + step * tick

    def _meta(raw_kind: str) -> EventMetadata:
        return EventMetadata(
            source_framework="swe_agent_nebius",
            ingestion_mode="replay",
            raw_kind=raw_kind,
            partial=False,
        )

    saw_first_user = False
    for entry in trajectory:
        role = entry["role"]
        text = entry["text"] or ""

        if role == "system":
            if text.strip():
                yield SessionEvent(
                    kind=EventKind.MESSAGE_SYSTEM,
                    session_id=sid,
                    timestamp=_next_ts(),
                    payload={"content": text},
                    raw_event={"role": role, "model": model_name},
                    metadata=_meta("system"),
                )
            continue

        if role == "user":
            kind = EventKind.MESSAGE_USER if not saw_first_user else EventKind.TOOL_CALL_COMPLETED
            saw_first_user = True
            if kind == EventKind.MESSAGE_USER:
                yield SessionEvent(
                    kind=kind,
                    session_id=sid,
                    timestamp=_next_ts(),
                    payload={"content": text},
                    raw_event={"role": role},
                    metadata=_meta("user_issue"),
                )
            else:
                # Observation following a tool call.
                yield SessionEvent(
                    kind=kind,
                    session_id=sid,
                    timestamp=_next_ts(),
                    payload={"output": text, "exit_code": 0},
                    raw_event={"role": role},
                    metadata=_meta("tool_observation"),
                )
            continue

        if role == "ai":
            parsed = _split_ai_entry(text)
            if parsed.reasoning:
                yield SessionEvent(
                    kind=EventKind.MESSAGE_ASSISTANT,
                    session_id=sid,
                    timestamp=_next_ts(),
                    payload={"content": parsed.reasoning},
                    raw_event={"role": role, "model": model_name},
                    metadata=_meta("assistant_reasoning"),
                )
            if parsed.command:
                yield SessionEvent(
                    kind=EventKind.TOOL_CALL_STARTED,
                    session_id=sid,
                    timestamp=_next_ts(),
                    payload={
                        "tool_name": parsed.tool_name,
                        "command": parsed.command,
                        "arguments": parsed.args,
                    },
                    raw_event={"role": role, "model": model_name},
                    metadata=_meta("tool_call"),
                )
            continue

        # Unknown role — emit raw.
        yield SessionEvent(
            kind=EventKind.RAW,
            session_id=sid,
            timestamp=_next_ts(),
            payload={"role": role, "text": text},
            raw_event={"role": role},
            metadata=_meta(f"unknown_role:{role}"),
        )


def _iter_enriched(result: Any) -> Iterable[Any]:
    if result is None:
        return ()
    if isinstance(result, list):
        return result
    return (result,)


def run_sync(config: SweAgentIngestConfig) -> IngestStats:
    return asyncio.run(ingest(config))


__all__ = [
    "IngestStats",
    "SweAgentIngestConfig",
    "default_output_dir",
    "default_shard_dir",
    "ingest",
    "run_sync",
]
