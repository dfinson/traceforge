import type { TEvent } from "@/data/runs";
import { RISK } from "@/data/runs";

export function RiskRibbon({
  events,
  sel,
  onSelect,
}: {
  events: TEvent[];
  sel: number;
  onSelect: (i: number) => void;
}) {
  return (
    <div className="flex h-9 w-full gap-px overflow-hidden rounded-md bg-muted/30 p-0.5">
      {events.map((e, i) => (
        <button
          key={e.id}
          onClick={() => onSelect(i)}
          title={`#${i + 1} · ${e.tool.n} · ${RISK[e.risk]}`}
          className="relative min-w-[3px] flex-1 rounded-[2px] transition-opacity hover:opacity-100"
          style={{ background: `var(--risk-${e.risk})`, opacity: i === sel ? 1 : 0.55 }}
        >
          {i === sel && (
            <span className="absolute inset-0 rounded-[2px] ring-2 ring-inset ring-foreground/80" />
          )}
        </button>
      ))}
    </div>
  );
}
