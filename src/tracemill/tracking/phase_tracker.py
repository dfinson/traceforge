"""Streaming phase segmentation: the debounced majority-vote PhaseTracker.

Consumes per-event phase signals one at a time and maintains an
incrementally-built phase timeline. The algorithm is a debounced majority vote
over a sliding window of per-event phase signals: the current block's phase is
the window mode, and a new block opens only after the mode changes for
``debounce`` consecutive events. O(1) per event.

The per-event phase signal is the existing ``metadata.phases`` estimate the
enricher already produces, collapsed to a single dominant phase. This module is
the *phase* system only; it is independent of the activity/step segmentation
system and does not consume or emit activity labels.

See docs/design-phase-tracker.md for the design and rationale (why not
BOCPD/HMM: short sessions, crisp categorical phase labels, no posterior
needed). All numeric knobs come from :class:`PhaseTrackerConfig`; nothing is
hardcoded here.
"""

from __future__ import annotations

from collections import Counter, deque
from datetime import datetime

from tracemill.config.models import PhaseTrackerConfig

from .models import (
    PhaseBlock,
    PhaseStats,
    PhaseSummary,
    PhaseTimeline,
    PhaseTransition,
)


def resolve_phase_root(phase: str, depth: int) -> str:
    """Group a phase dot-path to the root used for boundary comparison.

    ``resolve_phase_root('verification.lint', 1) == 'verification'``. The
    tracker compares roots when deciding boundaries, so that sub-phase churn
    (``verification.lint`` -> ``verification.test``) does not open a new block.
    """

    if depth <= 0:
        return phase
    return ".".join(phase.split(".")[:depth])


def _mode(window: deque[str]) -> str:
    """Most common element in the window, ties broken by most-recent occurrence.

    Recency tie-breaking keeps the tracker responsive: when two signals are
    equally frequent, the fresher one wins.
    """

    counts = Counter(window)
    top = max(counts.values())
    for item in reversed(window):
        if counts[item] == top:
            return item
    return window[-1]  # unreachable for non-empty window


class _OpenBlock:
    """Mutable accumulator for the block currently being built."""

    __slots__ = (
        "session_id",
        "phase_root",
        "start_time",
        "end_time",
        "event_count",
        "tool_names",
        "_motivations",
        "_phase_roots",
        "_full_phases",
    )

    def __init__(self, session_id: str, phase_root: str, timestamp: datetime) -> None:
        self.session_id = session_id
        self.phase_root = phase_root
        self.start_time = timestamp
        self.end_time = timestamp
        self.event_count = 0
        self.tool_names: list[str] = []
        self._motivations: list[str] = []
        self._phase_roots: list[str] = []
        self._full_phases: list[str] = []

    def add(
        self,
        phase: str,
        phase_root: str,
        timestamp: datetime,
        tool_name: str | None,
        motivation: str | None,
    ) -> None:
        self.event_count += 1
        self.end_time = timestamp
        self._full_phases.append(phase)
        self._phase_roots.append(phase_root)
        if tool_name:
            self.tool_names.append(tool_name)
        if motivation:
            self._motivations.append(motivation)

    def _dominant_phase(self) -> str:
        """Most common full phase dot-path in the block (falls back to root)."""

        if not self._full_phases:
            return self.phase_root
        return Counter(self._full_phases).most_common(1)[0][0]

    def _dominant_motivation(self) -> str | None:
        if not self._motivations:
            return None
        return Counter(self._motivations).most_common(1)[0][0]

    def _minority_phases(self) -> tuple[tuple[str, int], ...]:
        minority = Counter(r for r in self._phase_roots if r != self.phase_root)
        return tuple(sorted(minority.items(), key=lambda kv: (-kv[1], kv[0])))

    def close(self) -> PhaseBlock:
        return PhaseBlock(
            session_id=self.session_id,
            phase=self._dominant_phase(),
            phase_root=self.phase_root,
            start_time=self.start_time,
            end_time=self.end_time,
            event_count=self.event_count,
            tool_names=tuple(self.tool_names),
            dominant_motivation=self._dominant_motivation(),
            minority_phases=self._minority_phases(),
        )


class PhaseTracker:
    """Streaming phase segmentation for a single session.

    Single-writer; no locking. Feed events in session order via
    :meth:`observe`; query the live phase via :attr:`phase`; produce a frozen
    :class:`PhaseTimeline` via :meth:`snapshot`/:meth:`finalize`.
    """

    def __init__(self, session_id: str, config: PhaseTrackerConfig | None = None) -> None:
        self.session_id = session_id
        self._config = config or PhaseTrackerConfig()
        self._window: deque[str] = deque(maxlen=max(1, self._config.window_size))
        self._open: _OpenBlock | None = None
        self._closed_blocks: list[PhaseBlock] = []
        self._transitions: list[PhaseTransition] = []
        self._candidate_streak = 0
        self._finalized = False

    @property
    def phase(self) -> str | None:
        """Phase of the currently-open block (real-time query)."""

        return self._open.phase_root if self._open is not None else None

    def observe(
        self,
        phase: str,
        timestamp: datetime,
        event_id: str,
        *,
        tool_name: str | None = None,
        motivation: str | None = None,
    ) -> tuple[str, PhaseTransition | None]:
        """Process one event.

        ``phase`` is the per-event phase signal (the enricher's collapsed
        ``metadata.phases`` estimate). Returns ``(current_phase, transition_or_None)``;
        ``current_phase`` is what the enricher stamps onto ``event.metadata.phase``.
        """

        if self._finalized:
            raise RuntimeError("PhaseTracker.observe called after finalize()")

        root = resolve_phase_root(phase, self._config.phase_root_depth)
        self._window.append(root)
        transition: PhaseTransition | None = None

        if self._open is None:
            # First event seeds the first block; no transition.
            self._open = _OpenBlock(self.session_id, root, timestamp)
            self._candidate_streak = 0
        else:
            new_mode = _mode(self._window)
            if new_mode == self._open.phase_root:
                self._candidate_streak = 0
            else:
                self._candidate_streak += 1
                if self._candidate_streak >= self._config.debounce:
                    self._closed_blocks.append(self._open.close())
                    from_phase = self._open.phase_root
                    self._open = _OpenBlock(self.session_id, new_mode, timestamp)
                    transition = PhaseTransition(
                        session_id=self.session_id,
                        from_phase=from_phase,
                        to_phase=new_mode,
                        timestamp=timestamp,
                        trigger_event_id=event_id,
                    )
                    self._transitions.append(transition)
                    self._candidate_streak = 0

        self._open.add(phase, root, timestamp, tool_name, motivation)
        return self._open.phase_root, transition

    def snapshot(self) -> PhaseTimeline:
        """Immutable snapshot of the timeline so far, including the open block."""

        blocks = tuple(self._closed_blocks)
        if self._open is not None:
            blocks += (self._open.close(),)
        return PhaseTimeline(
            session_id=self.session_id,
            blocks=blocks,
            transitions=tuple(self._transitions),
        )

    def finalize(self) -> PhaseTimeline:
        """Close the session and return the final timeline. Idempotent."""

        if not self._finalized:
            if self._open is not None:
                self._closed_blocks.append(self._open.close())
                self._open = None
            self._finalized = True
        return PhaseTimeline(
            session_id=self.session_id,
            blocks=tuple(self._closed_blocks),
            transitions=tuple(self._transitions),
        )

    def summarize(self) -> PhaseSummary:
        """Aggregate statistics from the current (or finalized) timeline."""

        timeline = self.snapshot() if not self._finalized else self.finalize()
        blocks = timeline.blocks
        total_events = sum(b.event_count for b in blocks)
        total_duration = timeline.total_duration_seconds

        by_phase: dict[str, dict[str, float]] = {}
        for b in blocks:
            agg = by_phase.setdefault(
                b.phase_root,
                {"event_count": 0, "block_count": 0, "duration": 0.0},
            )
            agg["event_count"] += b.event_count
            agg["block_count"] += 1
            agg["duration"] += b.duration_seconds

        stats = tuple(
            PhaseStats(
                phase=phase,
                event_count=int(agg["event_count"]),
                block_count=int(agg["block_count"]),
                total_duration_seconds=agg["duration"],
                fraction_of_events=(agg["event_count"] / total_events) if total_events else 0.0,
                fraction_of_duration=(agg["duration"] / total_duration) if total_duration else 0.0,
                avg_block_duration_seconds=(agg["duration"] / agg["block_count"]) if agg["block_count"] else 0.0,
            )
            for phase, agg in sorted(by_phase.items(), key=lambda kv: (-kv[1]["event_count"], kv[0]))
        )

        transition_pairs = Counter((t.from_phase, t.to_phase) for t in timeline.transitions)
        most_common = tuple(
            (frm, to, count)
            for (frm, to), count in sorted(
                transition_pairs.items(), key=lambda kv: (-kv[1], kv[0])
            )
        )

        return PhaseSummary(
            session_id=self.session_id,
            total_events=total_events,
            total_duration_seconds=total_duration,
            by_phase=stats,
            transition_count=len(timeline.transitions),
            most_common_transitions=most_common,
        )
