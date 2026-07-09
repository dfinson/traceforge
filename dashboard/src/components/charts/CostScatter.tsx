import {
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";
import { RISK } from "@/lib/types";
import { useRuns } from "@/lib/queries";
import { axisTick, gridStroke, tooltipStyle } from "./chartTheme";

interface Pt {
  id: string;
  title: string;
  x: number;
  y: number;
  z: number;
  peak: number;
}

function ScatterTip({ active, payload }: { active?: boolean; payload?: { payload: Pt }[] }) {
  if (!active || !payload || !payload.length) return null;
  const d = payload[0].payload;
  return (
    <div style={tooltipStyle}>
      <div style={{ color: "var(--foreground)", fontWeight: 600, marginBottom: 3 }}>{d.title}</div>
      <div style={{ color: "var(--muted-foreground)" }}>
        ${d.y.toFixed(2)} · {d.x.toFixed(1)}m · {d.z} events · peak {RISK[d.peak as 0]}
      </div>
    </div>
  );
}

export function CostScatter({ onPick }: { onPick: (id: string) => void }) {
  const { data: runs = [] } = useRuns();
  const data: Pt[] = runs.map((r) => ({
    id: r.id,
    title: r.title,
    x: +(r.durMs / 60000).toFixed(1),
    y: +r.usage.cost.toFixed(2),
    z: r.events.length,
    peak: r.peak,
  }));
  const handle = (pt: { id?: string; payload?: { id?: string } }) => {
    const id = pt?.id ?? pt?.payload?.id;
    if (id) onPick(id);
  };
  return (
    <ResponsiveContainer width="100%" height={230}>
      <ScatterChart margin={{ top: 8, right: 12, left: -6, bottom: 4 }}>
        <CartesianGrid stroke={gridStroke} strokeDasharray="3 3" />
        <XAxis
          type="number"
          dataKey="x"
          name="duration"
          tick={axisTick}
          tickLine={false}
          axisLine={{ stroke: gridStroke }}
          unit="m"
        />
        <YAxis
          type="number"
          dataKey="y"
          name="cost"
          tick={axisTick}
          tickLine={false}
          axisLine={false}
          tickFormatter={(v) => "$" + v}
        />
        <ZAxis type="number" dataKey="z" range={[50, 460]} name="events" />
        <Tooltip cursor={{ strokeDasharray: "3 3" }} content={<ScatterTip />} />
        <Scatter data={data} onClick={handle} className="cursor-pointer">
          {data.map((d) => (
            <Cell
              key={d.id}
              fill={`var(--risk-${d.peak})`}
              fillOpacity={0.78}
              stroke={`var(--risk-${d.peak})`}
            />
          ))}
        </Scatter>
      </ScatterChart>
    </ResponsiveContainer>
  );
}
