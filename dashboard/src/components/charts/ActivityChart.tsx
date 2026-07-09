import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { RUNS } from "@/data/runs";
import {
  axisTick,
  gridStroke,
  CHART_FILL,
  tooltipItemStyle,
  tooltipLabelStyle,
  tooltipStyle,
} from "./chartTheme";

export function ActivityChart() {
  const phaseTotals: Record<string, number> = {};
  RUNS.forEach((run) =>
    run.events.forEach((e) => {
      phaseTotals[e.phase] = (phaseTotals[e.phase] || 0) + 1;
    })
  );
  const phases = Object.entries(phaseTotals)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6)
    .map(([p]) => p);

  const buckets: Record<number, Record<string, number>> = {};
  RUNS.forEach((run) =>
    run.events.forEach((e) => {
      const h = e.t.getHours();
      const b = (buckets[h] ||= {});
      b[e.phase] = (b[e.phase] || 0) + 1;
    })
  );
  const data = Object.keys(buckets)
    .map(Number)
    .sort((a, b) => a - b)
    .map((h) => {
      const row: Record<string, string | number> = { hour: `${String(h).padStart(2, "0")}:00` };
      phases.forEach((p) => (row[p] = buckets[h][p] || 0));
      return row;
    });

  return (
    <ResponsiveContainer width="100%" height={172}>
      <BarChart data={data} margin={{ top: 4, right: 6, left: -16, bottom: 0 }}>
        <CartesianGrid vertical={false} stroke={gridStroke} strokeDasharray="3 3" />
        <XAxis dataKey="hour" tick={axisTick} tickLine={false} axisLine={{ stroke: gridStroke }} />
        <YAxis tick={axisTick} tickLine={false} axisLine={false} width={38} allowDecimals={false} />
        <Tooltip
          cursor={{ fill: "var(--muted)", opacity: 0.35 }}
          contentStyle={tooltipStyle}
          labelStyle={tooltipLabelStyle}
          itemStyle={tooltipItemStyle}
        />
        {phases.map((p, i) => (
          <Bar
            key={p}
            dataKey={p}
            stackId="a"
            fill={CHART_FILL[i % CHART_FILL.length]}
            radius={i === phases.length - 1 ? [2, 2, 0, 0] : undefined}
          />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}
