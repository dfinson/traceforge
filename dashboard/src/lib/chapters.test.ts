import { describe, expect, it } from "vitest";
import { buildChapters, locateEvent } from "@/lib/chapters";
import type { ChapterActivity } from "@/lib/chapters";
import type { RiskLevel, Seg, TEvent } from "@/lib/types";

// --- Fixtures --------------------------------------------------------------
// buildChapters only reads a segment's (id, kind, parent, title, risk) and an
// event's (seg, risk); the factories fill everything else with inert defaults
// so the seeded runs stay focused on the fields that actually drive the tree.

function mkSeg(
  id: string,
  kind: Seg["kind"],
  parent: string | null,
  title: string,
  risk: RiskLevel = 0,
): Seg {
  return { id, kind, parent, title, risk };
}

function mkEvent(opts: { id: string; seg: string; tool?: string; risk?: RiskLevel }): TEvent {
  const toolName = opts.tool ?? "view";
  return {
    id: opts.id,
    t: new Date("2026-01-01T00:00:00Z"),
    tool: { n: toolName, cat: "fs", canon: toolName, w: 1 },
    kind: "tool",
    summary: "",
    risk: opts.risk ?? 0,
    score: 0,
    action: "allow",
    cost: null,
    tokens: 0,
    dur: 0,
    phase: "",
    seg: opts.seg,
    file: "",
    turn: "",
    retry: false,
    cls: { canon: "", cat: "", conf: 0 },
    ev: null,
    reco: { action: "allow", why: "" },
    gap: null,
    payload: {},
  };
}

// A realistic titled run: two activities, each with titled steps. Crucially,
// every event's `seg` holds a STEP id (the lead step even reuses its activity's
// id — the documented titler id collision), never a bare activity id. That is
// the exact shape that broke the old `e.seg === activity.id` grouping and hid
// ~77% of events; here five of six events carry a seg that is a step id but not
// an activity id, so the buggy filter would drop them.
function runSegs(): Seg[] {
  return [
    mkSeg("sess", "session", null, "Debug the flaky suite", 1),
    mkSeg("act-investigate", "activity", "sess", "Investigate failing tests", 1),
    mkSeg("act-implement", "activity", "sess", "Implement the fix", 2),
    // Lead step reuses the activity id (kind,id) collision.
    mkSeg("act-investigate", "step", "act-investigate", "Survey the test suite", 1),
    mkSeg("step-read", "step", "act-investigate", "Read project files", 1),
    mkSeg("step-edit", "step", "act-implement", "Edit the chapters builder", 2),
    mkSeg("step-verify", "step", "act-implement", "Run the verification build", 3),
  ];
}

function runEvents(): TEvent[] {
  return [
    mkEvent({ id: "e0", seg: "act-investigate", tool: "view", risk: 1 }), // lead step (id collision)
    mkEvent({ id: "e1", seg: "step-read", tool: "view" }),
    mkEvent({ id: "e2", seg: "step-read", tool: "edit", risk: 1 }),
    mkEvent({ id: "e3", seg: "step-edit", tool: "edit", risk: 2 }),
    mkEvent({ id: "e4", seg: "step-edit", tool: "powershell", risk: 1 }),
    mkEvent({ id: "e5", seg: "step-verify", tool: "powershell", risk: 3 }),
  ];
}

function allEventIndices(chapters: ChapterActivity[]): number[] {
  return chapters.flatMap((a) => a.steps.flatMap((s) => s.events.map((ce) => ce.i)));
}

function allStepTitles(chapters: ChapterActivity[]): string[] {
  return chapters.flatMap((a) => a.steps.map((s) => s.title));
}

// --- AC (a): zero event loss ----------------------------------------------

describe("buildChapters — zero event loss (issue #194 regression guard)", () => {
  it("places every event under a step, dropping none — even when seg is a step id, not an activity id", () => {
    const events = runEvents();
    const chapters = buildChapters(runSegs(), events);

    // Every original event index appears exactly once across the whole tree.
    const indices = allEventIndices(chapters).sort((a, b) => a - b);
    expect(indices).toEqual(events.map((_, i) => i));

    // Total placed === total input (the guard against the ~77% drop).
    const placed = chapters.reduce((n, a) => n + a.count, 0);
    expect(placed).toBe(events.length);
  });

  it("keeps events whose seg is a non-activity step id (the exact old-bug shape)", () => {
    const chapters = buildChapters(runSegs(), runEvents());
    // e1/e2 carry seg 'step-read', which is NOT any activity id. The old
    // `e.seg === activity.id` filter dropped these; they must be present now.
    expect(locateEvent(chapters, 1)).not.toBeNull();
    expect(locateEvent(chapters, 2)).not.toBeNull();
  });
});

// --- AC (b): activity ▸ step ▸ event nesting + step titles -----------------

describe("buildChapters — activity ▸ step ▸ event nesting", () => {
  it("nests steps under their owning activity with the right events, in segment order", () => {
    const chapters = buildChapters(runSegs(), runEvents());
    expect(chapters).toHaveLength(2);

    const [investigate, implement] = chapters;
    expect(investigate).toMatchObject({
      key: "act:act-investigate",
      id: "act-investigate",
      title: "Investigate failing tests",
      synthetic: false,
      count: 3,
    });
    expect(implement).toMatchObject({
      key: "act:act-implement",
      id: "act-implement",
      title: "Implement the fix",
      synthetic: false,
      count: 3,
    });

    expect(investigate.steps.map((s) => s.title)).toEqual([
      "Survey the test suite",
      "Read project files",
    ]);
    expect(implement.steps.map((s) => s.title)).toEqual([
      "Edit the chapters builder",
      "Run the verification build",
    ]);

    // Events sit under the correct step, addressed by their original index.
    const read = investigate.steps[1];
    expect(read.id).toBe("step-read");
    expect(read.events.map((ce) => ce.i)).toEqual([1, 2]);

    const edit = implement.steps[0];
    expect(edit.id).toBe("step-edit");
    expect(edit.events.map((ce) => ce.i)).toEqual([3, 4]);
  });

  it("titles step nodes with the multi-word segment title, never the single-word tool verb", () => {
    const chapters = buildChapters(runSegs(), runEvents());
    const titles = allStepTitles(chapters);

    // Every event uses a view/edit/powershell tool; no step may be labelled with
    // the verb instead of its segment title.
    for (const verb of ["view", "edit", "powershell"]) {
      expect(titles).not.toContain(verb);
    }

    // The step whose events are edit + powershell is titled from its segment.
    const editStep = chapters[1].steps[0];
    expect(editStep.title).toBe("Edit the chapters builder");
    expect(editStep.title.split(" ").length).toBeGreaterThan(1);
  });

  it("keeps the lead step whose id collides with its activity id as a distinct node", () => {
    const chapters = buildChapters(runSegs(), runEvents());
    const investigate = chapters[0];
    const lead = investigate.steps[0];

    // Activity and lead step share the id but are separate (kind, id) nodes.
    expect(investigate.id).toBe("act-investigate");
    expect(lead.id).toBe("act-investigate");
    expect(investigate.key).toBe("act:act-investigate");
    expect(lead.key).toBe("step:act-investigate:act-investigate");
    expect(lead.title).toBe("Survey the test suite");
    expect(lead.events.map((ce) => ce.i)).toEqual([0]);
  });
});

// --- AC (a) cont.: unplaceable events are bucketed, not dropped ------------

describe("buildChapters — unplaceable events are bucketed, not dropped", () => {
  it("routes an event whose seg is an activity id with no matching step into a synthetic 'Ungrouped events' step", () => {
    const segs = [
      mkSeg("a", "activity", null, "Lonely activity", 0),
      mkSeg("s", "step", "a", "A real step", 0),
    ];
    // seg 'a' is an activity id, and there is NO step with id 'a'.
    const chapters = buildChapters(segs, [mkEvent({ id: "x", seg: "a", risk: 2 })]);

    expect(chapters).toHaveLength(1);
    const activity = chapters[0];
    expect(activity.id).toBe("a");
    expect(activity.synthetic).toBe(false);

    // The real step 's' had no events → dropped; only the synthetic bucket remains.
    expect(activity.steps).toHaveLength(1);
    const bucket = activity.steps[0];
    expect(bucket.synthetic).toBe(true);
    expect(bucket.title).toBe("Ungrouped events");
    expect(bucket.events.map((ce) => ce.i)).toEqual([0]);
    expect(bucket.risk).toBe(2); // synthetic risk rolls up from its events
    expect(activity.count).toBe(1);
  });

  it("routes an event with an unknown seg into a synthetic 'Untitled step' under the catch-all activity", () => {
    const segs = [mkSeg("a", "activity", null, "An activity", 0)];
    const chapters = buildChapters(segs, [mkEvent({ id: "x", seg: "mystery-seg-id", risk: 1 })]);

    const unassigned = chapters.find((c) => c.synthetic);
    expect(unassigned?.title).toBe("Unassigned");
    const step = unassigned?.steps[0];
    expect(step?.synthetic).toBe(true);
    expect(step?.id).toBe("mystery-seg-id");
    expect(step?.title.startsWith("Untitled step")).toBe(true);
    expect(step?.events.map((ce) => ce.i)).toEqual([0]);
  });

  it("routes an event with an empty seg into a synthetic 'no segment' bucket", () => {
    const chapters = buildChapters([], [mkEvent({ id: "x", seg: "" })]);

    expect(chapters).toHaveLength(1);
    const unassigned = chapters[0];
    expect(unassigned.synthetic).toBe(true);
    const bucket = unassigned.steps[0];
    expect(bucket.synthetic).toBe(true);
    expect(bucket.title).toBe("Ungrouped (no segment)");
    expect(bucket.events.map((ce) => ce.i)).toEqual([0]);
  });
});

// --- AC (d): empty / titler-disabled inputs -------------------------------

describe("buildChapters — empty and titler-disabled inputs", () => {
  it("returns no chapters for empty input", () => {
    expect(buildChapters([], [])).toEqual([]);
  });

  it("returns no chapters when the titler is disabled (no segment_titles) and there are no events", () => {
    // --no-titles ⇒ the run carries no activity/step segments (only a session).
    const segs = [mkSeg("sess", "session", null, "A session", 0)];
    expect(buildChapters(segs, [])).toEqual([]);
  });

  it("does not crash when the titler is disabled but events still exist, and keeps every event", () => {
    // With no titled steps, un-segmented events (seg '') are preserved in a
    // synthetic bucket rather than dropped — the zero-loss guarantee holds even
    // with titling off.
    const events = [mkEvent({ id: "e0", seg: "" }), mkEvent({ id: "e1", seg: "" })];
    const chapters = buildChapters([], events);

    const placed = chapters.reduce((n, a) => n + a.count, 0);
    expect(placed).toBe(events.length);
    // Nothing titled: every activity produced is synthetic.
    expect(chapters.every((a) => a.synthetic)).toBe(true);
  });
});

// --- AC (c): locateEvent ---------------------------------------------------

describe("locateEvent", () => {
  it("returns the owning activity and step keys for a known event", () => {
    const chapters = buildChapters(runSegs(), runEvents());

    // e3 (index 3) has seg 'step-edit' under activity 'act-implement'.
    const loc = locateEvent(chapters, 3);
    expect(loc).toEqual({
      activityKey: "act:act-implement",
      stepKey: "step:act-implement:step-edit",
    });

    // Cross-check: the located step actually owns that index.
    const activity = chapters.find((a) => a.key === loc?.activityKey);
    const step = activity?.steps.find((s) => s.key === loc?.stepKey);
    expect(step?.events.some((ce) => ce.i === 3)).toBe(true);
  });

  it("locates the lead-step event whose seg id collides with the activity id", () => {
    const chapters = buildChapters(runSegs(), runEvents());
    expect(locateEvent(chapters, 0)).toEqual({
      activityKey: "act:act-investigate",
      stepKey: "step:act-investigate:act-investigate",
    });
  });

  it("returns null for an out-of-range index (not found handled gracefully)", () => {
    const chapters = buildChapters(runSegs(), runEvents());
    expect(locateEvent(chapters, 999)).toBeNull();
    expect(locateEvent(chapters, -1)).toBeNull();
  });

  it("returns null when there are no chapters", () => {
    expect(locateEvent([], 0)).toBeNull();
  });
});
