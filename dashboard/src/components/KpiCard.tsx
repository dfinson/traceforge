import type { ReactNode } from "react";
import { Card } from "@/components/ui/card";
import { Tip } from "./Tip";

export function KpiCard({
  label,
  value,
  sub,
  tip,
  accent,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tip?: string;
  accent?: string;
}) {
  return (
    <Card className="gap-1.5 p-4">
      {tip ? (
        <Tip tip={tip}>
          <span className="w-fit cursor-help text-[11px] font-medium uppercase tracking-wide text-muted-foreground underline decoration-dotted decoration-muted-foreground/40 underline-offset-2">
            {label}
          </span>
        </Tip>
      ) : (
        <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          {label}
        </span>
      )}
      <span className="text-2xl font-semibold tabular-nums" style={accent ? { color: accent } : undefined}>
        {value}
      </span>
      {sub && <span className="text-[11px] text-muted-foreground">{sub}</span>}
    </Card>
  );
}
