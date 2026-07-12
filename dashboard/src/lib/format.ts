import type { RiskLevel, TEvent } from "@/lib/types";
import { RISK } from "@/lib/types";

export const money = (n: number) => "$" + n.toFixed(2);
export const money3 = (n: number) => "$" + n.toFixed(3);

/** Render a run's dollar cost, or "—" when it is unknown. Cost is null when the
 * wire carries no dollars (e.g. GitHub Copilot CLI emits none) — that is *unknown*,
 * not free, so it must never render as "$0.00". A real 0 dollars (should any
 * source ever report it) still shows "$0.00"; only null/undefined becomes "—". */
export const fmtCost = (n: number | null | undefined) =>
  n == null ? "—" : money(n);

/** Render a run's Copilot premium-request count as "N premium request(s)", or ""
 * when unknown (null/undefined — every non-Copilot source). A true 0 is a real
 * "0 premium requests", distinct from unknown. */
export const premiumReq = (n: number | null | undefined) =>
  n == null ? "" : `${n} premium request${n === 1 ? "" : "s"}`;

/** Render a run's AI-Unit (AIU) consumption from integer nano-AIU, or "—" when
 * unknown. AIU ("AI credits") is Copilot's PRIMARY billing signal; it rides the
 * whole pipeline as integer nano-AIU and is divided by 1e9 ONLY here (no float
 * precision loss upstream). Null (every non-Copilot source) renders "—"; a genuine
 * 0 renders "0.0 AIU" (distinct from unknown). Formatted to one decimal with
 * thousands separators, e.g. "66,596.4 AIU". */
export const fmtAiu = (nano: number | null | undefined) =>
  nano == null
    ? "—"
    : (nano / 1e9).toLocaleString("en-US", {
        minimumFractionDigits: 1,
        maximumFractionDigits: 1,
      }) + " AIU";

export const tk = (n: number) => (n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n));

/** Whole-percent share of `n` over `total`, or null when either is unknown (null)
 * or the total is 0 — so an unknown share renders "—", never a fabricated "0%". */
export const pct = (n: number | null | undefined, total: number | null | undefined) =>
  n == null || total == null || total === 0 ? null : Math.round((n / total) * 100);

/** Null-aware running sum: unknown (null/undefined) addends are skipped so they
 * never fabricate a 0, but the moment any real number is seen the accumulator
 * becomes numeric. Stays null only while *everything* seen is unknown — the
 * "genuine 0 differs from unknown" invariant the truth instrument depends on. */
export const nsum = (acc: number | null, x: number | null | undefined) =>
  x == null ? acc : (acc ?? 0) + x;

export const hhmm = (d: Date) => d.toTimeString().slice(0, 8);
export const dmin = (ms: number) =>
  Math.floor(ms / 60000) + "m " + String(Math.floor(ms / 1000) % 60).padStart(2, "0") + "s";
export const rlab = (l: RiskLevel) => RISK[l];
export const riskVar = (l: number) => `var(--risk-${l})`;

/** Render a payload value for display: objects/arrays as pretty JSON, primitives
 * as-is, and null/undefined as an empty string (never "[object Object]"). */
export function fmtVal(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "object") return JSON.stringify(v, null, 2);
  return String(v);
}

export function peakOf(evs: TEvent[]): RiskLevel {
  return Math.max(0, ...evs.map((e) => e.risk)) as RiskLevel;
}

export function dist(evs: TEvent[]): { lvl: number; n: number; pct: number }[] {
  const c = [0, 0, 0, 0];
  evs.forEach((e) => c[e.risk]++);
  const tot = evs.length || 1;
  return c.map((n, i) => ({ lvl: i, n, pct: Math.round((n / tot) * 100) }));
}
