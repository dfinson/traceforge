import { useMemo } from "react";
import { RUNS } from "@/data/runs";
import { useApp } from "@/store";
import { G } from "@/data/tips";
import { Tip } from "@/components/Tip";
import { CoverageDonut } from "@/components/charts/CoverageDonut";
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
  const { openEvent } = useApp();

  const cats = useMemo(() => {
    const m: Record<string, number> = {};
    RUNS.forEach((r) => r.events.forEach((e) => (m[e.cls.cat] = (m[e.cls.cat] || 0) + 1)));
    return Object.entries(m)
      .map(([k, v]) => ({ k, v }))
      .sort((a, b) => b.v - a.v);
  }, []);

  const candidates = useMemo(() => {
    const m: Record<string, { cat: string; min: number; n: number }> = {};
    RUNS.forEach((r) =>
      r.events.forEach((e) => {
        if (e.cls.conf < 0.9) {
          const o = (m[e.cls.canon] ||= { cat: e.cls.cat, min: 1, n: 0 });
          o.min = Math.min(o.min, e.cls.conf);
          o.n++;
        }
      })
    );
    return Object.entries(m).sort((a, b) => a[1].min - b[1].min);
  }, []);

  const gaps = useMemo(() => {
    const g: { runId: string; title: string; idx: number; gap: string }[] = [];
    RUNS.forEach((r) =>
      r.events.forEach((e, idx) => {
        if (e.gap) g.push({ runId: r.id, title: r.title, idx, gap: e.gap });
      })
    );
    return g;
  }, []);

  const total = cats.reduce((a, c) => a + c.v, 0);

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
            <CardTitle className="text-base">Classification spread</CardTitle>
            <CardDescription>{total} events by canonical tool category.</CardDescription>
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
            {gaps.length} recorded breaks in coverage. Click to inspect the event.
          </CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          <div className="divide-y divide-border/60">
            {gaps.map((g, i) => (
              <button
                key={i}
                onClick={() => openEvent(g.runId, g.idx)}
                className="flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors hover:bg-muted/40"
              >
                <span className="size-1.5 shrink-0 rounded-full bg-[var(--risk-1)]" />
                <span className="flex-1 truncate text-[12.5px]">{g.gap}</span>
                <span className="truncate text-[11px] text-muted-foreground">{g.title}</span>
              </button>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
