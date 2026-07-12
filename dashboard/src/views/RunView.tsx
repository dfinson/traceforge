import { useMemo, useState } from "react";
import { AlertTriangle, ArrowLeft, ChevronRight, FileText, MessagesSquare } from "lucide-react";
import { useRuns, useTranscript } from "@/lib/queries";
import type { TEvent, TranscriptRole, TranscriptTurn } from "@/lib/types";
import { useApp } from "@/store";
import { dmin, hhmm, fmtCost, fmtVal, premiumReq } from "@/lib/format";
import { buildChapters, locateEvent } from "@/lib/chapters";
import { G, mtip } from "@/data/tips";
import { Tip } from "@/components/Tip";
import { RiskBadge, RiskDot } from "@/components/RiskBadge";
import { VerdictBadge, Pred } from "@/components/VerdictBadge";
import { RiskRibbon } from "@/components/charts/RiskRibbon";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";

export function RunView() {
  const { runId, sel, setSel, back } = useApp();
  const { data: runs = [], isLoading } = useRuns();
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const run = runId ? runs.find((r) => r.id === runId) : null;
  const chapters = useMemo(() => (run ? buildChapters(run.segs, run.events) : []), [run]);
  if (isLoading && !run) {
    return (
      <div className="flex h-64 items-center justify-center text-sm text-muted-foreground">
        Loading run…
      </div>
    );
  }
  if (!run) return null;
  const evs = run.events;
  const selIdx = Math.min(sel, evs.length - 1);
  const cur = evs[selIdx];
  const active = locateEvent(chapters, selIdx);
  const identity = [run.repo, run.agent, run.model].filter(Boolean).join(" · ");

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="truncate text-xl font-semibold tracking-tight">{run.title}</h1>
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12.5px] text-muted-foreground">
            {identity ? (
              <span>{identity}</span>
            ) : (
              <Tip tip={G.identity}>
                <span className="cursor-help underline decoration-dotted underline-offset-2">
                  identity unknown
                </span>
              </Tip>
            )}
            <span>·</span>
            <span>{hhmm(run.started)}</span>
            <span>·</span>
            <span>{dmin(run.durMs)}</span>
            <span>·</span>
            <span>{fmtCost(run.usage.cost)}</span>
            {run.usage.premiumRequests != null && (
              <>
                <span>·</span>
                <span>{premiumReq(run.usage.premiumRequests)}</span>
              </>
            )}
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
              One sliver per event, colored by risk. Click to inspect.
            </CardDescription>
          </div>
          <div className="flex items-center gap-4 text-[12px] text-muted-foreground">
            <span>
              peak <RiskBadge level={run.peak} />
            </span>
            <Tip tip={G.gov_meta}>
              <span className="cursor-help underline decoration-dotted underline-offset-2">
                drift {run.drift != null ? run.drift.toFixed(2) : "n/a"}
              </span>
            </Tip>
          </div>
        </CardHeader>
        <CardContent className="space-y-1">
          <RiskRibbon events={evs} sel={sel} onSelect={setSel} />
          <div className="flex justify-between text-[10.5px] text-muted-foreground">
            <span>{hhmm(evs[0].t)}</span>
            <span>
              {evs.length} events · {fmtCost(run.usage.cost)} total
              {run.usage.premiumRequests != null &&
                ` · ${premiumReq(run.usage.premiumRequests)}`}
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
                Titler tree — activity ▸ step ▸ event. Click an event to inspect it.
              </CardDescription>
            </CardHeader>
            <CardContent className="p-2">
              <ScrollArea className="h-[300px] pr-2">
                <div className="space-y-0.5">
                  {chapters.map((a) => {
                    const activityActive = a.key === active?.activityKey;
                    const activityOpen = open[a.key] ?? activityActive;
                    return (
                      <div key={a.key}>
                        <button
                          onClick={() => setOpen((o) => ({ ...o, [a.key]: !activityOpen }))}
                          className={`flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm transition-colors ${
                            activityActive ? "bg-muted/60" : "hover:bg-muted/50"
                          }`}
                        >
                          <ChevronRight
                            className={`size-3.5 shrink-0 text-muted-foreground transition-transform ${
                              activityOpen ? "rotate-90" : ""
                            }`}
                          />
                          <RiskDot level={a.risk} />
                          <span
                            className={`flex-1 truncate font-medium ${
                              a.synthetic ? "text-muted-foreground" : ""
                            }`}
                          >
                            {a.title}
                          </span>
                          <span className="text-[11px] tabular-nums text-muted-foreground">
                            {a.count}
                          </span>
                        </button>
                        {activityOpen && (
                          <div className="ml-[15px] space-y-0.5 border-l border-border/70 py-0.5 pl-1.5">
                            {a.steps.map((s) => {
                              const stepActive = s.key === active?.stepKey;
                              const stepOpen = open[s.key] ?? stepActive;
                              return (
                                <div key={s.key}>
                                  <button
                                    onClick={() => setOpen((o) => ({ ...o, [s.key]: !stepOpen }))}
                                    className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left transition-colors ${
                                      stepActive ? "bg-muted/60" : "hover:bg-muted/50"
                                    }`}
                                  >
                                    <ChevronRight
                                      className={`size-3 shrink-0 text-muted-foreground transition-transform ${
                                        stepOpen ? "rotate-90" : ""
                                      }`}
                                    />
                                    <RiskDot level={s.risk} />
                                    <span
                                      className={`flex-1 truncate text-[12.5px] ${
                                        s.synthetic ? "italic text-muted-foreground" : "font-medium"
                                      }`}
                                    >
                                      {s.title}
                                    </span>
                                    <span className="text-[10.5px] tabular-nums text-muted-foreground">
                                      {s.events.length}
                                    </span>
                                  </button>
                                  {stepOpen && (
                                    <div className="ml-[15px] space-y-0.5 border-l border-border/70 py-0.5 pl-1.5">
                                      {s.events.map(({ e, i }) => (
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
                            {a.steps.length === 0 && (
                              <div className="px-2 py-1.5 text-[11.5px] italic text-muted-foreground">
                                No steps recorded.
                              </div>
                            )}
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

        <Inspector e={cur} idx={selIdx} />
      </div>

      <TranscriptPanel runId={run.id} />
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
          <Meta label="Cost" value={fmtCost(e.cost)} />
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
                <span className="min-w-0 whitespace-pre-wrap break-all">{fmtVal(v)}</span>
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

// Per-role accent for the transcript's left rail + label, reusing the risk-soft
// tokens the rest of the console uses so the palette stays consistent.
const ROLE_STYLES: Record<TranscriptRole, { rail: string; label: string }> = {
  user: { rail: "border-l-primary", label: "text-primary" },
  assistant: { rail: "border-l-border", label: "text-foreground" },
  system: { rail: "border-l-muted-foreground/40", label: "text-muted-foreground" },
  tool: { rail: "border-l-muted-foreground/40", label: "text-muted-foreground" },
};

function TranscriptTurnRow({ turn }: { turn: TranscriptTurn }) {
  const style = ROLE_STYLES[turn.role];
  return (
    <div className={`border-l-2 ${style.rail} pl-3`}>
      <div className="flex items-baseline gap-2">
        <span className={`text-[11px] font-medium uppercase tracking-wide ${style.label}`}>
          {turn.role}
        </span>
        <span className="truncate font-mono text-[11px] text-muted-foreground">{turn.label}</span>
        <span className="ml-auto shrink-0 text-[10.5px] tabular-nums text-muted-foreground">
          {hhmm(new Date(turn.t))}
        </span>
      </div>
      {turn.text ? (
        <p className="mt-1 whitespace-pre-wrap break-words text-[12.5px] leading-relaxed">
          {turn.text}
        </p>
      ) : (
        <p className="mt-1 text-[12px] italic text-muted-foreground">No text captured.</p>
      )}
    </div>
  );
}

// Collapsible full-width panel rendering the run's full-text transcript. The
// transcript is fetched lazily (only once opened) via useTranscript so the
// potentially large bodies never load for readers who stay on the timeline.
function TranscriptPanel({ runId }: { runId: string }) {
  const [open, setOpen] = useState(false);
  const { data, isLoading, isError } = useTranscript(runId, open);
  const turns = data?.turns ?? [];

  return (
    <Card className="overflow-hidden">
      <CardHeader className="flex-row items-center justify-between gap-3 space-y-0">
        <div className="flex items-center gap-2">
          <MessagesSquare className="size-4 text-muted-foreground" />
          <div>
            <CardTitle className="text-base">Transcript</CardTitle>
            <CardDescription>The run’s full text — messages and tool calls, in order.</CardDescription>
          </div>
        </div>
        <Button variant="outline" size="sm" onClick={() => setOpen((o) => !o)} className="shrink-0">
          <ChevronRight className={`size-4 transition-transform ${open ? "rotate-90" : ""}`} />
          {open ? "Hide" : "Show"}
        </Button>
      </CardHeader>
      {open && (
        <CardContent className="p-0">
          {isLoading ? (
            <div className="px-4 py-6 text-sm text-muted-foreground">Loading transcript…</div>
          ) : isError ? (
            <div className="px-4 py-6 text-sm text-muted-foreground">
              Could not load the transcript for this run.
            </div>
          ) : turns.length === 0 ? (
            <div className="px-4 py-6 text-sm text-muted-foreground">
              No transcript recorded for this run.
            </div>
          ) : (
            <ScrollArea className="h-[420px]">
              <div className="space-y-4 px-4 py-3">
                {turns.map((turn) => (
                  <TranscriptTurnRow key={turn.id} turn={turn} />
                ))}
              </div>
            </ScrollArea>
          )}
        </CardContent>
      )}
    </Card>
  );
}
