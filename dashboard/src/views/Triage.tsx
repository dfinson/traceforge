import { useMemo, useState } from "react";
import { ShieldOff, ArrowRight, ChevronDown, ShieldAlert, AlertTriangle } from "lucide-react";
import type { TEvent } from "@/lib/types";
import { useRuns } from "@/lib/queries";
import { useApp } from "@/store";
import { G } from "@/data/tips";
import { Tip } from "@/components/Tip";
import { RiskBadge } from "@/components/RiskBadge";
import { VerdictBadge } from "@/components/VerdictBadge";
import { RiskByAgent } from "@/components/charts/RiskByAgent";
import { TechniqueBars } from "@/components/charts/TechniqueBars";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

const RISK_VAR = (l: number) => `var(--risk-${l})`;

type QRow = { runId: string; title: string; idx: number; e: TEvent };

export function Triage() {
  const { openEvent } = useApp();
  const { data: runs = [], isLoading } = useRuns();

  const queue = useMemo<QRow[]>(() => {
    const q: QRow[] = [];
    runs.forEach((r) =>
      r.events.forEach((e, idx) => {
        if (e.risk >= 2) q.push({ runId: r.id, title: r.title, idx, e });
      })
    );
    return q.sort((a, b) => b.e.score - a.e.score);
  }, [runs]);

  const mem = useMemo(() => {
    const taint = runs.flatMap((r) => r.taint.map((t) => ({ ...t, run: r.title })));
    const trust = runs.flatMap((r) => r.trust.map((t) => ({ ...t, run: r.title })));
    const mcp = runs.flatMap((r) => r.mcp.map((m) => ({ ...m, run: r.title })));
    return { taint, trust, mcp };
  }, [runs]);

  // Presence, not entry point: show the governance-memory cards when any of the
  // three surfaces actually has rows, else the honest empty state below.
  const hasMem = mem.taint.length > 0 || mem.trust.length > 0 || mem.mcp.length > 0;

  if (isLoading) {
    return (
      <div className="flex h-64 items-center justify-center text-sm text-muted-foreground">
        Loading triage…
      </div>
    );
  }

  const crit = queue.filter((q) => q.e.risk === 3);
  const danger = queue.filter((q) => q.e.risk === 2);

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Triage</h1>
        <p className="text-sm text-muted-foreground">
          Every danger- and critical-risk event across the fleet, worst first. Click to jump into the run.
        </p>
      </div>

      <div className="space-y-3">
        <Bucket
          level={3}
          label="Critical"
          hint="deny / destructive — act now"
          rows={crit}
          openEvent={openEvent}
        />
        <Bucket
          level={2}
          label="Danger"
          hint="escalated for review"
          rows={danger}
          openEvent={openEvent}
        />
      </div>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Risk by agent</CardTitle>
            <CardDescription>Where risky calls concentrate, by agent.</CardDescription>
          </CardHeader>
          <CardContent>
            <RiskByAgent />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="text-base">ATT&CK techniques</CardTitle>
            <CardDescription>Top MITRE techniques matched by the evidence chain.</CardDescription>
          </CardHeader>
          <CardContent>
            <TechniqueBars />
          </CardContent>
        </Card>
      </div>

      {hasMem ? (
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-3">
          <MemCard title="Taint ledger" tip={G.govMemory} desc="Untrusted → sink information flows.">
            {mem.taint.length ? (
              mem.taint.map((t, i) => (
                <MemRow key={i} lvl={t.lvl} head={t.flow} sub={`${t.det} · ${t.run}`} />
              ))
            ) : (
              <Empty>No taint flows recorded.</Empty>
            )}
          </MemCard>
          <MemCard title="Trust grants" tip={G.govMemory} desc="Active TTL-bound trust for sources.">
            {mem.trust.length ? (
              mem.trust.map((t, i) => (
                <MemRow key={i} lvl={t.lvl} head={t.who} sub={`${t.ttl} · ${t.run}`} />
              ))
            ) : (
              <Empty>No trust grants active.</Empty>
            )}
          </MemCard>
          <MemCard title="MCP drift" tip={G.govMemory} desc="Changes in MCP server tool surface.">
            {mem.mcp.length ? (
              mem.mcp.map((m, i) => (
                <MemRow key={i} lvl={m.lvl} head={m.srv} sub={`${m.msg} · ${m.run}`} />
              ))
            ) : (
              <Empty>No MCP drift detected.</Empty>
            )}
          </MemCard>
        </div>
      ) : (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center gap-2 py-10 text-center">
            <ShieldOff className="size-7 text-muted-foreground/60" />
            <div className="text-sm font-medium">No governance memory recorded</div>
            <p className="max-w-md text-[12.5px] text-muted-foreground">
              Taint ledger, trust grants and MCP drift appear here once TraceForge has recorded
              them for these runs. None has been recorded yet — the risk queue above is unaffected.
            </p>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function MemCard({
  title,
  desc,
  tip,
  children,
}: {
  title: string;
  desc: string;
  tip: string;
  children: React.ReactNode;
}) {
  return (
    <Card>
      <CardHeader>
        <Tip tip={tip}>
          <CardTitle className="w-fit cursor-help text-base underline decoration-dotted underline-offset-4">
            {title}
          </CardTitle>
        </Tip>
        <CardDescription>{desc}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">{children}</CardContent>
    </Card>
  );
}

function MemRow({ lvl, head, sub }: { lvl: number; head: string; sub: string }) {
  return (
    <div className="flex items-start gap-2.5">
      <span
        className="mt-1 size-2 shrink-0 rounded-full"
        style={{ background: RISK_VAR(lvl) }}
      />
      <div className="min-w-0">
        <div className="text-[12.5px] font-medium">{head}</div>
        <div className="truncate text-[11px] text-muted-foreground">{sub}</div>
      </div>
    </div>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="py-3 text-[12px] text-muted-foreground">{children}</div>;
}

function Bucket({
  level,
  label,
  hint,
  rows,
  openEvent,
}: {
  level: 2 | 3;
  label: string;
  hint: string;
  rows: QRow[];
  openEvent: (runId: string, idx: number) => void;
}) {
  const [open, setOpen] = useState(true);
  const Icon = level === 3 ? ShieldAlert : AlertTriangle;
  return (
    <Card className="overflow-hidden py-0">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2.5 px-4 py-3 text-left transition-colors hover:bg-muted/30"
      >
        <Icon className="size-4 shrink-0" style={{ color: RISK_VAR(level) }} />
        <span className="text-sm font-semibold">{label}</span>
        <span className="rounded-full bg-muted px-1.5 py-0.5 text-[11px] font-medium tabular-nums text-muted-foreground">
          {rows.length}
        </span>
        <span className="truncate text-[12px] text-muted-foreground">{hint}</span>
        <ChevronDown
          className={`ml-auto size-4 shrink-0 text-muted-foreground transition-transform ${
            open ? "" : "-rotate-90"
          }`}
        />
      </button>
      {open &&
        (rows.length ? (
          <div className="divide-y divide-border/60 border-t border-border/60">
            {rows.map((r) => (
              <QueueRow key={`${r.runId}-${r.e.id}`} row={r} openEvent={openEvent} />
            ))}
          </div>
        ) : (
          <div className="border-t border-border/60 px-4 py-4 text-[12px] text-muted-foreground">
            Nothing at this level.
          </div>
        ))}
    </Card>
  );
}

function QueueRow({
  row,
  openEvent,
}: {
  row: QRow;
  openEvent: (runId: string, idx: number) => void;
}) {
  const { runId, title, idx, e } = row;
  return (
    <button
      onClick={() => openEvent(runId, idx)}
      className="group flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors hover:bg-muted/40"
    >
      <span className="h-8 w-1 shrink-0 rounded-full" style={{ background: RISK_VAR(e.risk) }} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="font-mono text-[11.5px]">{e.tool.n}</span>
          <span className="truncate text-[12.5px] text-muted-foreground">{e.summary}</span>
        </div>
        <div className="truncate text-[11px] text-muted-foreground/80">{title}</div>
      </div>
      <span className="hidden shrink-0 text-[11px] tabular-nums text-muted-foreground sm:inline">
        score {e.score.toFixed(2)}
      </span>
      <RiskBadge level={e.risk} />
      <VerdictBadge v={e.action} />
      <ArrowRight className="size-4 shrink-0 text-muted-foreground/40 transition-transform group-hover:translate-x-0.5 group-hover:text-muted-foreground" />
    </button>
  );
}
