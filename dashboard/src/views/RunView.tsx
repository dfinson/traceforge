import { useMemo, useState } from "react";
import { AlertTriangle, ArrowLeft, ChevronRight, FileText } from "lucide-react";
import { RUNS } from "@/data/runs";
import type { TEvent } from "@/data/runs";
import { useApp } from "@/store";
import { dmin, hhmm, money, money3 } from "@/lib/format";
import { G, mtip } from "@/data/tips";
import { Tip } from "@/components/Tip";
import { RiskBadge, RiskDot } from "@/components/RiskBadge";
import { VerdictBadge, Pred } from "@/components/VerdictBadge";
import { RiskRibbon } from "@/components/charts/RiskRibbon";
import { SpendSparkline } from "@/components/charts/SpendSparkline";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";

export function RunView() {
  const { runId, sel, setSel, sysdb, back } = useApp();
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const run = runId ? RUNS.find((r) => r.id === runId) : null;
  if (!run) return null;
  const evs = run.events;
  const cur = evs[Math.min(sel, evs.length - 1)];
  const activities = run.segs.filter((s) => s.kind === "activity");

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="truncate text-xl font-semibold tracking-tight">{run.title}</h1>
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12.5px] text-muted-foreground">
            {sysdb ? (
              <span>
                {run.repo} · {run.agent} · {run.model}
              </span>
            ) : (
              <Tip tip={G.identity}>
                <span className="cursor-help underline decoration-dotted underline-offset-2">
                  identity unknown (SDK-embed)
                </span>
              </Tip>
            )}
            <span>·</span>
            <span>{hhmm(run.started)}</span>
            <span>·</span>
            <span>{dmin(run.durMs)}</span>
            <span>·</span>
            <span>{money(run.usage.cost)}</span>
          </div>
        </div>
        <Button variant="outline" size="sm" onClick={back} className="shrink-0">
          <ArrowLeft className="size-4" /> Fleet
        </Button>
      </div>

      <Card>
        <CardHeader className="flex-row items-center justify-between gap-3 space-y-0">
          <div>
            <CardTitle className="text-base">Rewind</CardTitle>
            <CardDescription>
              One sliver per event, colored by risk. Click to inspect — spend accrues below.
            </CardDescription>
          </div>
          <div className="flex items-center gap-4 text-[12px] text-muted-foreground">
            <span>
              peak <RiskBadge level={run.peak} />
            </span>
            <Tip tip={sysdb ? G.gov_meta : G.identity}>
              <span className="cursor-help underline decoration-dotted underline-offset-2">
                drift {sysdb ? run.drift.toFixed(2) : "n/a"}
              </span>
            </Tip>
          </div>
        </CardHeader>
        <CardContent className="space-y-1">
          <RiskRibbon events={evs} sel={sel} onSelect={setSel} />
          <SpendSparkline events={evs} />
          <div className="flex justify-between text-[10.5px] text-muted-foreground">
            <span>{hhmm(evs[0].t)}</span>
            <span>
              {evs.length} events · {money(run.usage.cost)} total
            </span>
            <span>{hhmm(evs[evs.length - 1].t)}</span>
          </div>
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.35fr)]">
        <div className="space-y-5">
          <Card className="overflow-hidden">
            <CardHeader>
              <Tip tip={G.segment_titles}>
                <CardTitle className="w-fit cursor-help text-base underline decoration-dotted underline-offset-4">
                  Chapters
                </CardTitle>
              </Tip>
              <CardDescription>
                Titler tree — activity ▸ step. Click a step to inspect it.
              </CardDescription>
            </CardHeader>
            <CardContent className="p-2">
              <ScrollArea className="h-[300px] pr-2">
                <div className="space-y-0.5">
                  {activities.map((a) => {
                    const steps = evs.map((e, i) => ({ e, i })).filter((x) => x.e.seg === a.id);
                    const activeChapter = cur.seg === a.id;
                    const isOpen = open[a.id] ?? activeChapter;
                    return (
                      <div key={a.id}>
                        <button
                          onClick={() => setOpen((o) => ({ ...o, [a.id]: !isOpen }))}
                          className={`flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm transition-colors ${
                            activeChapter ? "bg-muted/60" : "hover:bg-muted/50"
                          }`}
                        >
                          <ChevronRight
                            className={`size-3.5 shrink-0 text-muted-foreground transition-transform ${
                              isOpen ? "rotate-90" : ""
                            }`}
                          />
                          <RiskDot level={a.risk} />
                          <span className="flex-1 truncate font-medium">{a.title}</span>
                          <span className="text-[11px] tabular-nums text-muted-foreground">
                            {steps.length}
                          </span>
                        </button>
                        {isOpen && (
                          <div className="ml-[15px] space-y-0.5 border-l border-border/70 py-0.5 pl-1.5">
                            {steps.map(({ e, i }) => (
                              <button
                                key={e.id}
                                onClick={() => setSel(i)}
                                className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left transition-colors ${
                                  i === sel ? "bg-muted" : "hover:bg-muted/40"
                                }`}
                              >
                                <span className="w-5 shrink-0 text-right text-[10.5px] tabular-nums text-muted-foreground">
                                  {i + 1}
                                </span>
                                <RiskDot level={e.risk} />
                                <span className="w-14 shrink-0 truncate font-mono text-[11px]">
                                  {e.tool.n}
                                </span>
                                <span className="flex-1 truncate text-[11.5px] text-muted-foreground">
                                  {e.summary}
                                </span>
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </ScrollArea>
            </CardContent>
          </Card>

          <Card className="overflow-hidden">
            <CardHeader>
              <Tip tip={G.enriched_events}>
                <CardTitle className="w-fit cursor-help text-base underline decoration-dotted underline-offset-4">
                  Timeline
                </CardTitle>
              </Tip>
              <CardDescription>{evs.length} enriched events.</CardDescription>
            </CardHeader>
            <CardContent className="p-0">
              <ScrollArea className="h-[360px]">
                <div className="divide-y divide-border/60">
                  {evs.map((e, i) => (
                    <button
                      key={e.id}
                      onClick={() => setSel(i)}
                      className={`flex w-full items-center gap-2.5 px-4 py-2 text-left transition-colors ${
                        i === sel ? "bg-muted" : "hover:bg-muted/40"
                      }`}
                    >
                      <span className="w-6 shrink-0 text-right text-[11px] tabular-nums text-muted-foreground">
                        {i + 1}
                      </span>
                      <RiskDot level={e.risk} />
                      <span className="w-16 shrink-0 truncate font-mono text-[11.5px]">
                        {e.tool.n}
                      </span>
                      <span className="flex-1 truncate text-[12.5px] text-muted-foreground">
                        {e.summary}
                      </span>
                      {e.action !== "allow" && <VerdictBadge v={e.action} />}
                    </button>
                  ))}
                </div>
              </ScrollArea>
            </CardContent>
          </Card>
        </div>

        <Inspector e={cur} idx={Math.min(sel, evs.length - 1)} />
      </div>
    </div>
  );
}

function Meta({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <div className="text-[10.5px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="text-[13px] tabular-nums">{value}</div>
    </div>
  );
}

function Inspector({ e, idx }: { e: TEvent; idx: number }) {
  const payload = useMemo(() => Object.entries(e.payload), [e.payload]);
  return (
    <Card className="h-fit">
      <CardHeader>
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <FileText className="size-4 text-muted-foreground" />
            <CardTitle className="font-mono text-base">{e.tool.n}</CardTitle>
            <span className="text-[11px] text-muted-foreground">event #{idx + 1}</span>
          </div>
          <div className="flex items-center gap-2">
            <RiskBadge level={e.risk} />
            <VerdictBadge v={e.action} />
          </div>
        </div>
        <CardDescription className="pt-1">{e.summary}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div>
          <Tip tip="Recommendation|The rules→assessor verdict and its reasoning. Governance stamps this on every event; enforcement (gating) is opt-in.">
            <div className="mb-1 w-fit cursor-help text-[11px] font-medium uppercase tracking-wide text-muted-foreground underline decoration-dotted underline-offset-2">
              Recommendation
            </div>
          </Tip>
          <div className={`risk-soft-${e.risk} rounded-md border px-3 py-2 text-[13px]`}>
            {e.reco.why}
          </div>
        </div>

        {e.ev && (
          <div className="space-y-2.5">
            <div className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
              Evidence
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Tip tip={mtip(e.ev.mitre)}>
                <span className="risk-soft-2 cursor-help rounded-md border px-2 py-0.5 font-mono text-[11px]">
                  {e.ev.mitre[0]} · {e.ev.mitre[1]}
                </span>
              </Tip>
              {e.ev.preds.map((p) => (
                <Pred key={p} p={p} />
              ))}
            </div>
            <div className="grid grid-cols-3 gap-3">
              <Meta label="PII" value={e.ev.pii} />
              <Meta label="Info-flow" value={e.ev.ifc} />
              <Meta
                label="Payload ptr"
                value={<span className="font-mono text-[11px]">{e.ev.ptr}</span>}
              />
            </div>
          </div>
        )}

        <Separator />

        <div className="grid grid-cols-3 gap-3">
          <Meta label="Phase" value={e.phase} />
          <Meta label="Turn" value={e.turn} />
          <Meta label="Retry" value={e.retry ? "yes" : "no"} />
          <Meta label="File" value={<span className="font-mono text-[11.5px]">{e.file}</span>} />
          <Meta label="Duration" value={`${e.dur} ms`} />
          <Meta label="Score" value={e.score.toFixed(2)} />
          <Meta label="Tokens" value={e.tokens.toLocaleString()} />
          <Meta label="Cost" value={money3(e.cost)} />
          <Meta
            label="Confidence"
            value={
              <Tip tip="Classification confidence|How sure the classifier is of the canonical tool identity + category.">
                <span className="cursor-help underline decoration-dotted underline-offset-2">
                  {(e.cls.conf * 100).toFixed(0)}%
                </span>
              </Tip>
            }
          />
        </div>

        <Separator />

        <div>
          <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
            Payload
          </div>
          <div className="rounded-md border border-border bg-muted/30 p-3 font-mono text-[11.5px]">
            {payload.map(([k, v]) => (
              <div key={k} className="flex gap-2">
                <span className="shrink-0 text-muted-foreground">{k}:</span>
                <span className="min-w-0 break-all">{String(v)}</span>
              </div>
            ))}
          </div>
        </div>

        {e.gap && (
          <Tip tip={G.context_gaps}>
            <div className="risk-soft-1 flex w-full cursor-help items-center gap-2 rounded-md border px-3 py-2 text-[12px]">
              <AlertTriangle className="size-3.5 shrink-0" />
              {e.gap}
            </div>
          </Tip>
        )}
      </CardContent>
    </Card>
  );
}
