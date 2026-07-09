import { createContext, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";
import type { Dim } from "@/lib/format";

export type View = "fleet" | "run" | "triage" | "cost" | "coverage";
export type SortKey = "recent" | "risk" | "cost";

export interface AppApi {
  view: View;
  runId: string | null;
  sel: number;
  dim: Dim;
  filt: string;
  sort: SortKey;
  sysdb: boolean;
  setView: (v: View) => void;
  openRun: (id: string) => void;
  openEvent: (id: string, idx: number) => void;
  back: () => void;
  setSel: (i: number) => void;
  setDim: (d: Dim) => void;
  setFilt: (s: string) => void;
  setSort: (s: SortKey) => void;
  setSysdb: (b: boolean) => void;
}

const Ctx = createContext<AppApi | null>(null);

export function useApp(): AppApi {
  const c = useContext(Ctx);
  if (!c) throw new Error("useApp must be used within <AppProvider>");
  return c;
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [view, setView] = useState<View>("fleet");
  const [runId, setRunId] = useState<string | null>(null);
  const [sel, setSel] = useState(0);
  const [dim, setDim] = useState<Dim>("phase");
  const [filt, setFilt] = useState("");
  const [sort, setSort] = useState<SortKey>("recent");
  const [sysdb, setSysdb] = useState(true);

  const api = useMemo<AppApi>(
    () => ({
      view,
      runId,
      sel,
      dim,
      filt,
      sort,
      sysdb,
      setView,
      openRun: (id) => {
        setRunId(id);
        setSel(0);
        setView("run");
      },
      openEvent: (id, idx) => {
        setRunId(id);
        setSel(idx);
        setView("run");
      },
      back: () => setView("fleet"),
      setSel,
      setDim,
      setFilt,
      setSort,
      setSysdb,
    }),
    [view, runId, sel, dim, filt, sort, sysdb]
  );

  return <Ctx.Provider value={api}>{children}</Ctx.Provider>;
}
