// Builds the Chapters navigator tree for a run: activity ▸ step ▸ events.
//
// The backend ships a flat `run.segs` list of segment nodes
// (`session | activity | step`, each with a `parent`) plus a flat `run.events`
// list where every event carries `e.seg` — the id of the *step* it belongs to
// (occasionally an activity id as a fallback, or "" when it has no segment).
//
// Two things make the grouping subtle, and both are handled here so the view
// stays declarative:
//
//   1. Id collision across kinds. The titler reuses an activity's id for that
//      activity's "lead" step, so an `activity` node and one `step` node can
//      share the same id. Nodes must therefore be identified by (kind, id), not
//      id alone.
//   2. Zero event loss. Every event with a non-empty `seg` must land under
//      exactly one step. Events whose `seg` matches no titled step (a few
//      untitled steps exist) or is empty are routed into synthetic buckets
//      instead of being dropped — the previous implementation grouped by
//      `e.seg === activity.id`, which silently hid ~77% of events.

import type { RiskLevel, Seg, TEvent } from "@/lib/types";

// One event plus its original index in `run.events` (needed for `setSel`, the
// active-row highlight, and the inspector).
export interface ChapterEvent {
  e: TEvent;
  i: number;
}

export interface ChapterStep {
  key: string; // stable React key (unique across the whole tree)
  id: string; // segment id ("" for the no-segment bucket)
  title: string;
  risk: RiskLevel;
  synthetic: boolean; // true for "Untitled step" / "Ungrouped" buckets
  events: ChapterEvent[];
}

export interface ChapterActivity {
  key: string;
  id: string;
  title: string;
  risk: RiskLevel;
  synthetic: boolean; // true for the catch-all "Unassigned" activity
  steps: ChapterStep[];
  count: number; // total events across the activity's steps
}

const UNASSIGNED_ID = "__unassigned__";
const NO_SEGMENT_ID = "__no_segment__";

function maxRisk(events: ChapterEvent[]): RiskLevel {
  let r: RiskLevel = 0;
  for (const { e } of events) if (e.risk > r) r = e.risk;
  return r;
}

/**
 * Group a run's events under their titled step, and steps under their activity.
 *
 * All real activities are returned (even those with no events) to preserve the
 * existing "one row per activity" overview; empty *steps* are omitted to keep
 * the tree readable, and a trailing synthetic "Unassigned" activity is appended
 * only when some event could not be placed under a real step.
 */
export function buildChapters(segs: Seg[], events: TEvent[]): ChapterActivity[] {
  const activitySegs = segs.filter((s) => s.kind === "activity");
  const stepSegs = segs.filter((s) => s.kind === "step");

  // Real activity nodes, in segment order. First wins on the (unexpected) case
  // of a duplicate activity id.
  const activityById = new Map<string, ChapterActivity>();
  const activities: ChapterActivity[] = [];
  for (const a of activitySegs) {
    if (activityById.has(a.id)) continue;
    const node: ChapterActivity = {
      key: `act:${a.id}`,
      id: a.id,
      title: a.title,
      risk: a.risk,
      synthetic: false,
      steps: [],
      count: 0,
    };
    activityById.set(a.id, node);
    activities.push(node);
  }

  // Lazily-created catch-all activity for events we cannot attribute to a real
  // activity (unknown/empty seg, or a step whose parent activity is missing).
  let unassigned: ChapterActivity | null = null;
  const unassignedActivity = (): ChapterActivity => {
    if (!unassigned) {
      unassigned = {
        key: `act:${UNASSIGNED_ID}`,
        id: UNASSIGNED_ID,
        title: "Unassigned",
        risk: 0,
        synthetic: true,
        steps: [],
        count: 0,
      };
      activities.push(unassigned);
    }
    return unassigned;
  };

  // Real step nodes keyed by step id (first wins on cross-activity id reuse).
  // A step whose id equals its parent activity id (the "lead" step) is still a
  // distinct node here and nests under that activity.
  const stepById = new Map<string, ChapterStep>();
  for (const s of stepSegs) {
    if (stepById.has(s.id)) continue;
    const step: ChapterStep = {
      key: `step:${s.parent ?? ""}:${s.id}`,
      id: s.id,
      title: s.title,
      risk: s.risk,
      synthetic: false,
      events: [],
    };
    stepById.set(s.id, step);
    const parent = s.parent ? activityById.get(s.parent) : undefined;
    (parent ?? unassignedActivity()).steps.push(step);
  }

  // Per-activity synthetic "Ungrouped" step for events whose seg is that
  // activity's id but has no matching titled step.
  const ungroupedByActivity = new Map<string, ChapterStep>();
  const ungroupedStep = (activity: ChapterActivity): ChapterStep => {
    let step = ungroupedByActivity.get(activity.id);
    if (!step) {
      step = {
        key: `step:${activity.id}:${UNASSIGNED_ID}`,
        id: `${activity.id}:${UNASSIGNED_ID}`,
        title: "Ungrouped events",
        risk: 0,
        synthetic: true,
        events: [],
      };
      ungroupedByActivity.set(activity.id, step);
      activity.steps.push(step);
    }
    return step;
  };

  // Synthetic "Untitled step" nodes (one per unknown seg id) and a single
  // "No segment" bucket, all hung off the catch-all activity.
  const untitledBySeg = new Map<string, ChapterStep>();
  const untitledStep = (seg: string): ChapterStep => {
    let step = untitledBySeg.get(seg);
    if (!step) {
      step = {
        key: `step:${UNASSIGNED_ID}:${seg}`,
        id: seg,
        title: `Untitled step · ${seg.slice(0, 8)}`,
        risk: 0,
        synthetic: true,
        events: [],
      };
      untitledBySeg.set(seg, step);
      unassignedActivity().steps.push(step);
    }
    return step;
  };
  let noSegment: ChapterStep | null = null;
  const noSegmentStep = (): ChapterStep => {
    if (!noSegment) {
      noSegment = {
        key: `step:${UNASSIGNED_ID}:${NO_SEGMENT_ID}`,
        id: NO_SEGMENT_ID,
        title: "Ungrouped (no segment)",
        risk: 0,
        synthetic: true,
        events: [],
      };
      unassignedActivity().steps.push(noSegment);
    }
    return noSegment;
  };

  for (let i = 0; i < events.length; i++) {
    const e = events[i];
    const seg = e.seg;
    let target: ChapterStep;
    if (seg && stepById.has(seg)) {
      target = stepById.get(seg)!;
    } else if (seg && activityById.has(seg)) {
      target = ungroupedStep(activityById.get(seg)!);
    } else if (seg) {
      target = untitledStep(seg);
    } else {
      target = noSegmentStep();
    }
    target.events.push({ e, i });
  }

  // Drop steps with no events, finalize synthetic risk + counts.
  for (const activity of activities) {
    activity.steps = activity.steps.filter((s) => s.events.length > 0);
    for (const step of activity.steps) {
      if (step.synthetic) step.risk = maxRisk(step.events);
    }
    activity.count = activity.steps.reduce((n, s) => n + s.events.length, 0);
    if (activity.synthetic) {
      activity.risk = activity.steps.reduce<RiskLevel>((r, s) => (s.risk > r ? s.risk : r), 0);
    }
  }

  return activities;
}

/**
 * Locate the node keys for the event at `index`, so the view can highlight and
 * auto-expand the activity/step that owns the currently-inspected event.
 */
export function locateEvent(
  chapters: ChapterActivity[],
  index: number,
): { activityKey: string; stepKey: string } | null {
  for (const activity of chapters) {
    for (const step of activity.steps) {
      if (step.events.some((ce) => ce.i === index)) {
        return { activityKey: activity.key, stepKey: step.key };
      }
    }
  }
  return null;
}
