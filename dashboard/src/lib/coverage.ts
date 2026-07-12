// Honest scoping for the classification-"Coverage" metric.
//
// `cls.cat` is the EFFECT taxonomy (`read_only` / `mutating` / `destructive`).
// An effect is a TOOL-ACTION property, so only tool-call and permission events
// can carry one. Measuring "classified vs unclassified" against ALL events is a
// framing bug: it drags in events that have no effect BY NATURE and makes the
// classifier look broken when it is not.
//
//   * lifecycle events (session / turn / message / llm markers) never have an
//     effect;
//   * hook wrapper events (`hook.started` / `hook.completed`) DUPLICATE the
//     already-classified `tool.call.*` events, so classifying them would
//     double-count and counting them as "unclassified" is pure noise.
//
// So the coverage DENOMINATOR is scoped to *classifiable* events (tool calls +
// permission requests). Lifecycle and hook events get their own labelled
// buckets — visible, but never masquerading as classification failures. Events
// that are classifiable yet still carry no effect (shell commands, whose effect
// is statically indeterminate today; `permission.*`, until governance
// classification lands) stay counted as a REAL, honest gap — the dashboard must
// not paper over pipeline deficiencies in either direction.

import type { TEvent } from "@/lib/types";

const isToolCall = (kind: string): boolean => kind.startsWith("tool.call.");
const isPermission = (kind: string): boolean => kind.startsWith("permission.");
const isHook = (kind: string): boolean => kind.startsWith("hook.");

// An effect only applies to tool actions. Permission events are included even
// though they carry no effect YET — a sibling change adds governance
// classification to them, so counting them now keeps the metric honest before
// (a visible gap) and after (they flip to classified) that lands. Any event the
// classifier actually resolved (non-empty effect) is classifiable by
// definition, so it is never hidden away in a non-tool bucket.
export function isClassifiable(e: TEvent): boolean {
  return isToolCall(e.kind) || isPermission(e.kind) || Boolean(e.cls?.cat);
}

export type CoverageBucket = "classified" | "unclassified" | "hook" | "lifecycle";

// Which honest bucket an event belongs to. `classified` / `unclassified` are the
// two halves of the classifiable denominator; `hook` and `lifecycle` are out of
// scope for classification but kept visible.
export function coverageBucket(e: TEvent): CoverageBucket {
  if (e.cls?.cat) return "classified";
  if (isToolCall(e.kind) || isPermission(e.kind)) return "unclassified";
  if (isHook(e.kind)) return "hook";
  return "lifecycle";
}

export interface CoverageStats {
  classified: number; // classifiable events the classifier resolved (effect set)
  unclassified: number; // classifiable events with no effect yet (the real gap)
  hook: number; // hook wrappers (duplicate the tool.call.* events)
  lifecycle: number; // non-tool events (session / turn / message / llm markers)
  classifiable: number; // classified + unclassified — the honest denominator
  total: number;
  pct: number; // classified / classifiable, 0..100 (0 when nothing is classifiable)
}

export function coverageStats(events: TEvent[]): CoverageStats {
  let classified = 0;
  let unclassified = 0;
  let hook = 0;
  let lifecycle = 0;
  for (const e of events) {
    switch (coverageBucket(e)) {
      case "classified":
        classified++;
        break;
      case "unclassified":
        unclassified++;
        break;
      case "hook":
        hook++;
        break;
      case "lifecycle":
        lifecycle++;
        break;
    }
  }
  const classifiable = classified + unclassified;
  return {
    classified,
    unclassified,
    hook,
    lifecycle,
    classifiable,
    total: events.length,
    pct: classifiable ? Math.round((classified / classifiable) * 100) : 0,
  };
}

// Effect-category breakdown over CLASSIFIABLE events only: each real effect
// (`read_only` / `mutating` / `destructive`) plus a single honest
// `"unclassified"` slice for classifiable events the classifier has not
// resolved. Lifecycle and hook events are excluded — they get their own
// breakdown so the classifier is never blamed for events it is not meant to
// touch. Sorted by count, descending.
export function effectMix(events: TEvent[]): { label: string; value: number }[] {
  const cats: Record<string, number> = {};
  for (const e of events) {
    const bucket = coverageBucket(e);
    if (bucket === "classified") {
      const cat = e.cls?.cat;
      if (cat) cats[cat] = (cats[cat] || 0) + 1;
    } else if (bucket === "unclassified") {
      cats.unclassified = (cats.unclassified || 0) + 1;
    }
  }
  return Object.entries(cats)
    .map(([label, value]) => ({ label, value }))
    .sort((a, b) => b.value - a.value);
}

// Honest "event scope" split for a distribution bar: how the full event volume
// divides into the classifiable denominator vs. the two out-of-scope buckets.
// Zero-count buckets are dropped so the bar only shows what is present.
export function scopeSplit(
  stats: CoverageStats,
  colors: { classifiable: string; hook: string; lifecycle: string },
): { label: string; value: number; color: string }[] {
  return [
    { label: "classifiable", value: stats.classifiable, color: colors.classifiable },
    { label: "hook wrappers", value: stats.hook, color: colors.hook },
    { label: "non-tool / lifecycle", value: stats.lifecycle, color: colors.lifecycle },
  ].filter((s) => s.value > 0);
}
