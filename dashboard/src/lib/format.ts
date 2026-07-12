import type { RiskLevel, TEvent } from "@/lib/types";
import { RISK } from "@/lib/types";

export type Dim = "phase" | "tool" | "file" | "segment" | "turn" | "retry";

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

export const tk = (n: number) => (n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n));
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

export function agg(evs: TEvent[], dim: Dim): { k: string; v: number }[] {
  const m: Record<string, number> = {};
  evs.forEach((e) => {
    const k =
      dim === "phase"
        ? e.phase
        : dim === "tool"
          ? e.tool.n
          : dim === "file"
            ? e.file
            : dim === "segment"
              ? e.seg
              : dim === "turn"
                ? e.turn
                : e.retry
                  ? "retry"
                  : "first-try";
    m[k] = (m[k] || 0) + e.cost;
  });
  return Object.entries(m)
    .map(([k, v]) => ({ k, v }))
    .sort((a, b) => b.v - a.v);
}

export function dist(evs: TEvent[]): { lvl: number; n: number; pct: number }[] {
  const c = [0, 0, 0, 0];
  evs.forEach((e) => c[e.risk]++);
  const tot = evs.length || 1;
  return c.map((n, i) => ({ lvl: i, n, pct: Math.round((n / tot) * 100) }));
}
