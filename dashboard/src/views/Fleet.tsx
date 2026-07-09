import { useMemo } from "react";
import { ArrowRight, Search } from "lucide-react";
import { RISK } from "@/lib/types";
import { useRuns } from "@/lib/queries";
import { useApp } from "@/store";
import type { SortKey } from "@/store";
import { agg, dmin, hhmm, money, tk } from "@/lib/format";
import { G } from "@/data/tips";
import { KpiCard } from "@/components/KpiCard";
import { Tip } from "@/components/Tip";
import { RiskBadge } from "@/components/RiskBadge";
import { DistBar } from "@/components/DistBar";
import { CHART_FILL, RISK_FILL } from "@/components/charts/chartTheme";
import { MiniRibbon } from "@/components/charts/MiniRibbon";
import { ActivityChart } from "@/components/charts/ActivityChart";
import { AttributionBars } from "@/components/charts/AttributionBars";
import { SpendArea } from "@/components/charts/SpendArea";
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

export function Fleet() {
  const { filt, setFilt, sort, setSort, sysdb, openRun, setView } = useApp();
  const { data: runs = [], isLoading } = useRuns();

  const totals = useMemo(() => {
    const all = runs.flatMap((r) => r.events);
    const classified = all.filter((e) => (e.cls?.conf ?? 0) >= 0.9).length;
    return {
      live: runs.filter((r) => r.live).length,
      runs: runs.length,
      spend: runs.reduce((a, r) => a + r.usage.cost, 0),
      tokens: runs.reduce((a, r) => a + r.usage.in + r.usage.out, 0),
      classifiedPct: Math.round((classified / (all.length || 1)) * 100),
      triage: runs.filter((r) => r.peak >= 2).length,
    };
  }, [runs]);

  const rail = useMemo(() => {
    const all = runs.flatMap((r) => r.events);
    const spendByPhase = agg(all, "phase");
    const risk = [0, 1, 2, 3].map((l) => ({
      label: RISK[l],
      value: all.filter((e) => e.risk === l).length,
      color: RISK_FILL[l],
    }));
    const cats: Record<string, number> = {};
    all.forEach((e) => {
      const c = e.cls?.cat || "unclassified";
      cats[c] = (cats[c] || 0) + 1;
    });
    const coverage = Object.entries(cats)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 5)
      .map(([label, value], i) => ({ label, value, color: CHART_FILL[i % CHART_FILL.length] }));
    return { spendByPhase, risk, coverage };
  }, [runs]);

  const rows = useMemo(() => {
    const q = filt.trim().toLowerCase();
    let list = runs.filter(
      (r) =>
        !q ||
        r.title.toLowerCase().includes(q) ||
        r.repo.toLowerCase().includes(q) ||
        r.agent.toLowerCase().includes(q)
    );
    list = [...list].sort((a, b) => {
      if (sort === "risk") return b.peak - a.peak || b.usage.cost - a.usage.cost;
      if (sort === "cost") return b.usage.cost - a.usage.cost;
      return b.started.getTime() - a.started.getTime();
    });
    return list;
  }, [filt, sort, runs]);

  if (isLoading) {
    return (
      <div className="flex h-64 items-center justify-center text-sm text-muted-foreground">
        Loading fleet…
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Fleet</h1>
          <p className="text-sm text-muted-foreground">
            The most recent runs TraceForge has structured. Click a row to rewind it.
          </p>
        </div>
        {totals.triage > 0 && (
          <button
            onClick={() => setView("triage")}
            className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-[12.5px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            <span className="font-medium tabular-nums text-foreground">{totals.triage}</span>
            runs flagged for triage
            <ArrowRight className="size-3.5" />
          </button>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
        <KpiCard
          label="Active"
          value={totals.live}
          sub="live now"
          tip="Live runs|Runs still emitting events. On the CLI path these tail ~/.traceforge/traceforge.db."
        />
        <KpiCard label="Runs" value={totals.runs} sub="last 24h" tip={G.session_id} />
        <KpiCard label="Spend" value={money(totals.spend)} sub="all runs" tip={G.usage_records} />
        <KpiCard label="Tokens" value={tk(totals.tokens)} sub="in + out" tip={G.usage_records} />
        <KpiCard
          label="Classified"
          value={`${totals.classifiedPct}%`}
          sub="conf ≥ 0.9"
          tip={G.context_gaps}
        />
      </div>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-[minmax(0,1fr)_290px]">
        <div className="space-y-5">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Activity by hour</CardTitle>
              <CardDescription>
                Tool events bucketed by hour, stacked by phase — a temporal read on what the fleet was
                doing.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <ActivityChart />
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <Tip tip={G.usage_records}>
                <CardTitle className="w-fit cursor-help text-base underline decoration-dotted underline-offset-4">
                  Spend over time
                </CardTitle>
              </Tip>
              <CardDescription>
                Cumulative fleet cost across the session window — every tool call adds to the running
                total.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <SpendArea />
            </CardContent>
          </Card>
        </div>

        <div className="space-y-5">
          <Card>
            <CardHeader className="pb-3">
              <Tip tip={G.attribution_rollups}>
                <CardTitle className="w-fit cursor-help text-[11px] font-medium uppercase tracking-wide text-muted-foreground underline decoration-dotted underline-offset-4">
                  Spend by phase
                </CardTitle>
              </Tip>
            </CardHeader>
            <CardContent>
              <AttributionBars data={rail.spendByPhase} />
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                Classification mix
              </CardTitle>
            </CardHeader>
            <CardContent>
              <DistBar segments={rail.coverage} />
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="flex-row items-center justify-between gap-2 space-y-0 pb-3">
              <CardTitle className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                Risk mix
              </CardTitle>
              <button
                onClick={() => setView("triage")}
                className="inline-flex items-center gap-1 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
              >
                Triage <ArrowRight className="size-3" />
              </button>
            </CardHeader>
            <CardContent>
              <DistBar segments={rail.risk} />
            </CardContent>
          </Card>
        </div>
      </div>

      <Card>
        <CardHeader className="flex-row items-center justify-between gap-3 space-y-0">
          <div>
            <CardTitle className="text-base">Runs</CardTitle>
            <CardDescription>{rows.length} of {runs.length} shown</CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={filt}
                onChange={(e) => setFilt(e.target.value)}
                placeholder="Filter runs…"
                className="h-8 w-48 pl-8 text-sm"
              />
            </div>
            <Select value={sort} onValueChange={(v) => setSort(v as SortKey)}>
              <SelectTrigger className="h-8 w-[130px] text-sm">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="recent">Most recent</SelectItem>
                <SelectItem value="risk">Highest risk</SelectItem>
                <SelectItem value="cost">Highest cost</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Run</TableHead>
                <TableHead>Identity</TableHead>
                <TableHead>Risk</TableHead>
                <TableHead className="text-right">Events</TableHead>
                <TableHead className="text-right">Cost</TableHead>
                <TableHead className="text-right">Duration</TableHead>
                <TableHead className="text-right">Started</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((r) => (
                <TableRow
                  key={r.id}
                  onClick={() => openRun(r.id)}
                  className="cursor-pointer"
                >
                  <TableCell>
                    <div className="flex items-center gap-2">
                      {r.live && (
                        <span className="relative flex size-2">
                          <span className="absolute inline-flex size-full animate-ping rounded-full bg-[var(--risk-0)] opacity-70" />
                          <span className="relative inline-flex size-2 rounded-full bg-[var(--risk-0)]" />
                        </span>
                      )}
                      <div className="min-w-0">
                        <div className="max-w-[34ch] truncate font-medium">{r.title}</div>
                        {sysdb ? (
                          <div className="text-[11px] text-muted-foreground">{r.repo}</div>
                        ) : (
                          <Tip tip={G.identity}>
                            <span className="cursor-help text-[11px] text-muted-foreground underline decoration-dotted underline-offset-2">
                              unknown repo
                            </span>
                          </Tip>
                        )}
                      </div>
                    </div>
                  </TableCell>
                  <TableCell>
                    {sysdb ? (
                      <div className="text-[12.5px]">
                        <div>{r.agent}</div>
                        <div className="text-[11px] text-muted-foreground">{r.model}</div>
                      </div>
                    ) : (
                      <Tip tip={G.identity}>
                        <span className="cursor-help text-[12.5px] text-muted-foreground underline decoration-dotted underline-offset-2">
                          unknown
                        </span>
                      </Tip>
                    )}
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-2">
                      <MiniRibbon events={r.events} />
                      <RiskBadge level={r.peak} />
                    </div>
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{r.events.length}</TableCell>
                  <TableCell className="text-right tabular-nums">{money(r.usage.cost)}</TableCell>
                  <TableCell className="text-right tabular-nums text-muted-foreground">
                    {dmin(r.durMs)}
                  </TableCell>
                  <TableCell className="text-right tabular-nums text-muted-foreground">
                    {hhmm(r.started)}
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
