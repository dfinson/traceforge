import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { RUNS } from "@/data/runs";
import { axisTick, tooltipItemStyle, tooltipLabelStyle, tooltipStyle } from "./chartTheme";

export function TechniqueBars() {
  const m: Record<string, { label: string; n: number }> = {};
  RUNS.forEach((r) =>
    r.events.forEach((e) => {
      if (e.ev) {
        const [id, label] = e.ev.mitre;
        (m[id] ||= { label, n: 0 }).n++;
      }
    })
  );
  const data = Object.entries(m)
    .map(([id, o]) => ({ k: `${id} · ${o.label}`, v: o.n }))
    .sort((a, b) => b.v - a.v)
    .slice(0, 6);
  if (!data.length) return <Empty />;
  return (
    <ResponsiveContainer width="100%" height={Math.max(120, data.length * 34)}>
      <BarChart data={data} layout="vertical" margin={{ top: 0, right: 20, left: 8, bottom: 0 }}>
        <XAxis type="number" tick={axisTick} tickLine={false} axisLine={false} allowDecimals={false} />
        <YAxis type="category" dataKey="k" tick={axisTick} tickLine={false} axisLine={false} width={150} />
        <Tooltip
          cursor={{ fill: "var(--muted)", opacity: 0.35 }}
          contentStyle={tooltipStyle}
          labelStyle={tooltipLabelStyle}
          itemStyle={tooltipItemStyle}
          formatter={(v: any) => [String(v ?? ""), "matches"]}
        />
        <Bar dataKey="v" radius={[0, 3, 3, 0]} fill="var(--risk-2)" barSize={16} />
      </BarChart>
    </ResponsiveContainer>
  );
}

function Empty() {
  return (
    <div className="flex h-28 items-center justify-center text-sm text-muted-foreground">
      No ATT&CK techniques matched.
    </div>
  );
}
