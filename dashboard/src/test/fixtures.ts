// Seeded, in-memory fixtures for view/selector tests.
//
// These build the same domain shapes (@/lib/types) the real API returns, so views
// and selectors can be exercised without a live server. Every builder takes an
// overrides bag so a test can pin exactly the field under test and let the rest
// default to a realistic value.

import type {
  ModelUsage,
  Run,
  Seg,
  TEvent,
  Transcript,
  TranscriptTurn,
} from "@/lib/types";

let eventSeq = 0;
let runSeq = 0;

export function makeEvent(o: Partial<TEvent> = {}): TEvent {
  const id = o.id ?? `ev-${eventSeq++}`;
  return {
    id,
    t: o.t ?? new Date("2026-07-10T09:00:00Z"),
    tool: o.tool ?? { n: "read_file", cat: "read_only", canon: "read_file", w: 1 },
    kind: o.kind ?? "tool.call.function",
    summary: o.summary ?? "read a source file",
    risk: o.risk ?? 0,
    score: o.score ?? 0.12,
    action: o.action ?? "allow",
    cost: o.cost ?? null,
    tokens: o.tokens ?? 120,
    dur: o.dur ?? 14,
    phase: o.phase ?? "explore",
    seg: o.seg ?? "step-1",
    file: o.file ?? "src/app.ts",
    turn: o.turn ?? "turn-1",
    retry: o.retry ?? false,
    cls: o.cls ?? { canon: "read_file", cat: "read_only", conf: 0.99 },
    ev: o.ev ?? null,
    reco: o.reco ?? { action: "allow", why: "read-only access to a tracked file" },
    gap: o.gap ?? null,
    payload: o.payload ?? { path: "src/app.ts" },
  };
}

// A lifecycle event (session / turn / message / llm marker): carries no effect by
// nature, so its `cls.cat` is empty and it lands in the coverage "lifecycle"
// bucket — never the classifiable denominator.
export function makeLifecycleEvent(o: Partial<TEvent> = {}): TEvent {
  return makeEvent({
    kind: "turn.completed",
    tool: { n: "turn", cat: "", canon: "", w: 0 },
    summary: "assistant turn completed",
    cls: { canon: "", cat: "", conf: 0 },
    seg: "",
    ...o,
  });
}

export function makeModelUsage(o: Partial<ModelUsage> = {}): ModelUsage {
  // Spread overrides LAST so an explicit `null` (unknown signal) is honored
  // rather than coalesced back to a default — the "unknown → —" path Cost tests.
  return {
    model: "gpt-5",
    aiuNano: 40_000_000_000,
    premiumRequests: 2,
    requests: 10,
    inputUncached: 5_000,
    cacheRead: 2_000,
    cacheCreation: 500,
    input: 7_500,
    output: 3_000,
    ...o,
  };
}

const defaultSegs = (): Seg[] => [
  { id: "act-1", kind: "activity", parent: null, title: "Explore the repository", risk: 1 },
  { id: "step-1", kind: "step", parent: "act-1", title: "Read the source files", risk: 1 },
];

export function makeRun(o: Partial<Run> = {}): Run {
  const id = o.id ?? `run-${runSeq++}`;
  return {
    id,
    repo: o.repo ?? "acme/api",
    agent: o.agent ?? "copilot",
    model: o.model ?? "gpt-5",
    title: o.title ?? `Run ${id}`,
    live: o.live ?? false,
    segs: o.segs ?? defaultSegs(),
    events: o.events ?? [makeEvent()],
    // A supplied `usage` is taken verbatim so explicit nulls (unknown signals)
    // survive — a per-field `?? default` merge would coalesce them back and mask
    // the "unknown → —" rendering the Cost fixtures rely on.
    usage: o.usage ?? {
      in: 7_500,
      out: 3_000,
      cost: null,
      aiuNano: 40_000_000_000,
      premiumRequests: 2,
      inputUncached: 5_000,
      cacheRead: 2_000,
      cacheCreation: 500,
      requestsTotal: 10,
      models: [makeModelUsage()],
    },
    started: o.started ?? new Date("2026-07-10T09:00:00Z"),
    durMs: o.durMs ?? 8 * 60_000 + 30_000,
    drift: o.drift ?? null,
    peak: o.peak ?? 1,
    taint: o.taint ?? [],
    trust: o.trust ?? [],
    mcp: o.mcp ?? [],
    gaps: o.gaps ?? [],
  };
}

// A realistic two-run fleet exercising every view: classified tool events, a
// permission escalation (risk 2) and a destructive call (risk 3) for Triage,
// lifecycle events for the coverage scope split, per-model usage (including an
// unattributed "" model with unknown counts) for Cost, and a recorded gap +
// taint row for Coverage / Triage governance memory.
export function seedRuns(): Run[] {
  const runA = makeRun({
    id: "run-a",
    title: "Refactor the auth module",
    repo: "acme/api",
    live: true,
    peak: 3,
    drift: 0.31,
    events: [
      makeEvent({ id: "a-1", tool: { n: "read_file", cat: "read_only", canon: "read_file", w: 1 } }),
      makeEvent({
        id: "a-2",
        tool: { n: "edit_file", cat: "mutating", canon: "edit_file", w: 2 },
        kind: "tool.call.function",
        summary: "edit the token verifier",
        risk: 1,
        phase: "edit",
        cls: { canon: "edit_file", cat: "mutating", conf: 0.95 },
      }),
      makeEvent({
        id: "a-3",
        tool: { n: "http_post", cat: "network", canon: "http_post", w: 3 },
        kind: "permission.request",
        summary: "outbound POST to an unrecognized host",
        risk: 2,
        score: 0.71,
        action: "escalate",
        phase: "act",
        // Permission events are classifiable but carry no effect yet → the honest
        // "unclassified" gap slice.
        cls: { canon: "http_post", cat: "", conf: 0.4 },
      }),
      makeEvent({
        id: "a-4",
        tool: { n: "run_shell", cat: "destructive", canon: "run_shell", w: 4 },
        kind: "tool.call.shell",
        summary: "delete the build directory",
        risk: 3,
        score: 0.93,
        action: "deny",
        phase: "act",
        cls: { canon: "run_shell", cat: "destructive", conf: 0.88 },
      }),
      makeLifecycleEvent({ id: "a-5", summary: "user message received", kind: "message.user" }),
      makeLifecycleEvent({ id: "a-6" }),
    ],
    taint: [{ flow: "web content → shell arg", det: "tainted argument reached run_shell", lvl: 2 }],
    trust: [{ who: "github.com", ttl: "30m remaining", lvl: 1 }],
    mcp: [{ srv: "filesystem", msg: "tool surface changed since last run", lvl: 1 }],
    gaps: [{ t: "2026-07-10T09:04:00Z", dropped: 12, reason: "output sink restarted mid-run" }],
  });

  const runB = makeRun({
    id: "run-b",
    title: "Fix the flaky title test",
    repo: "acme/web",
    live: false,
    peak: 0,
    events: [
      makeEvent({ id: "b-1", summary: "read the failing spec" }),
      makeLifecycleEvent({ id: "b-2", kind: "session.started", summary: "session started" }),
    ],
    usage: {
      in: 1_000,
      out: 500,
      cost: null,
      // AIU present but from a single blank-model row → surfaces "unknown model"
      // with unknown ("—") premium / request counts in the Cost table.
      aiuNano: 26_596_400_000,
      premiumRequests: null,
      inputUncached: null,
      cacheRead: null,
      cacheCreation: null,
      requestsTotal: null,
      models: [
        makeModelUsage({
          model: "",
          aiuNano: 26_596_400_000,
          premiumRequests: null,
          requests: null,
          inputUncached: null,
          cacheRead: null,
          cacheCreation: null,
          input: 1_000,
          output: 500,
        }),
      ],
    },
  });

  return [runA, runB];
}

// A fully-classified tool-event fixture for the coverage-denominator regression:
// every classifiable (tool) event carries an effect, plus lifecycle events that
// must be excluded from the denominator. Honest coverage here is 100%.
export function fullyClassifiedEvents(): TEvent[] {
  return [
    makeEvent({ id: "fc-1", cls: { canon: "read_file", cat: "read_only", conf: 0.99 } }),
    makeEvent({
      id: "fc-2",
      tool: { n: "edit_file", cat: "mutating", canon: "edit_file", w: 2 },
      cls: { canon: "edit_file", cat: "mutating", conf: 0.97 },
    }),
    makeEvent({
      id: "fc-3",
      tool: { n: "run_shell", cat: "destructive", canon: "run_shell", w: 4 },
      kind: "tool.call.shell",
      cls: { canon: "run_shell", cat: "destructive", conf: 0.95 },
    }),
    // Lifecycle events: no effect by nature, excluded from the denominator. If they
    // leaked into it the metric would read well below 100%.
    makeLifecycleEvent({ id: "fc-4", kind: "session.started" }),
    makeLifecycleEvent({ id: "fc-5", kind: "turn.completed" }),
    makeLifecycleEvent({ id: "fc-6", kind: "message.assistant" }),
  ];
}

export function makeTranscript(runId: string, turns?: TranscriptTurn[]): Transcript {
  return {
    id: runId,
    turns: turns ?? [
      {
        id: "a-1",
        t: "2026-07-10T09:00:00Z",
        role: "user",
        label: "User",
        kind: "message.user",
        text: "Please refactor the auth module for clarity.",
      },
      {
        id: "a-2",
        t: "2026-07-10T09:01:00Z",
        role: "assistant",
        label: "Assistant",
        kind: "message.assistant",
        text: "Starting by reading the token verifier.",
      },
    ],
  };
}
