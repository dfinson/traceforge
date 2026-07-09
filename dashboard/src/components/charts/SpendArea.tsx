import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { RUNS } from "@/data/runs";
import { axisTick, gridStroke, tooltipItemStyle, tooltipLabelStyle, tooltipStyle } from "./chartTheme";

export function SpendArea() {
  const all = RUNS.flatMap((r) => r.events).sort((a, b) => a.t.getTime() - b.t.getTime());
  let c = 0;
  const data = all.map((e) => ({
    t: e.t.toTimeString().slice(0, 5),
    v: +(c += e.cost).toFixed(2),
  }));
  return (
    <ResponsiveContainer width="100%" height={190}>
      <AreaChart data={data} margin={{ top: 6, right: 12, left: -8, bottom: 0 }}>
        <defs>
          <linearGradient id="spend" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--chart-1)" stopOpacity={0.4} />
            <stop offset="100%" stopColor="var(--chart-1)" stopOpacity={0.02} />
          </linearGradient>
        </defs>
        <CartesianGrid vertical={false} stroke={gridStroke} strokeDasharray="3 3" />
        <XAxis
          dataKey="t"
          tick={axisTick}
          tickLine={false}
          axisLine={{ stroke: gridStroke }}
          minTickGap={48}
        />
        <YAxis
          tick={axisTick}
          tickLine={false}
          axisLine={false}
          width={46}
          tickFormatter={(v) => "$" + v}
        />
        <Tooltip
          contentStyle={tooltipStyle}
          labelStyle={tooltipLabelStyle}
          itemStyle={tooltipItemStyle}
          formatter={(v: any) => ["$" + Number(v ?? 0).toFixed(2), "cumulative"]}
        />
        <Area
          type="monotone"
          dataKey="v"
          stroke="var(--chart-1)"
          strokeWidth={2}
          fill="url(#spend)"
          isAnimationActive={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
