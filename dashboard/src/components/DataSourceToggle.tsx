import { Database, Cpu } from "lucide-react";
import { useApp } from "@/store";
import { useHealth } from "@/lib/queries";
import { G } from "@/data/tips";
import { Tip } from "./Tip";

export function DataSourceToggle() {
  const { sysdb, sysdbTouched, setSysdb, resetSysdb } = useApp();
  const { data: health } = useHealth();
  const detected = health?.has_system_memory;
  const overridden = detected !== undefined && sysdbTouched && sysdb !== detected;

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between">
        <Tip tip={G.sysdb} side="right">
          <span className="w-fit cursor-help text-[10.5px] font-medium uppercase tracking-wide text-muted-foreground underline decoration-dotted decoration-muted-foreground/40 underline-offset-2">
            Data source
          </span>
        </Tip>
        {detected !== undefined ? (
          <span className="text-[9px] font-medium uppercase tracking-wider text-muted-foreground/60">
            {overridden ? "override" : "auto"}
          </span>
        ) : null}
      </div>
      <div className="flex rounded-lg border border-border bg-muted/30 p-0.5 text-[12px]">
        <button
          onClick={() => setSysdb(true)}
          className={`flex flex-1 items-center justify-center gap-1.5 rounded-md px-2 py-1.5 transition-colors ${
            sysdb
              ? "bg-card text-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground"
          }`}
        >
          <Database className="size-3.5" />
          CLI
        </button>
        <button
          onClick={() => setSysdb(false)}
          className={`flex flex-1 items-center justify-center gap-1.5 rounded-md px-2 py-1.5 transition-colors ${
            !sysdb
              ? "bg-card text-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground"
          }`}
        >
          <Cpu className="size-3.5" />
          SDK
        </button>
      </div>
      <span className="text-[10.5px] leading-snug text-muted-foreground">
        {detected === undefined ? (
          "Detecting data source…"
        ) : sysdb ? (
          <>
            <code className="text-[10px]">system.db</code> present — full governance memory.
          </>
        ) : (
          "SDK-embed — drift, taint & trust unavailable."
        )}
        {overridden ? (
          <>
            {" · "}
            <button
              onClick={resetSysdb}
              className="underline decoration-dotted underline-offset-2 transition-colors hover:text-foreground"
            >
              reset to detected
            </button>
          </>
        ) : null}
      </span>
    </div>
  );
}
