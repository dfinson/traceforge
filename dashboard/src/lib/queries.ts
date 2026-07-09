// React Query hooks over the read-only dashboard API.
//
// The dashboard follows the thin-API design (docs/dashboard-spec.md fork 2): one
// `GET /api/runs` returns the most-recent *window* of runs fully assembled, and
// every view aggregates client-side over that shared array — exactly as the
// approved mock did against its synthetic `RUNS` const. The window is bounded
// server-side (default 200, hard max 500); `getRuns` requests RUNS_PAGE_SIZE.
// React Query dedupes by query key, so the many components that call `useRuns()`
// (Fleet, its charts, RunView, Triage, Cost, Coverage) share a single fetch +
// cache entry.

import { useQuery } from "@tanstack/react-query";
import type { Query } from "@tanstack/react-query";
import { getHealth, getRuns } from "@/lib/api";
import type { Health } from "@/lib/api";
import type { Run } from "@/lib/types";

// Poll while any run is still live (spec fork 4: interval poll for v1), otherwise
// stay idle. Live runs tail the output DB, so their events/cost keep growing.
const LIVE_POLL_MS = 5000;

export function useRuns() {
  return useQuery<Run[]>({
    queryKey: ["runs"],
    queryFn: ({ signal }) => getRuns(signal),
    refetchInterval: (query: Query<Run[], Error>) =>
      query.state.data?.some((r) => r.live) ? LIVE_POLL_MS : false,
  });
}

export function useHealth() {
  return useQuery<Health>({
    queryKey: ["health"],
    queryFn: ({ signal }) => getHealth(signal),
    staleTime: 60_000,
  });
}
