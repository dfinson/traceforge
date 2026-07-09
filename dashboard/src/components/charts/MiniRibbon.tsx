import type { TEvent } from "@/data/runs";

export function MiniRibbon({ events }: { events: TEvent[] }) {
  return (
    <div className="flex h-4 w-28 gap-px overflow-hidden rounded-sm bg-muted/30">
      {events.map((e, i) => (
        <span
          key={i}
          className="min-w-[1px] flex-1"
          style={{ background: `var(--risk-${e.risk})`, opacity: e.risk === 0 ? 0.45 : 0.9 }}
        />
      ))}
    </div>
  );
}
