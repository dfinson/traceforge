import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";
import { useRuns } from "@/lib/queries";
import { CHART_FILL, tooltipItemStyle, tooltipLabelStyle, tooltipStyle } from "./chartTheme";

export function CoverageDonut() {
  const { data: runs = [] } = useRuns();
  const m: Record<string, number> = {};
  runs.forEach((r) =>
    r.events.forEach((e) => {
      m[e.cls.cat] = (m[e.cls.cat] || 0) + 1;
    })
  );
  const data = Object.entries(m)
    .map(([k, v]) => ({ k, v }))
    .sort((a, b) => b.v - a.v);
  const total = data.reduce((a, d) => a + d.v, 0);
  return (
    <div className="relative">
      <ResponsiveContainer width="100%" height={220}>
        <PieChart>
          <Pie
            data={data}
            dataKey="v"
            nameKey="k"
            innerRadius={62}
            outerRadius={92}
            paddingAngle={2}
            stroke="var(--card)"
            strokeWidth={2}
          >
            {data.map((_d, i) => (
              <Cell key={i} fill={CHART_FILL[i % CHART_FILL.length]} />
            ))}
          </Pie>
          <Tooltip
            contentStyle={tooltipStyle}
            labelStyle={tooltipLabelStyle}
            itemStyle={tooltipItemStyle}
            formatter={(v: any) => [String(v ?? ""), "events"]}
          />
        </PieChart>
      </ResponsiveContainer>
      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
        <span className="text-2xl font-semibold tabular-nums">{total}</span>
        <span className="text-[11px] text-muted-foreground">classified</span>
      </div>
    </div>
  );
}
