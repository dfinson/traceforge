import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { renderView } from "@/test/harness";
import { fullyClassifiedEvents, makeRun, makeTranscript, seedRuns } from "@/test/fixtures";
import { useRuns, useTranscript } from "@/lib/queries";
import { Fleet } from "@/views/Fleet";
import { RunView } from "@/views/RunView";
import { Cost } from "@/views/Cost";
import { Coverage } from "@/views/Coverage";
import { Triage } from "@/views/Triage";
import type { Run, Transcript } from "@/lib/types";

// The views read their data through `useRuns()` / `useTranscript()` and their
// navigation state through `useApp()`. Mock both so each view renders against an
// in-memory seed with no live server, React Query cache, or fetch.
const store = vi.hoisted(() => {
  const noop = () => {};
  return {
    app: {
      view: "fleet",
      runId: null as string | null,
      sel: 0,
      filt: "",
      sort: "recent",
      setView: noop,
      openRun: noop,
      openEvent: noop,
      back: noop,
      setSel: noop,
      setFilt: noop,
      setSort: noop,
    },
  };
});

vi.mock("@/store", () => ({ useApp: () => store.app }));
vi.mock("@/lib/queries", () => ({
  useRuns: vi.fn(),
  useTranscript: vi.fn(),
  useHealth: vi.fn(),
}));

const mockedUseRuns = vi.mocked(useRuns);
const mockedUseTranscript = vi.mocked(useTranscript);

function setRuns(runs: Run[], isLoading = false) {
  mockedUseRuns.mockReturnValue({
    data: runs,
    isLoading,
    isError: false,
    error: null,
    refetch: () => {},
  } as unknown as ReturnType<typeof useRuns>);
}

function setTranscript(transcript: Transcript, isLoading = false) {
  mockedUseTranscript.mockReturnValue({
    data: transcript,
    isLoading,
    isError: false,
    error: null,
  } as unknown as ReturnType<typeof useTranscript>);
}

beforeEach(() => {
  store.app.view = "fleet";
  store.app.runId = null;
  store.app.sel = 0;
  store.app.filt = "";
  store.app.sort = "recent";
  setRuns(seedRuns());
  setTranscript(makeTranscript("run-a"));
});

describe("Fleet", () => {
  it("mounts on seed data and lists a row per run", () => {
    renderView(<Fleet />);

    expect(screen.getByRole("heading", { name: "Fleet" })).toBeInTheDocument();
    expect(screen.getByText("Refactor the auth module")).toBeInTheDocument();
    expect(screen.getByText("Fix the flaky title test")).toBeInTheDocument();
    // "2 of 2 shown" run count.
    expect(screen.getByText(/2 of 2 shown/)).toBeInTheDocument();
  });

  it("scores the Classified KPI over classifiable events, not all events", () => {
    // 3 fully-classified tool events (conf >= 0.9) + 3 lifecycle events. Dividing
    // by ALL events would read 50%; the honest classifiable denominator reads 100%.
    const run = makeRun({ id: "fc", title: "Fully classified", events: fullyClassifiedEvents() });
    setRuns([run]);

    const { container } = renderView(<Fleet />);

    const label = screen.getByText("Classified");
    const card = label.closest("[data-slot='card']");
    expect(card).toHaveTextContent("100%");
    // The buggy all-events framing would have surfaced 50% for this fixture.
    expect(container.textContent).not.toMatch(/50\s*%/);
  });
});

describe("RunView", () => {
  beforeEach(() => {
    store.app.runId = "run-a";
    store.app.sel = 0;
  });

  it("mounts the opened run with its chapters, timeline and inspector", () => {
    renderView(<RunView />);

    expect(screen.getByRole("heading", { name: "Refactor the auth module" })).toBeInTheDocument();
    expect(screen.getByText("Chapters")).toBeInTheDocument();
    // Titler tree: activity + step titles from the seed.
    expect(screen.getByText("Explore the repository")).toBeInTheDocument();
    expect(screen.getByText("Read the source files")).toBeInTheDocument();
    expect(screen.getByText("Timeline")).toBeInTheDocument();
    expect(screen.getByText(/6 enriched events/)).toBeInTheDocument();
  });

  it("reveals the full-text transcript when expanded", () => {
    renderView(<RunView />);

    expect(screen.getByText("Transcript")).toBeInTheDocument();
    // Transcript is lazy — its turns render only once shown.
    expect(
      screen.queryByText("Please refactor the auth module for clarity."),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /show/i }));

    expect(
      screen.getByText("Please refactor the auth module for clarity."),
    ).toBeInTheDocument();
  });
});

describe("Cost", () => {
  it("shows AIU as the primary signal and the premium-request count as secondary", () => {
    const { container } = renderView(<Cost />);

    const aiuKpi = screen.getAllByText("AI credits")[0];
    const premiumKpi = screen.getByText("Premium requests");
    expect(aiuKpi).toBeInTheDocument();
    expect(premiumKpi).toBeInTheDocument();
    // Primary before secondary in document order.
    expect(
      aiuKpi.compareDocumentPosition(premiumKpi) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    // Primary AIU total (40 + 26.5964 AIU) and secondary premium count.
    expect(container.textContent).toMatch(/66\.6 AIU/);
    expect(premiumKpi.closest("[data-slot='card']")).toHaveTextContent("2");
  });

  it("renders unknown per-model counts as an em dash, never a fabricated 0", () => {
    renderView(<Cost />);

    // The blank-model run surfaces as an "unknown model" row with unknown counts.
    expect(screen.getByText("unknown model")).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });
});

describe("Coverage", () => {
  it("mounts on seed data and scores over the classifiable denominator", () => {
    const { container } = renderView(<Coverage />);

    expect(screen.getByRole("heading", { name: "Coverage" })).toBeInTheDocument();
    expect(screen.getByText("Classification spread")).toBeInTheDocument();
    // 5 classifiable events across the seed (4 tool + 1 permission), NOT the 8
    // total events — the honest denominator surfaced in the view.
    expect(container.textContent).toMatch(/of 5 classifiable events/);
    // Recorded context gap from the seed.
    expect(screen.getByText("output sink restarted mid-run")).toBeInTheDocument();
  });

  it("shows 100% coverage for a fully-classified tool fixture (denominator regression)", () => {
    const run = makeRun({ id: "fc", title: "Fully classified", events: fullyClassifiedEvents() });
    setRuns([run]);

    const { container } = renderView(<Coverage />);

    expect(screen.getByText("Classification spread")).toBeInTheDocument();
    expect(container.textContent).toMatch(/100\s*%/);
    // If lifecycle events had leaked into the denominator this would read 50%.
    expect(container.textContent).not.toMatch(/50\s*%/);
  });
});

describe("Triage", () => {
  it("lists danger- and critical-risk events worst first", () => {
    renderView(<Triage />);

    expect(screen.getByRole("heading", { name: "Triage" })).toBeInTheDocument();
    expect(screen.getByText("Critical")).toBeInTheDocument();
    expect(screen.getByText("Danger")).toBeInTheDocument();
    // The critical (risk 3) and danger (risk 2) events from the seed.
    expect(screen.getByText("delete the build directory")).toBeInTheDocument();
    expect(screen.getByText("outbound POST to an unrecognized host")).toBeInTheDocument();
    // Governance memory surfaced from the run's taint ledger.
    expect(screen.getByText(/web content → shell arg/)).toBeInTheDocument();
  });
});
