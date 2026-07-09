// Typed client for the read-only dashboard API served by `traceforge dashboard`.
//
// Not yet wired into the views — they still read the synthetic generator
// (src/data/runs.ts). Each view is switched to these fetchers during the
// per-view wiring tasks (docs/dashboard-spec.md, section 6, tasks D5-D8);
// the per-view aggregate fetchers (fleet / triage / cost / coverage) are added
// then, alongside their response types.
//
// The API serializes the shapes in @/lib/types with Date fields as ISO-8601
// strings. `reviveRun` turns those back into Date objects so the presentational
// components (which call Date methods) keep working unchanged.

import type { Run, TEvent } from "@/lib/types";

export const API_BASE = "/api";

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

export async function getRun(id: string, signal?: AbortSignal): Promise<Run> {
  const w = await getJson<RunWire>(
    `/runs/${encodeURIComponent(id)}`,
    signal,
  );
  return reviveRun(w);
}
