"""Session workflow dimensions — phase and visibility.

These are derived/presentation concerns, not intrinsic semantic properties
of an action. They live separately from the classification taxonomy.
"""

from __future__ import annotations

from enum import StrEnum


class Phase(StrEnum):
    """Workflow stage inferred from classification dimensions of recent events."""

    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    VERIFICATION = "verification"
    EXPLORATION = "exploration"
    REVIEW = "review"


class Visibility(StrEnum):
    """Display audience for an event in a trace view.

    SYSTEM = bookkeeping, not shown to end users.
    VISIBLE = shown by default.
    COLLAPSED = shown but minimized (e.g. repeated similar events).
    """

    VISIBLE = "visible"
    SYSTEM = "system"
    COLLAPSED = "collapsed"
