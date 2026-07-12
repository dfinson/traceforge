import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";
import { useRuns } from "@/lib/queries";
import { coverageStats, effectMix } from "@/lib/coverage";
import { CHART_FILL, tooltipItemStyle, tooltipLabelStyle, tooltipStyle } from "./chartTheme";

export function CoverageDonut() {
  const { data: runs = [] } = useRuns();
  const events = runs.flatMap((r) => r.events);
  // Scored over classifiable events only (tool calls + permissions); each slice
  // is an effect category, plus one honest "unclassified" slice for classifiable
  // events the classifier has not resolved yet.
  const data = effectMix(events).map(({ label, value }) => ({ k: label, v: value }));
  const { classifiable } = coverageStats(events);
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
        <span className="text-2xl font-semibold tabular-nums">{classifiable}</span>
        <span className="text-[11px] text-muted-foreground">classifiable</span>
      </div>
    </div>
  );
}
