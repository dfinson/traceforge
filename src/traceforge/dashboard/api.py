"""Route handlers for the dashboard JSON API.

Each handler is a pure function ``(repository, regex-match, query) -> JSON-able``
registered onto the server's route table by :func:`register_api_routes`, which
:func:`traceforge.dashboard.server.create_server` calls once at construction.
Every read goes through :class:`DashboardRepository` (``mode=ro``); nothing here
mutates state.

The API is deliberately **thin** (see ``docs/dashboard-spec.md`` fork 2): a single
``GET /api/runs`` returns every run fully assembled — event bodies included — and
the ported frontend aggregates client-side exactly as the approved mock did
(Fleet KPIs + rail, the per-run ribbons, and the Triage/Cost/Coverage lenses all
read one shared ``Run[]``). ``GET /api/runs/{id}`` returns a single run for the
drill-in / live refetch.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from traceforge.dashboard.repository import DashboardRepository
from traceforge.dashboard.server import register_route

_Query = Mapping[str, list[str]]


def get_runs(
    repo: DashboardRepository, _match: re.Match[str], _query: _Query
) -> list[dict[str, Any]]:
    """``GET /api/runs`` — every run, fully assembled (the fleet-wide dataset).

    Returns an empty list when there is no output database yet (the SDK-embed /
    not-configured degraded mode), so the SPA renders an empty state instead of
    surfacing a 500.
    """
    if not repo.has_output_db():
        return []
    runs: list[dict[str, Any]] = []
    for session_id in repo.list_run_ids():
        run = repo.build_run(session_id)
        if run is not None:
            runs.append(run)
    return runs


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
