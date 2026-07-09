import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { axisTick, tooltipItemStyle, tooltipLabelStyle, tooltipStyle } from "./chartTheme";

export function AttributionBars({ data }: { data: { k: string; v: number }[] }) {
  const top = data.slice(0, 8);
  return (
    <ResponsiveContainer width="100%" height={Math.max(130, top.length * 34)}>
      <BarChart data={top} layout="vertical" margin={{ top: 0, right: 20, left: 8, bottom: 0 }}>
        <XAxis
          type="number"
          tick={axisTick}
          tickLine={false}
          axisLine={false}
          tickFormatter={(v) => "$" + Number(v).toFixed(2)}
        />
        <YAxis
          type="category"
          dataKey="k"
          tick={axisTick}
          tickLine={false}
          axisLine={false}
          width={130}
        />
        <Tooltip
          cursor={{ fill: "var(--muted)", opacity: 0.35 }}
          contentStyle={tooltipStyle}
          labelStyle={tooltipLabelStyle}
          itemStyle={tooltipItemStyle}
          formatter={(v: any) => ["$" + Number(v ?? 0).toFixed(3), "cost"]}
        />
        <Bar dataKey="v" radius={[0, 3, 3, 0]} fill="var(--chart-1)" barSize={16} />
      </BarChart>
    </ResponsiveContainer>
  );
}
