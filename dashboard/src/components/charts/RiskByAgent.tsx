import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useRuns } from "@/lib/queries";
import {
  axisTick,
  gridStroke,
  RISK_FILL,
  tooltipItemStyle,
  tooltipLabelStyle,
  tooltipStyle,
} from "./chartTheme";

export function RiskByAgent() {
  const { data: runs = [] } = useRuns();
  const m: Record<string, number[]> = {};
  runs.forEach((r) =>
    r.events.forEach((e) => {
      (m[r.agent] ||= [0, 0, 0, 0])[e.risk]++;
    })
  );
  const data = Object.entries(m).map(([agent, c]) => ({
    agent,
    safe: c[0],
    caution: c[1],
    danger: c[2],
    critical: c[3],
  }));
  return (
    <ResponsiveContainer width="100%" height={190}>
      <BarChart data={data} margin={{ top: 4, right: 6, left: -16, bottom: 0 }}>
        <CartesianGrid vertical={false} stroke={gridStroke} strokeDasharray="3 3" />
        <XAxis dataKey="agent" tick={axisTick} tickLine={false} axisLine={{ stroke: gridStroke }} />
        <YAxis tick={axisTick} tickLine={false} axisLine={false} width={38} allowDecimals={false} />
        <Tooltip
          cursor={{ fill: "var(--muted)", opacity: 0.35 }}
          contentStyle={tooltipStyle}
          labelStyle={tooltipLabelStyle}
          itemStyle={tooltipItemStyle}
        />
        <Bar dataKey="safe" stackId="a" fill={RISK_FILL[0]} />
        <Bar dataKey="caution" stackId="a" fill={RISK_FILL[1]} />
        <Bar dataKey="danger" stackId="a" fill={RISK_FILL[2]} />
        <Bar dataKey="critical" stackId="a" fill={RISK_FILL[3]} radius={[2, 2, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}
