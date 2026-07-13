// Shared render helper for view/component tests.
//
// jsdom provides neither `ResizeObserver` (recharts' ResponsiveContainer measures
// with it), `matchMedia`, nor `Element.scrollIntoView` (RunView's transcript
// scrolls the selected turn into view); without stubs the chart-bearing views and
// the transcript throw on mount. The views also assume the app-level
// <TooltipProvider> ancestor (App.tsx), so `renderView` supplies one — matching
// production wiring without pulling in the full <App> shell.

import type { ReactElement } from "react";
import { render } from "@testing-library/react";
import { TooltipProvider } from "@/components/ui/tooltip";

class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

const g = globalThis as unknown as {
  ResizeObserver?: unknown;
  matchMedia?: unknown;
};

g.ResizeObserver ??= ResizeObserverStub;

if (typeof g.matchMedia !== "function") {
  g.matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() {
      return false;
    },
  });
}

if (typeof Element !== "undefined" && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function scrollIntoView() {};
}

export function renderView(ui: ReactElement) {
  return render(<TooltipProvider>{ui}</TooltipProvider>);
}
