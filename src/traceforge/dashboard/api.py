"""Route handlers for the dashboard JSON API.

Each handler is a pure function ``(repository, regex-match, query) -> JSON-able``
registered onto the server's route table by :func:`register_api_routes`, which
:func:`traceforge.dashboard.server.create_server` calls once at construction.
Every read goes through :class:`DashboardRepository` (``mode=ro``); nothing here
mutates state.

The API is deliberately **thin** (see ``docs/dashboard-spec.md`` fork 2): a single
``GET /api/runs`` returns the most-recent *window* of runs, fully assembled — event
bodies included — and the ported frontend aggregates client-side exactly as the
approved mock did (Fleet KPIs + rail, the per-run ribbons, and the Triage/Cost/
Coverage lenses all read one shared ``Run[]``). ``GET /api/runs/{id}`` returns a
single run for the drill-in / live refetch.

``GET /api/runs`` is **bounded** so a high-volume store can't produce an unbounded
payload: ``?limit`` (default 200, hard server-side max 500) and ``?offset`` select
the window, ordered most-recent-first. Each view's aggregation therefore reflects
the most-recent N runs — correct for a live console. See ``docs/dashboard-spec.md``
for the as-built note.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from traceforge.dashboard.repository import DashboardRepository
from traceforge.dashboard.server import register_route

_Query = Mapping[str, list[str]]

# Run-list windowing. The default keeps the fleet payload sane on ordinary stores;
# the hard max stops a crafted ``?limit=99999`` from re-unbounding the endpoint.
DEFAULT_RUNS_LIMIT = 200
MAX_RUNS_LIMIT = 500


def _bounded_int(
    query: _Query, name: str, *, default: int, minimum: int, maximum: int | None
) -> int:
    """Parse one query param as an int, falling back/clamping instead of erroring.

    Missing or non-numeric -> ``default``; below ``minimum`` -> ``minimum``; above
    ``maximum`` (when set) -> ``maximum``. Read-only, so a bad param never 500s.
    """
    raw = query.get(name)
    if not raw:
        return default
    try:
        value = int(raw[0])
    except (TypeError, ValueError):
        return default
    if value < minimum:
        return minimum
    if maximum is not None and value > maximum:
        return maximum
    return value


def _runs_limit(query: _Query) -> int:
    """Effective page size: default 200, clamped to [1, 500]."""
    return _bounded_int(
        query, "limit", default=DEFAULT_RUNS_LIMIT, minimum=1, maximum=MAX_RUNS_LIMIT
    )


def _runs_offset(query: _Query) -> int:
    """Effective offset: default 0, floored at 0, unbounded above."""
    return _bounded_int(query, "offset", default=0, minimum=0, maximum=None)


def get_runs(
    repo: DashboardRepository, _match: re.Match[str], query: _Query
) -> list[dict[str, Any]]:
    """``GET /api/runs`` — the most-recent window of runs, fully assembled.

    Bounded by ``?limit`` (default 200, max 500) + ``?offset``, ordered
    most-recent-first, so a high-volume store yields a paged payload rather than an
    unbounded one. Returns an empty list when there is no output database yet (the
    SDK-embed / not-configured degraded mode) so the SPA renders an empty state
    instead of surfacing a 500.
    """
    if not repo.has_output_db():
        return []
    limit = _runs_limit(query)
    offset = _runs_offset(query)
    session_ids = repo.list_run_ids(limit=limit, offset=offset)
    return repo.build_runs(session_ids)


def get_run(
    repo: DashboardRepository, match: re.Match[str], _query: _Query
) -> dict[str, Any] | None:
    """``GET /api/runs/{id}`` — one fully-assembled run, or ``None`` (-> 404)."""
    if not repo.has_output_db():
        return None
    return repo.build_run(match.group("id"))


_REGISTERED = False


def register_api_routes() -> None:
    """Append the view routes to the server's route table (idempotent).

    Registration mutates the module-global table in ``server``; the guard keeps
    repeated ``create_server`` calls (e.g. across tests) from stacking duplicate
    routes. The run-list pattern is registered before ``/{id}`` so a bare
    ``/api/runs`` never gets captured as an id.
    """
    global _REGISTERED
    if _REGISTERED:
        return
    register_route(r"^/api/runs/?$", get_runs)
    register_route(r"^/api/runs/(?P<id>[^/]+)/?$", get_run)
    _REGISTERED = True
