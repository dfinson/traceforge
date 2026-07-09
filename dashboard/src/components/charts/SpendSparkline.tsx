import { Area, AreaChart, ResponsiveContainer, YAxis } from "recharts";
import type { TEvent } from "@/lib/types";

export function SpendSparkline({ events }: { events: TEvent[] }) {
  let c = 0;
  const data = events.map((e, i) => ({ i, v: +(c += e.cost).toFixed(3) }));
  return (
    <ResponsiveContainer width="100%" height={38}>
      <AreaChart data={data} margin={{ top: 2, right: 0, bottom: 0, left: 0 }}>
        <defs>
          <linearGradient id="spk" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--chart-1)" stopOpacity={0.45} />
            <stop offset="100%" stopColor="var(--chart-1)" stopOpacity={0} />
          </linearGradient>
        </defs>
        <YAxis hide domain={[0, "dataMax"]} />
        <Area
          type="monotone"
          dataKey="v"
          stroke="var(--chart-1)"
          strokeWidth={1.5}
          fill="url(#spk)"
          isAnimationActive={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
