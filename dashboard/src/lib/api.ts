// Typed client for the read-only dashboard API served by `traceforge dashboard`.
//
// Thin-API design (docs/dashboard-spec.md fork 2, resolved to B): the backend
// exposes `GET /api/runs` returning the most-recent *window* of runs fully
// assembled, plus `GET /api/runs/{id}` for the drill-in. Views aggregate
// client-side over the shared run array via the `useRuns()` hook
// (src/lib/queries.ts), exactly as the approved mock did against its synthetic
// generator (src/data/runs.ts).
//
// The list is bounded: `getRuns` requests `?limit=RUNS_PAGE_SIZE` and the server
// caps it (hard max 500), ordered most-recent-first, so a high-volume store can't
// return an unbounded payload. Fleet/Triage/Cost/Coverage therefore reflect the
// most-recent N runs — correct for a live console.
//
// The API serializes the shapes in @/lib/types with Date fields as ISO-8601
// strings. `reviveRun` turns those back into Date objects so the presentational
// components (which call Date methods) keep working unchanged.

import type { Run, TEvent } from "@/lib/types";

export const API_BASE = "/api";

// Default run-list window requested by `useRuns()`. The server clamps to a hard
// max (500); this is the "sane default" page size for the fleet views.
export const RUNS_PAGE_SIZE = 200;

export class ApiError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { Accept: "application/json" },
    signal,
  });
  if (!res.ok) {
    throw new ApiError(`GET ${path} failed: ${res.status}`, res.status);
  }
  return (await res.json()) as T;
}

// --- Health / degraded-mode detection -------------------------------------

export interface Health {
  output_db: string | null;
  system_db: string | null;
  has_output_db: boolean;
  has_system_memory: boolean;
}

export function getHealth(signal?: AbortSignal): Promise<Health> {
  return getJson<Health>("/health", signal);
}

// --- Date revival ----------------------------------------------------------

// The wire form matches Run/TEvent but with Date fields as ISO strings.
type EventWire = Omit<TEvent, "t"> & { t: string };
type RunWire = Omit<Run, "started" | "events"> & {
  started: string;
  events: EventWire[];
};

export function reviveRun(w: RunWire): Run {
  return {
    ...w,
    started: new Date(w.started),
    events: w.events.map((e) => ({ ...e, t: new Date(e.t) })),
  };
}

// --- Runs ------------------------------------------------------------------

export async function getRuns(
  signal?: AbortSignal,
  limit: number = RUNS_PAGE_SIZE,
): Promise<Run[]> {
  const wire = await getJson<RunWire[]>(`/runs?limit=${limit}`, signal);
  return wire.map(reviveRun);
}

export async function getRun(id: string, signal?: AbortSignal): Promise<Run> {
  const w = await getJson<RunWire>(
    `/runs/${encodeURIComponent(id)}`,
    signal,
  );
  return reviveRun(w);
}
