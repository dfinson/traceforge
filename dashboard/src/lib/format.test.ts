import { describe, expect, it } from "vitest";

import { fmtAiu, fmtCost, money, money3, nsum, pct, premiumReq, tk } from "@/lib/format";

// Unit tests locking the dashboard's money / AIU / token formatters (issue #195).
//
// Honesty note on the `cost-zero-papering` defect: the diagnostic that spawned this
// issue observed `money(None)` rendering "$0.00" — papering *unknown* over as *free*.
// The codebase has since split that responsibility (commit #189): `money` is now the
// PURE dollar formatter (a building block that only ever sees real numbers), and the
// null-honest cost renderer is `fmtCost`, which is the single entry point every view
// (Fleet.tsx, RunView.tsx) uses to render a possibly-unknown cost. So the regression
// guard for "unknown must never render as $0.00" is asserted on `fmtCost` below — that
// is where the honesty contract actually lives. `money` is deliberately left null-unaware
// (its type forbids null) so the null check is not duplicated across two functions.

// Em dash (U+2014) — the sentinel every honest formatter emits for an unknown value.
const DASH = "\u2014";

describe("money", () => {
  it("formats a real zero as an honest free $0.00", () => {
    expect(money(0)).toBe("$0.00");
  });

  it("formats positive values to exactly two decimals", () => {
    expect(money(1)).toBe("$1.00");
    expect(money(1.5)).toBe("$1.50");
    expect(money(12.99)).toBe("$12.99");
    expect(money(1234.5)).toBe("$1234.50");
  });

  it("rounds to two decimals", () => {
    expect(money(1.239)).toBe("$1.24");
    expect(money(1.234)).toBe("$1.23");
  });

  it("does not insert thousands separators (distinct from AIU)", () => {
    expect(money(1234567.5)).toBe("$1234567.50");
  });
});

describe("money3", () => {
  it("formats to three decimals, real zero included", () => {
    expect(money3(0)).toBe("$0.000");
    expect(money3(0.125)).toBe("$0.125");
  });

  it("rounds to three decimals", () => {
    expect(money3(0.1236)).toBe("$0.124");
  });
});

describe("fmtCost (cost-zero-papering regression guard)", () => {
  it("renders unknown cost as an em dash, NOT $0.00", () => {
    // The core honesty invariant: null/undefined is *unknown*, never *free*.
    expect(fmtCost(null)).toBe(DASH);
    expect(fmtCost(undefined)).toBe(DASH);
    // Explicitly assert it never papers unknown over as a fabricated zero dollars.
    expect(fmtCost(null)).not.toBe("$0.00");
  });

  it("renders a genuine zero as $0.00 (a real, legitimately free cost)", () => {
    expect(fmtCost(0)).toBe("$0.00");
  });

  it("renders known positive costs to two decimals", () => {
    expect(fmtCost(1)).toBe("$1.00");
    expect(fmtCost(1.5)).toBe("$1.50");
    expect(fmtCost(12.99)).toBe("$12.99");
    expect(fmtCost(1234.5)).toBe("$1234.50");
  });
});

describe("fmtAiu", () => {
  // AIU ("AI credits") is Copilot's PRIMARY billing signal. It rides the pipeline as
  // integer nano-AIU and is divided by 1e9 ONLY here, formatted to one decimal with
  // thousands separators.
  it("renders unknown consumption as an em dash", () => {
    expect(fmtAiu(null)).toBe(DASH);
    expect(fmtAiu(undefined)).toBe(DASH);
  });

  it("renders a genuine zero as 0.0 AIU (distinct from unknown)", () => {
    expect(fmtAiu(0)).toBe("0.0 AIU");
  });

  it("divides nano-AIU by 1e9", () => {
    expect(fmtAiu(1_000_000_000)).toBe("1.0 AIU");
    expect(fmtAiu(1_500_000_000)).toBe("1.5 AIU");
    expect(fmtAiu(500_000_000)).toBe("0.5 AIU");
  });

  it("rounds to one decimal", () => {
    expect(fmtAiu(1_260_000_000)).toBe("1.3 AIU");
    expect(fmtAiu(1_240_000_000)).toBe("1.2 AIU");
  });

  it("adds thousands separators on large values", () => {
    // Matches the "66,596.4 AIU" example documented on fmtAiu.
    expect(fmtAiu(66_596_400_000_000)).toBe("66,596.4 AIU");
    expect(fmtAiu(12_345_678_900_000)).toBe("12,345.7 AIU");
  });
});

describe("tk (token counts)", () => {
  // `tk` is a pure formatter typed `(n: number)`; unknown token classes are guarded by
  // the caller (`x == null ? "—" : tk(x)` in Cost.tsx), so null never reaches `tk`. The
  // null-aware side of the "unknown vs genuine 0" contract is covered by fmtCost / fmtAiu
  // / pct / nsum; here we lock `tk`'s numeric behaviour, real zero included.
  it("passes values below 1000 through verbatim, real zero included", () => {
    expect(tk(0)).toBe("0");
    expect(tk(999)).toBe("999");
  });

  it("abbreviates values of 1000+ to one-decimal k", () => {
    expect(tk(1000)).toBe("1.0k");
    expect(tk(1500)).toBe("1.5k");
    expect(tk(1234)).toBe("1.2k");
  });

  it("rounds the k abbreviation to one decimal", () => {
    expect(tk(9999)).toBe("10.0k");
  });

  it("only abbreviates to k (never M) — large values stay in thousands", () => {
    expect(tk(1_000_000)).toBe("1000.0k");
  });
});

describe("premiumReq", () => {
  it("renders unknown counts as an empty string", () => {
    expect(premiumReq(null)).toBe("");
    expect(premiumReq(undefined)).toBe("");
  });

  it("renders a genuine zero as a real 0 premium requests", () => {
    expect(premiumReq(0)).toBe("0 premium requests");
  });

  it("pluralises the noun by count", () => {
    expect(premiumReq(1)).toBe("1 premium request");
    expect(premiumReq(2)).toBe("2 premium requests");
    expect(premiumReq(10)).toBe("10 premium requests");
  });
});

describe("pct", () => {
  it("returns null (never a fabricated 0%) when either operand is unknown or total is 0", () => {
    expect(pct(null, 100)).toBeNull();
    expect(pct(50, null)).toBeNull();
    expect(pct(null, null)).toBeNull();
    expect(pct(50, 0)).toBeNull();
  });

  it("renders a genuine 0 share as 0, distinct from unknown", () => {
    expect(pct(0, 100)).toBe(0);
  });

  it("computes a whole-percent share, rounded", () => {
    expect(pct(1, 4)).toBe(25);
    expect(pct(1, 3)).toBe(33);
    expect(pct(2, 3)).toBe(67);
    expect(pct(100, 100)).toBe(100);
  });
});

describe("nsum (null-aware running sum)", () => {
  it("stays null only while everything seen is unknown", () => {
    expect(nsum(null, null)).toBeNull();
    expect(nsum(null, undefined)).toBeNull();
  });

  it("skips unknown addends without fabricating a 0", () => {
    expect(nsum(5, null)).toBe(5);
    expect(nsum(5, undefined)).toBe(5);
  });

  it("becomes numeric the moment a real number is seen — a genuine 0 counts", () => {
    expect(nsum(null, 0)).toBe(0);
    expect(nsum(null, 5)).toBe(5);
    expect(nsum(5, 3)).toBe(8);
  });
});
