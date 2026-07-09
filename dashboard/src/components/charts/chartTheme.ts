// Shared Recharts theming — all colors resolve to the de-neon TraceForge tokens
// defined in index.css, so charts track the palette automatically.

export const axisTick = { fill: "var(--muted-foreground)", fontSize: 11 };
export const gridStroke = "var(--border)";

export const tooltipStyle: React.CSSProperties = {
  background: "var(--popover)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  color: "var(--popover-foreground)",
  fontSize: 12,
  padding: "8px 10px",
  boxShadow: "0 6px 22px rgba(0,0,0,.35)",
};

export const tooltipLabelStyle: React.CSSProperties = {
  color: "var(--foreground)",
  fontWeight: 600,
  marginBottom: 2,
};

export const tooltipItemStyle: React.CSSProperties = {
  color: "var(--muted-foreground)",
};

export const RISK_FILL = ["#22c55e", "#f59e0b", "#f97316", "#ef4444"];

// Categorical ramp echoing the CodePlane analytics palette (indigo → orange).
export const CHART_FILL = ["#6366f1", "#8b5cf6", "#60a5fa", "#34d399", "#fbbf24", "#fb923c"];
