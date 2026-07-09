import { LayoutGrid, ShieldAlert, Receipt, Gauge } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { RUNS } from "@/data/runs";
import { useApp } from "@/store";
import type { View } from "@/store";
import { DataSourceToggle } from "./DataSourceToggle";
import { Separator } from "@/components/ui/separator";

const HIGH_RISK = RUNS.flatMap((r) => r.events).filter((e) => e.risk >= 2).length;

function NavItem({
  icon: Icon,
  label,
  active,
  onClick,
  badge,
}: {
  icon: LucideIcon;
  label: string;
  active: boolean;
  onClick: () => void;
  badge?: number;
}) {
  return (
    <button
      onClick={onClick}
      className={`flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors ${
        active
          ? "bg-sidebar-accent font-medium text-sidebar-accent-foreground"
          : "text-muted-foreground hover:bg-sidebar-accent/50 hover:text-foreground"
      }`}
    >
      <Icon className="size-4 shrink-0" strokeWidth={2} />
      <span className="flex-1 text-left">{label}</span>
      {badge ? (
        <span className="risk-soft-2 rounded-full border px-1.5 text-[10.5px] font-semibold tabular-nums">
          {badge}
        </span>
      ) : null}
    </button>
  );
}

export function Sidebar() {
  const { view, setView, back } = useApp();
  const fleetActive = view === "fleet" || view === "run";
  const go = (v: View) => (v === "fleet" ? back() : setView(v));
  return (
    <aside className="flex h-full w-60 shrink-0 flex-col border-r border-sidebar-border bg-sidebar">
      <div className="flex items-center gap-2.5 px-4 py-4">
        <img src="/logo.png" alt="" className="size-7 rounded" />
        <div className="leading-tight">
          <div className="text-[15px] font-semibold tracking-tight">TraceForge</div>
          <div className="text-[10.5px] uppercase tracking-[0.14em] text-muted-foreground">
            Console
          </div>
        </div>
      </div>
      <Separator />
      <nav className="flex flex-1 flex-col gap-0.5 p-2.5">
        <div className="px-2 pb-1 pt-2 text-[10px] font-medium uppercase tracking-wider text-muted-foreground/70">
          Overview
        </div>
        <NavItem
          icon={LayoutGrid}
          label="Fleet"
          active={fleetActive}
          onClick={() => go("fleet")}
        />
        <NavItem
          icon={ShieldAlert}
          label="Triage"
          active={view === "triage"}
          onClick={() => go("triage")}
          badge={HIGH_RISK}
        />
        <div className="px-2 pb-1 pt-4 text-[10px] font-medium uppercase tracking-wider text-muted-foreground/70">
          Fleet lenses
        </div>
        <NavItem icon={Receipt} label="Cost" active={view === "cost"} onClick={() => go("cost")} />
        <NavItem
          icon={Gauge}
          label="Coverage"
          active={view === "coverage"}
          onClick={() => go("coverage")}
        />
      </nav>
      <Separator />
      <div className="p-3">
        <DataSourceToggle />
      </div>
    </aside>
  );
}
