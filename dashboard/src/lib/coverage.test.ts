import { describe, expect, it } from "vitest";
import {
  coverageBucket,
  coverageStats,
  effectMix,
  isClassifiable,
} from "@/lib/coverage";
import { fullyClassifiedEvents, makeEvent, makeLifecycleEvent } from "@/test/fixtures";

describe("coverage buckets", () => {
  it("classifies a tool call with an effect as classified", () => {
    const e = makeEvent({ kind: "tool.call.function", cls: { canon: "x", cat: "read_only", conf: 0.9 } });
    expect(coverageBucket(e)).toBe("classified");
    expect(isClassifiable(e)).toBe(true);
  });

  it("counts an effect-less tool/permission event as an honest unclassified gap", () => {
    const perm = makeEvent({ kind: "permission.request", cls: { canon: "", cat: "", conf: 0.3 } });
    expect(coverageBucket(perm)).toBe("unclassified");
    expect(isClassifiable(perm)).toBe(true);
  });

  it("routes hook wrappers and lifecycle events out of the denominator", () => {
    const hook = makeEvent({ kind: "hook.completed", cls: { canon: "", cat: "", conf: 0 } });
    const lifecycle = makeLifecycleEvent();
    expect(coverageBucket(hook)).toBe("hook");
    expect(coverageBucket(lifecycle)).toBe("lifecycle");
    expect(isClassifiable(hook)).toBe(false);
    expect(isClassifiable(lifecycle)).toBe(false);
  });
});

describe("coverageStats denominator", () => {
  // Regression guard for the `unclassified-coverage-framing` diagnostic: the
  // denominator must be classifiable (tool + permission) events only. Scoring
  // against ALL events — the lifecycle turns/messages included below — made a
  // fully-classified pipeline read ~50% instead of the true 100%.
  it("scores a fully-classified tool fixture at 100%", () => {
    const stats = coverageStats(fullyClassifiedEvents());
    expect(stats.classified).toBe(3);
    expect(stats.unclassified).toBe(0);
    expect(stats.classifiable).toBe(3);
    expect(stats.lifecycle).toBe(3);
    expect(stats.total).toBe(6);
    expect(stats.pct).toBe(100);
  });

  it("excludes lifecycle events from the denominator entirely", () => {
    // 1 classified tool event among 4 lifecycle events: dividing by all events
    // would give 20%; the honest denominator gives 100%.
    const events = [
      makeEvent({ cls: { canon: "read_file", cat: "read_only", conf: 0.99 } }),
      makeLifecycleEvent(),
      makeLifecycleEvent(),
      makeLifecycleEvent(),
      makeLifecycleEvent(),
    ];
    const stats = coverageStats(events);
    expect(stats.classifiable).toBe(1);
    expect(stats.pct).toBe(100);
  });

  it("keeps effect-less classifiable events as a real gap", () => {
    const events = [
      makeEvent({ cls: { canon: "read_file", cat: "read_only", conf: 0.99 } }),
      makeEvent({ kind: "permission.request", cls: { canon: "", cat: "", conf: 0.2 } }),
    ];
    const stats = coverageStats(events);
    expect(stats.classifiable).toBe(2);
    expect(stats.unclassified).toBe(1);
    expect(stats.pct).toBe(50);
  });

  it("reports 0% (not NaN) when nothing is classifiable", () => {
    const stats = coverageStats([makeLifecycleEvent(), makeLifecycleEvent()]);
    expect(stats.classifiable).toBe(0);
    expect(stats.pct).toBe(0);
  });
});

describe("effectMix", () => {
  it("breaks down classified events by effect, ignoring lifecycle noise", () => {
    const mix = effectMix(fullyClassifiedEvents());
    const labels = mix.map((m) => m.label).sort();
    expect(labels).toEqual(["destructive", "mutating", "read_only"]);
    expect(mix.every((m) => m.value === 1)).toBe(true);
  });
});
