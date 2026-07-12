import { useMemo } from "react";
import { useRuns } from "@/lib/queries";
import { useApp } from "@/store";
import { G } from "@/data/tips";
import { Tip } from "@/components/Tip";
import { CoverageDonut } from "@/components/charts/CoverageDonut";
import { coverageStats, effectMix } from "@/lib/coverage";
import { CHART_FILL } from "@/components/charts/chartTheme";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

export function Coverage() {
  const { openRun } = useApp();
  const { data: runs = [], isLoading } = useRuns();

  const events = useMemo(() => runs.flatMap((r) => r.events), [runs]);

  const cats = useMemo(
    () => effectMix(events).map(({ label, value }) => ({ k: label, v: value })),
    [events],
  );

  const cov = useMemo(() => coverageStats(events), [events]);

  const candidates = useMemo(() => {
    const m: Record<string, { cat: string; min: number; n: number }> = {};
    runs.forEach((r) =>
      r.events.forEach((e) => {
        if (e.cls.conf < 0.9) {
          const o = (m[e.cls.canon] ||= { cat: e.cls.cat, min: 1, n: 0 });
          o.min = Math.min(o.min, e.cls.conf);
          o.n++;
        }
      })
    );
    return Object.entries(m).sort((a, b) => a[1].min - b[1].min);
  }, [runs]);

  // Real context gaps are session-scoped (output-sink `context_gaps`), surfaced
  // by the repository as a per-run `gaps[]` array — unlike the mock, which
  // stamped a gap string on individual events. Each row opens its run.
  const gaps = useMemo(() => {
    const g: { runId: string; title: string; reason: string }[] = [];
    runs.forEach((r) =>
      r.gaps.forEach((gp) => {
        g.push({ runId: r.id, title: r.title, reason: gp.reason });
      })
    );
    return g;
  }, [runs]);

  if (isLoading) {
    return (
      <div className="flex h-64 items-center justify-center text-sm text-muted-foreground">
        Loading coverage…
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Coverage</h1>
        <p className="text-sm text-muted-foreground">
          How completely TraceForge understood the fleet — classification spread, low-confidence
          calls, and recorded gaps.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
        <Card>
          <CardHeader>
            <Tip tip="Classification spread|Scored over CLASSIFIABLE events only — tool calls and permission requests, the events an effect (read_only / mutating / destructive) can apply to. Lifecycle and hook-wrapper events have no effect by nature, so they are excluded from the denominator rather than counted as failures.">
              <CardTitle className="w-fit cursor-help text-base underline decoration-dotted underline-offset-4">
                Classification spread
              </CardTitle>
            </Tip>
            <CardDescription>
              {cov.classified} of {cov.classifiable} classifiable events carry an effect ({cov.pct}
              %).
            </CardDescription>
          </CardHeader>
          <CardContent>
            <CoverageDonut />
            <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1.5">
              {cats.map((c, i) => (
                <div key={c.k} className="flex items-center gap-2 text-[12px]">
                  <span
                    className="size-2.5 shrink-0 rounded-sm"
                    style={{ background: CHART_FILL[i % CHART_FILL.length] }}
                  />
                  <span className="flex-1 truncate capitalize">{c.k}</span>
                  <span className="tabular-nums text-muted-foreground">{c.v}</span>
                </div>
              ))}
            </div>
            {(cov.lifecycle > 0 || cov.hook > 0) && (
              <p className="mt-3 border-t pt-3 text-[11px] leading-snug text-muted-foreground">
                Excludes {cov.lifecycle.toLocaleString()} lifecycle
                {cov.hook > 0 ? ` + ${cov.hook.toLocaleString()} hook-wrapper` : ""} events — no
                effect by nature, so they are not scored here.
              </p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <Tip tip="Override candidates|Canonical tools the classifier resolved with < 90% confidence — the ones worth a tool_display or classification override in your config.">
              <CardTitle className="w-fit cursor-help text-base underline decoration-dotted underline-offset-4">
                Override candidates
              </CardTitle>
            </Tip>
            <CardDescription>Low-confidence classifications, worst first.</CardDescription>
          </CardHeader>
          <CardContent>
            {candidates.length ? (
              <Table>
                <TableHeader>
                  <TableRow className="hover:bg-transparent">
                    <TableHead>Canonical</TableHead>
                    <TableHead>Category</TableHead>
                    <TableHead className="text-right">Events</TableHead>
                    <TableHead className="text-right">Min conf</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {candidates.map(([canon, o]) => (
                    <TableRow key={canon} className="hover:bg-transparent">
                      <TableCell className="font-mono text-[12px]">{canon}</TableCell>
                      <TableCell className="text-[12.5px] capitalize text-muted-foreground">
                        {o.cat}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">{o.n}</TableCell>
                      <TableCell className="text-right tabular-nums">
                        {(o.min * 100).toFixed(0)}%
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <div className="py-8 text-center text-sm text-muted-foreground">
                Every tool classified with ≥ 90% confidence.
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <Tip tip={G.context_gaps}>
            <CardTitle className="w-fit cursor-help text-base underline decoration-dotted underline-offset-4">
              Context gaps
            </CardTitle>
          </Tip>
          <CardDescription>
            {gaps.length} recorded breaks in coverage. Click to open the run.
          </CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          <div className="divide-y divide-border/60">
            {gaps.map((g, i) => (
              <button
                key={i}
                onClick={() => openRun(g.runId)}
                className="flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors hover:bg-muted/40"
              >
                <span className="size-1.5 shrink-0 rounded-full bg-[var(--risk-1)]" />
                <span className="flex-1 truncate text-[12.5px]">{g.reason}</span>
                <span className="truncate text-[11px] text-muted-foreground">{g.title}</span>
              </button>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
