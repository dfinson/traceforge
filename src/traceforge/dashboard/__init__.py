"""traceforge local dashboard — read-only API + bundled SPA.

This package powers the ``traceforge dashboard`` command: a small read-only HTTP
server (:mod:`traceforge.dashboard.server`) that serves the built single-page app
from ``static/`` and a JSON API backed by :mod:`traceforge.dashboard.repository`,
which reads (and only ever reads) traceforge's two SQLite databases.
"""

from __future__ import annotations

from traceforge.dashboard.repository import (
    DashboardPaths,
    DashboardRepository,
    resolve_paths,
)

__all__ = ["DashboardPaths", "DashboardRepository", "resolve_paths"]
