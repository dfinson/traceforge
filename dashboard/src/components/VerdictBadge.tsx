import type { Verdict } from "@/data/runs";
import { ATIP, PRED_TIP } from "@/data/tips";
import { Tip } from "./Tip";

const VC: Record<Verdict, string> = {
  allow: "border-border text-muted-foreground",
  warn: "risk-soft-1",
  escalate: "risk-soft-2",
  deny: "risk-soft-3",
  transform: "border-border text-foreground",
};

export function VerdictBadge({ v }: { v: Verdict }) {
  return (
    <Tip tip={ATIP[v]}>
      <span
        className={`${VC[v]} inline-flex cursor-default items-center rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide`}
      >
        {v}
      </span>
    </Tip>
  );
}

export function Pred({ p }: { p: string }) {
  return (
    <Tip tip={PRED_TIP}>
      <span className="cursor-default rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[10.5px] text-muted-foreground">
        {p}
      </span>
    </Tip>
  );
}
