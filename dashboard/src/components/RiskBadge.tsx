import type { RiskLevel } from "@/lib/types";
import { RISK } from "@/lib/types";
import { RTIP } from "@/data/tips";
import { Tip } from "./Tip";

export function RiskBadge({ level, prefix }: { level: RiskLevel; prefix?: string }) {
  return (
    <Tip tip={RTIP[level]}>
      <span
        className={`risk-soft-${level} inline-flex cursor-default items-center gap-1.5 rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide`}
      >
        <span className="size-1.5 rounded-full" style={{ background: `var(--risk-${level})` }} />
        {prefix ?? ""}
        {RISK[level]}
      </span>
    </Tip>
  );
}

export function RiskDot({ level }: { level: RiskLevel }) {
  return (
    <Tip tip={RTIP[level]}>
      <span
        className="inline-block size-2.5 cursor-default rounded-full"
        style={{ background: `var(--risk-${level})` }}
      />
    </Tip>
  );
}
