import { TooltipProvider } from "@/components/ui/tooltip";
import { AppProvider, useApp } from "@/store";
import { Sidebar } from "@/components/Sidebar";
import { Topbar } from "@/components/Topbar";
import { Fleet } from "@/views/Fleet";
import { RunView } from "@/views/RunView";
import { Triage } from "@/views/Triage";
import { Cost } from "@/views/Cost";
import { Coverage } from "@/views/Coverage";

function Body() {
  const { view } = useApp();
  return (
    <div className="flex h-screen w-full overflow-hidden bg-background text-foreground">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <Topbar />
        <main className="flex-1 overflow-y-auto">
          <div className="mx-auto max-w-[1200px] px-6 py-6">
            {view === "fleet" && <Fleet />}
            {view === "run" && <RunView />}
            {view === "triage" && <Triage />}
            {view === "cost" && <Cost />}
            {view === "coverage" && <Coverage />}
          </div>
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
