// Shared domain types for the TraceForge console.
//
// These describe the shapes the dashboard renders. They started life inside the
// synthetic generator (src/data/runs.ts) and were lifted here so the real API
// client (src/lib/api.ts) and the views can depend on the contract without
// pulling in the mock data generator. The generator now re-exports these.
//
// The field-by-field mapping from these shapes to real SQLite sources lives in
// docs/dashboard-spec.md (section 3, "Data contract").

export type RiskLevel = 0 | 1 | 2 | 3;
export type Verdict = "allow" | "warn" | "escalate" | "deny" | "transform";
export type SegKind = "session" | "activity" | "step";

export interface Tool {
  n: string;
  cat: string;
  canon: string;
  w: number;
}

export interface Evidence {
  mitre: [string, string];
  preds: string[];
  pii: string;
  ifc: string;
  ptr: string;
}

export interface Reco {
  action: Verdict;
  why: string;
}

export interface Seg {
  id: string;
  kind: SegKind;
  parent: string | null;
  title: string;
  risk: RiskLevel;
}

export interface TEvent {
  id: string;
  t: Date;
  tool: Tool;
  kind: string;
  summary: string;
  risk: RiskLevel;
  score: number;
  action: Verdict;
  cost: number;
  tokens: number;
  dur: number;
  phase: string;
  seg: string;
  file: string;
  turn: string;
  retry: boolean;
  cls: { canon: string; cat: string; conf: number };
  ev: Evidence | null;
  reco: Reco;
  gap: string | null;
  payload: Record<string, unknown>;
}

export interface Taint {
  flow: string;
  det: string;
  lvl: number;
}
export interface Trust {
  who: string;
  ttl: string;
  lvl: number;
}
export interface McpAlert {
  srv: string;
  msg: string;
  lvl: number;
}

// A recorded break in observability coverage (output-sink `context_gaps` row).
// Session-scoped, not per-event — the Coverage view lists these per run. `t` is
// kept as an ISO string (not revived to Date); it is not rendered as a Date.
export interface RunGap {
  t: string;
  dropped: number;
  reason: string;
}

export interface Run {
  id: string;
  repo: string;
  agent: string;
  model: string;
  title: string;
  live: boolean;
  segs: Seg[];
  events: TEvent[];
  usage: { in: number; out: number; cost: number };
  started: Date;
  durMs: number;
  // null when no cross-session drift baseline has been recorded for the run (it
  // lives in the cross-session governance store, which may be absent). RunView
  // renders "n/a".
  drift: number | null;
  peak: RiskLevel;
  taint: Taint[];
  trust: Trust[];
  mcp: McpAlert[];
  gaps: RunGap[];
}

// Risk-level labels, index-aligned to RiskLevel (0..3). Display + data concern,
// shared by badges, ribbons and formatters — so it lives with the types.
export const RISK = ["safe", "caution", "danger", "critical"] as const;
