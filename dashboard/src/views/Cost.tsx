import { useMemo } from "react";
import { useRuns } from "@/lib/queries";
import { fmtAiu, nsum, pct, tk } from "@/lib/format";
import { G } from "@/data/tips";
import { KpiCard } from "@/components/KpiCard";
import { DistBar } from "@/components/DistBar";
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

// GitHub Copilot bills in AI Units ("AIU", AI credits), not dollars — the wire
// carries no per-event cost. AIU is now the PRIMARY billing signal; the
// premium-request count is a secondary/legacy count. This view is built entirely
// from the real signals persisted on usage_records: total AIU, the premium-request
// count, and cache-aware token attribution. Every aggregate is null-aware: unknown
// stays null (rendered "—"), never a fabricated 0, so a genuine zero (e.g. a model
// that ran no premium requests) stays distinct from "we don't know". No dollars are
// ever derived from AIU.
export function Cost() {
  const { data: runs = [], isLoading } = useRuns();

  const totals = useMemo(() => {
    const aiu = runs.reduce<number | null>((a, r) => nsum(a, r.usage.aiuNano), null);
    const premium = runs.reduce<number | null>((a, r) => nsum(a, r.usage.premiumRequests), null);
    const billedInput = runs.reduce<number | null>((a, r) => nsum(a, r.usage.inputUncached), null);
    const cacheRead = runs.reduce<number | null>((a, r) => nsum(a, r.usage.cacheRead), null);
    const cacheCreation = runs.reduce<number | null>(
      (a, r) => nsum(a, r.usage.cacheCreation),
      null
    );
    const output = runs.reduce((a, r) => a + r.usage.out, 0);
    const inputTotal = nsum(nsum(billedInput, cacheRead), cacheCreation);
    const cacheHeadline =
      inputTotal && cacheRead != null ? ((cacheRead / inputTotal) * 100).toFixed(1) + "%" : "—";
    return {
      aiu,
      premium,
      billedInput,
      cacheRead,
      cacheCreation,
      output,
      inputTotal,
      cachePct: pct(cacheRead, inputTotal),
      cacheHeadline,
      hasTokens: inputTotal != null,
      // Only render classes we actually know: an unknown (null) class is dropped
      // rather than shown as a fabricated "0"; a genuine 0 (value != null) stays.
      tokenSegs: [
        { label: "billed input", value: billedInput, color: CHART_FILL[0] },
        { label: "cache read", value: cacheRead, color: CHART_FILL[2] },
        { label: "cache creation", value: cacheCreation, color: CHART_FILL[3] },
      ].filter((s): s is { label: string; value: number; color: string } => s.value != null),
    };
  }, [runs]);

  // Per-model attribution: fold every run's usage.models list into one map keyed
  // by the raw model string, summing null-aware. A blank model string is real
  // token usage we couldn't attribute — surfaced as "unknown model", never dropped.
  const models = useMemo(() => {
    const m = new Map<
      string,
      {
        model: string;
        aiuNano: number | null;
        premiumRequests: number | null;
        requests: number | null;
        inputUncached: number | null;
        cacheRead: number | null;
        output: number;
      }
    >();
    runs
      .flatMap((r) => r.usage.models)
      .forEach((mu) => {
        const o = m.get(mu.model) ?? {
          model: mu.model,
          aiuNano: null,
          premiumRequests: null,
          requests: null,
          inputUncached: null,
          cacheRead: null,
          output: 0,
        };
        o.aiuNano = nsum(o.aiuNano, mu.aiuNano);
        o.premiumRequests = nsum(o.premiumRequests, mu.premiumRequests);
        o.requests = nsum(o.requests, mu.requests);
        o.inputUncached = nsum(o.inputUncached, mu.inputUncached);
        o.cacheRead = nsum(o.cacheRead, mu.cacheRead);
        o.output += mu.output;
        m.set(mu.model, o);
      });
    // Sort AIU desc (the primary signal), then premium desc, then requests desc.
    // `?? -1` sinks unknown below a real 0 so the honest "we don't know" rows never
    // outrank a measured zero.
    return [...m.values()].sort(
      (a, b) =>
        (b.aiuNano ?? -1) - (a.aiuNano ?? -1) ||
        (b.premiumRequests ?? -1) - (a.premiumRequests ?? -1) ||
        (b.requests ?? -1) - (a.requests ?? -1)
    );
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
          GitHub Copilot bills in AI credits (AIU); premium requests are a secondary/legacy count.
          The wire carries no dollar cost (—). Tokens are attributed per model.
        </p>
      </div>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        <KpiCard
          label="AI credits"
          value={fmtAiu(totals.aiu)}
          sub="the billed signal"
          tip={G.usage_records}
        />
        <KpiCard
          label="Premium requests"
          value={totals.premium == null ? "—" : totals.premium}
          sub="secondary / legacy count"
          tip={G.usage_records}
        />
        <KpiCard
          label="Billed input"
          value={totals.billedInput == null ? "—" : tk(totals.billedInput)}
          sub="uncached tokens"
          tip={G.usage_records}
        />
        <KpiCard
          label="Cache reads"
          value={totals.cacheRead == null ? "—" : tk(totals.cacheRead)}
          sub={totals.cachePct == null ? "input served from cache" : `${totals.cachePct}% of input`}
          tip={G.usage_records}
        />
        <KpiCard label="Output" value={tk(totals.output)} sub="tokens" tip={G.usage_records} />
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Cache efficiency</CardTitle>
          <CardDescription>
            Input tokens by billing class — cache reads bill at a fraction of fresh input, so a
            high cache share is the real cost story.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {totals.hasTokens ? (
            <div className="space-y-3">
              <div className="flex items-baseline justify-between">
                <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
                  served from cache
                </span>
                <span className="text-lg font-semibold tabular-nums">{totals.cacheHeadline}</span>
              </div>
              <DistBar segments={totals.tokenSegs} />
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">No token breakdown available.</p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">By model</CardTitle>
          <CardDescription>
            AI credits, premium requests, and cache-aware token volume per model — the honest
            per-model attribution.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Model</TableHead>
                <TableHead className="text-right">AI credits</TableHead>
                <TableHead className="text-right">Premium reqs</TableHead>
                <TableHead className="text-right">Requests</TableHead>
                <TableHead className="text-right">Billed input</TableHead>
                <TableHead className="text-right">Cache read</TableHead>
                <TableHead className="text-right">Output</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {models.length === 0 ? (
                <TableRow className="hover:bg-transparent">
                  <TableCell colSpan={7} className="text-center text-muted-foreground">
                    No usage records.
                  </TableCell>
                </TableRow>
              ) : (
                models.map((o) => (
                  <TableRow key={o.model} className="hover:bg-transparent">
                    <TableCell className="font-mono text-[12.5px]">
                      {o.model || "unknown model"}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{fmtAiu(o.aiuNano)}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      {o.premiumRequests == null ? "—" : o.premiumRequests}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {o.requests == null ? "—" : o.requests}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {o.inputUncached == null ? "—" : tk(o.inputUncached)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {o.cacheRead == null ? "—" : tk(o.cacheRead)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{tk(o.output)}</TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
