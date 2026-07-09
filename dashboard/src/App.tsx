import { useEffect } from "react";
import { TooltipProvider } from "@/components/ui/tooltip";
import { AppProvider, useApp } from "@/store";
import { useHealth } from "@/lib/queries";
import { Sidebar } from "@/components/Sidebar";
import { Topbar } from "@/components/Topbar";
import { NoOutputDb } from "@/components/NoOutputDb";
import { Fleet } from "@/views/Fleet";
import { RunView } from "@/views/RunView";
import { Triage } from "@/views/Triage";
import { Cost } from "@/views/Cost";
import { Coverage } from "@/views/Coverage";

function Body() {
  const { view, sysdbTouched, syncSysdb } = useApp();
  const { data: health } = useHealth();

  // Auto-detect the governance-memory lens from the real data source, unless the
  // user has manually overridden it (spec fork 1: auto-detect + keep override).
  useEffect(() => {
    if (health && !sysdbTouched) syncSysdb(health.has_system_memory);
  }, [health, sysdbTouched, syncSysdb]);

  const noOutput = health != null && !health.has_output_db;

  return (
    <div className="flex h-screen w-full overflow-hidden bg-background text-foreground">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <Topbar />
        <main className="flex-1 overflow-y-auto">
          {noOutput ? (
            <NoOutputDb health={health} />
          ) : (
            <div className="mx-auto max-w-[1200px] px-6 py-6">
              {view === "fleet" && <Fleet />}
              {view === "run" && <RunView />}
              {view === "triage" && <Triage />}
              {view === "cost" && <Cost />}
              {view === "coverage" && <Coverage />}
            </div>
          )}
        </main>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <AppProvider>
      <TooltipProvider delayDuration={120} skipDelayDuration={300}>
        <Body />
      </TooltipProvider>
    </AppProvider>
  );
}
