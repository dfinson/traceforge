// Segmented distribution bar + dot legend — the CodePlane "change breakdown" pattern.
export type Seg = { label: string; value: number; color: string };

export function DistBar({ segments, unit }: { segments: Seg[]; unit?: string }) {
  const total = segments.reduce((a, s) => a + s.value, 0) || 1;
  return (
    <div className="space-y-2.5">
      <div className="flex h-2 overflow-hidden rounded-full bg-border">
        {segments.map((s) =>
          s.value > 0 ? (
            <div
              key={s.label}
              style={{ width: `${(s.value / total) * 100}%`, background: s.color }}
              className="transition-all"
            />
          ) : null
        )}
      </div>
      <div className="space-y-1.5">
        {segments.map((s) => (
          <div key={s.label} className="flex items-center justify-between text-[12px]">
            <div className="flex min-w-0 items-center gap-1.5">
              <span className="size-2 shrink-0 rounded-full" style={{ background: s.color }} />
              <span className="truncate capitalize text-foreground/90">{s.label}</span>
            </div>
            <span className="tabular-nums text-muted-foreground">
              {s.value}
              {unit ?? ""}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
