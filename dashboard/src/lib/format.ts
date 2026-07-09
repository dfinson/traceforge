import type { RiskLevel, TEvent } from "@/data/runs";
import { RISK } from "@/data/runs";

export type Dim = "phase" | "tool" | "file" | "segment" | "turn" | "retry";

export const money = (n: number) => "$" + n.toFixed(2);
export const money3 = (n: number) => "$" + n.toFixed(3);
export const tk = (n: number) => (n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n));
export const hhmm = (d: Date) => d.toTimeString().slice(0, 8);
export const dmin = (ms: number) =>
  Math.floor(ms / 60000) + "m " + String(Math.floor(ms / 1000) % 60).padStart(2, "0") + "s";
export const rlab = (l: RiskLevel) => RISK[l];
export const riskVar = (l: number) => `var(--risk-${l})`;

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
