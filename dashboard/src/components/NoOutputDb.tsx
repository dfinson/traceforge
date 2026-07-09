import { DatabaseZap } from "lucide-react";
import type { Health } from "@/lib/api";

/**
 * Global empty state shown when the SQLite output sink DB is absent — with no
 * output DB every view is empty, so we replace the whole content area with a
 * single one-liner explaining how to turn the sink on. Detected from
 * `/api/health` (`has_output_db === false`).
 */
export function NoOutputDb({ health }: { health: Health }) {
  return (
    <div className="flex h-full items-center justify-center px-6">
      <div className="max-w-md text-center">
        <div className="mx-auto mb-4 flex size-12 items-center justify-center rounded-xl border border-border bg-muted/30">
          <DatabaseZap className="size-6 text-muted-foreground" />
        </div>
        <h2 className="text-base font-semibold tracking-tight">No trace data yet</h2>
        <p className="mt-1.5 text-[13px] leading-relaxed text-muted-foreground">
          The dashboard reads from TraceForge's SQLite output sink, but none was found at
        </p>
        <code className="mt-2 inline-block rounded bg-muted px-2 py-1 font-mono text-[11.5px] text-muted-foreground">
          {health.output_db ?? "~/.traceforge/traceforge.db"}
        </code>
        <p className="mt-3 text-[12.5px] leading-relaxed text-muted-foreground">
          Enable the <code className="text-[11.5px]">sqlite</code> sink in your TraceForge config
          (or run a session with it on), then reload. Point at a different file with{" "}
          <code className="text-[11.5px]">traceforge dashboard --output-db &lt;path&gt;</code>.
        </p>
      </div>
    </div>
  );
}
