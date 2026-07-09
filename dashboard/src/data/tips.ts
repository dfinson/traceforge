// Glossary + tooltip copy, ported from the lean-v2 mockup. Each string is a
// "Title|Body" pair; the <Tip> component splits on the first pipe.

export const RTIP: string[] = [
  "Risk 0 · safe|Read-only or well-scoped. No risk predicates matched.",
  "Risk 1 · caution|Mutating but low-impact — formatting, config, scoped edits. Worth a glance.",
  "Risk 2 · danger|Destructive-looking or boundary-crossing — new file outside the allowlist, shell exec, network egress. Review recommended.",
  "Risk 3 · critical|Recursive delete, credential access, or exfiltration pattern. Block recommended.",
];

export const ATIP: Record<string, string> = {
  allow: "Verdict · allow|Proceed — no intervention.",
  warn: "Verdict · warn|Proceed, but surface a note to the human.",
  escalate: "Verdict · escalate|Pause for human/agent review before proceeding.",
  deny: "Verdict · deny|Block the action. Governance is fail-closed.",
  transform: "Verdict · transform|Rewrite the action into a safer form before it runs.",
};

export const DIMTIP: Record<string, string> = {
  phase: "Dimension · phase|Cost grouped by workflow phase (exploring → implementing → debugging → testing → reviewing).",
  tool: "Dimension · tool|Cost grouped by the tool invoked — Bash, Edit, WebFetch and so on.",
  file: "Dimension · file|Cost grouped by the file the action touched.",
  segment: "Dimension · segment|Cost grouped by titler segment — a chapter of the run.",
  turn: "Dimension · turn|Cost grouped by conversation turn.",
  retry: "Dimension · retry|First-try vs retried calls — surfaces wasted spend.",
};

export const G = {
  session_id:
    "Grouped by session_id|The stable id every event carries. TraceForge groups a run by it — no external run-tracking needed.",
  identity:
    "Identity ← system.db|repo + agent·model are cross-session memory (system.db → session_summaries). On the SDK-embed path with no db_path they read 'unknown'.",
  enriched_events:
    "enriched_events|Output-sink table — one row per tool event, with risk, action, cost and duration hoisted into columns plus the full governance metadata_json.",
  segment_titles:
    "segment_titles|A 3-level tree (session ▸ activity ▸ step) named by the heuristic titler. Drives the Chapters pane.",
  context_gaps:
    "context_gaps|Recorded breaks in the trace — truncated output, missing tool-result pairing, sub-agent boundary — so you can see where coverage is incomplete.",
  usage_records: "usage_records|Per-call token and cost rows: input/output tokens, model, dollars.",
  attribution_rollups:
    "attribution_rollups|Cost/latency rolled up per dimension. Global / last-write (no session_id) — so per-run cost is reconstructed from usage_records + spans.",
  spans: "spans|Timing spans per step — the source for duration and latency attribution.",
  gov_meta:
    "metadata.governance|The governance SessionMeta stamped on every event: classification, risk, recommendation, budget, drift, MCP alerts, evidence.",
  sysdb:
    "system.db memory|Cross-session governance store: identity, taint ledger, trust grants, drift baselines, MCP registry, budget counters.",
} as const;

export const mtip = (m: [string, string]) =>
  `MITRE ATT&CK ${m[0]}|${m[1]} — matched by the evidence chain and factored into the risk score.`;

export const PRED_TIP =
  "Risk predicate|A rule the risk engine matched against this payload — a building block of the evidence chain.";
