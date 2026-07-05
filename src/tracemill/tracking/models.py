"""Output data types for the phase tracker.

All types are frozen, following project convention: once a block or summary is
emitted it is immutable. See docs/design-phase-tracker.md for the full design.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class PhaseBlock:
    """A contiguous run of events dominated by a single phase.

    Boundaries are determined by the debounced majority-vote algorithm. Phase
    blocks are first-class pipeline data: emitted to sinks and the system DB on
    each boundary commit.
    """

    session_id: str
    phase: str
    """Dominant phase, dot-path string (e.g. 'verification.lint'). Derived from
    the per-event phase signals in this block."""

    phase_root: str
    """Root phase used for boundary detection (e.g. 'verification'). What the
    majority-vote window compares."""

    start_time: datetime
    end_time: datetime
    event_count: int

    tool_names: tuple[str, ...] = ()
    """Ordered tool names invoked during this block."""

    dominant_motivation: str | None = None
    """Most common motivation intent across events in this block, or None."""

    minority_phases: tuple[tuple[str, int], ...] = ()
    """Phase signals suggesting a different phase during this block, sorted by
    count desc. E.g. (('exploration', 3),) means 3 exploration-signal events
    appeared inside an implementation block."""

    @property
    def duration_seconds(self) -> float:
        return (self.end_time - self.start_time).total_seconds()


@dataclass(frozen=True)
class PhaseTransition:
    """A boundary between two adjacent phase blocks. Derivable from consecutive
    blocks; surfaced directly for downstream consumers."""

    session_id: str
    from_phase: str
    to_phase: str
    timestamp: datetime
    """Timestamp of the first event in the new block."""

    trigger_event_id: str
    """ID of the event that caused the boundary to commit."""


@dataclass(frozen=True)
class PhaseStats:
    """Aggregate stats for a single phase across the session."""

    phase: str
    event_count: int
    block_count: int
    total_duration_seconds: float
    fraction_of_events: float
    fraction_of_duration: float
    avg_block_duration_seconds: float


@dataclass(frozen=True)
class PhaseTimeline:
    """Complete phase segmentation of a session.

    Built incrementally — blocks are appended as they close. The last block may
    be open (still accumulating events) when produced via ``snapshot()``.
    """

    session_id: str
    blocks: tuple[PhaseBlock, ...] = ()
    transitions: tuple[PhaseTransition, ...] = ()

    @property
    def total_events(self) -> int:
        return sum(b.event_count for b in self.blocks)

    @property
    def total_duration_seconds(self) -> float:
        if not self.blocks:
            return 0.0
        return (self.blocks[-1].end_time - self.blocks[0].start_time).total_seconds()


@dataclass(frozen=True)
class PhaseSummary:
    """Aggregate statistics derived from a finalized PhaseTimeline.

    Provides the '60% implementation, 25% exploration' view.
    """

    session_id: str
    total_events: int
    total_duration_seconds: float
    by_phase: tuple[PhaseStats, ...] = ()
    transition_count: int = 0
    most_common_transitions: tuple[tuple[str, str, int], ...] = ()
    """Top transition pairs (from, to, count), sorted desc."""
