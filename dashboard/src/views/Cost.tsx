import { useMemo } from "react";
import { useRuns } from "@/lib/queries";
import { useApp } from "@/store";
import type { Dim } from "@/lib/format";
import { agg, money, money3 } from "@/lib/format";
import { DIMTIP, G } from "@/data/tips";
import { Tip } from "@/components/Tip";
import { KpiCard } from "@/components/KpiCard";
import { AttributionBars } from "@/components/charts/AttributionBars";
import { CostScatter } from "@/components/charts/CostScatter";
import { SpendArea } from "@/components/charts/SpendArea";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

const DIMS: Dim[] = ["phase", "tool", "file"];

export function Cost() {
  const { dim, setDim, openRun } = useApp();
  const { data: runs = [], isLoading } = useRuns();
  const activeDim: Dim = DIMS.includes(dim) ? dim : "phase";

  const all = useMemo(() => runs.flatMap((r) => r.events), [runs]);
  const bars = useMemo(() => agg(all, activeDim).map((d) => ({ k: d.k, v: d.v })), [all, activeDim]);
  const totals = useMemo(() => {
    const cost = all.reduce((a, e) => a + e.cost, 0);
    const tokens = all.reduce((a, e) => a + e.tokens, 0);
    const retried = all.filter((e) => e.retry).reduce((a, e) => a + e.cost, 0);
    return { cost, tokens, retried, calls: all.length };
  }, [all]);

  const models = useMemo(() => {
    const m: Record<string, { calls: number; tokens: number; cost: number }> = {};
    runs.forEach((r) => {
      const o = (m[r.model] ||= { calls: 0, tokens: 0, cost: 0 });
      o.calls += r.events.length;
      o.tokens += r.usage.in + r.usage.out;
      // Unknown cost (null — e.g. Copilot carries no dollars) adds nothing to the
      // per-model dollar total rather than NaN-poisoning it.
      o.cost += r.usage.cost ?? 0;
    });
    return Object.entries(m).sort((a, b) => b[1].cost - a[1].cost);
  }, [runs]);

  if (isLoading) {
    return (
      <div className="flex h-64 items-center justify-center text-sm text-muted-foreground">
        Loading cost…
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Cost</h1>
        <p className="text-sm text-muted-foreground">
          Spend and latency attributed across trace-native dimensions.
        </p>
      </div>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <KpiCard label="Total spend" value={money(totals.cost)} tip={G.attribution_rollups} />
        <KpiCard label="Tokens" value={(totals.tokens / 1000).toFixed(0) + "k"} tip={G.usage_records} />
        <KpiCard label="Tool calls" value={totals.calls} tip={G.spans} />
        <KpiCard
          label="Retry waste"
          value={money(totals.retried)}
          accent="var(--risk-1)"
          tip={DIMTIP.retry}
        />
      </div>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        <Card>
          <CardHeader className="flex-row items-start justify-between gap-3 space-y-0">
            <div>
              <CardTitle className="text-base">Attribution</CardTitle>
              <CardDescription>Cost grouped by dimension.</CardDescription>
            </div>
            <Tabs value={activeDim} onValueChange={(v) => setDim(v as Dim)}>
              <TabsList>
                {DIMS.map((d) => (
                  <TabsTrigger key={d} value={d} className="capitalize">
                    {d}
                  </TabsTrigger>
                ))}
              </TabsList>
            </Tabs>
          </CardHeader>
          <CardContent>
            <Tip tip={DIMTIP[activeDim]}>
              <div className="mb-2 w-fit cursor-help text-[11px] text-muted-foreground underline decoration-dotted underline-offset-2">
                by {activeDim}
              </div>
            </Tip>
            <AttributionBars data={bars} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">Cost × duration</CardTitle>
            <CardDescription>
              Each run — bubble size = events, color = peak risk. Click to open.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <CostScatter onPick={openRun} />
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Cumulative spend</CardTitle>
          <CardDescription>Fleet spend accruing over the day.</CardDescription>
        </CardHeader>
        <CardContent>
          <SpendArea />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">By model</CardTitle>
          <CardDescription>Spend and volume per model.</CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Model</TableHead>
                <TableHead className="text-right">Calls</TableHead>
                <TableHead className="text-right">Tokens</TableHead>
                <TableHead className="text-right">Cost</TableHead>
                <TableHead className="text-right">$/call</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {models.map(([model, o]) => (
                <TableRow key={model} className="hover:bg-transparent">
                  <TableCell className="font-mono text-[12.5px]">{model}</TableCell>
                  <TableCell className="text-right tabular-nums">{o.calls}</TableCell>
                  <TableCell className="text-right tabular-nums">
                    {(o.tokens / 1000).toFixed(1)}k
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{money(o.cost)}</TableCell>
                  <TableCell className="text-right tabular-nums text-muted-foreground">
                    {money3(o.cost / o.calls)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
