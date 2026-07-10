import { ChevronRight } from "lucide-react";
import { useRuns } from "@/lib/queries";
import { useApp } from "@/store";

const VIEW_LABEL: Record<string, string> = {
  fleet: "Fleet",
  triage: "Triage",
  cost: "Cost",
  coverage: "Coverage",
};

export function Topbar() {
  const { view, runId, back } = useApp();
  const { data: runs = [] } = useRuns();
  const run = runId ? runs.find((r) => r.id === runId) : null;
  return (
    <header className="flex h-14 shrink-0 items-center justify-between border-b border-border bg-background/80 px-6 backdrop-blur">
      <div className="flex items-center gap-1.5 text-sm">
        {view === "run" && run ? (
          <>
            <button
              onClick={back}
              className="text-muted-foreground transition-colors hover:text-foreground"
            >
              Fleet
            </button>
            <ChevronRight className="size-3.5 text-muted-foreground/60" />
            <span className="max-w-[42ch] truncate font-medium">{run.title}</span>
            <span className="ml-1 rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
              {run.id}
            </span>
          </>
        ) : (
          <span className="font-medium">{VIEW_LABEL[view] ?? "Fleet"}</span>
        )}
      </div>
    </header>
  );
}
