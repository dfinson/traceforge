import { useEffect, useMemo, useState } from "react";

import { NewTicketForm } from "./components/NewTicketForm";
import { TicketTable } from "./components/TicketTable";
import { seedTickets } from "./lib/seedData";
import type { Ticket, TicketStatus } from "./types";

const statusOptions: Array<{ label: string; value: TicketStatus | "all" }> = [
  { label: "All", value: "all" },
  { label: "Open", value: "open" },
  { label: "Investigating", value: "investigating" },
  { label: "Waiting", value: "waiting" },
  { label: "Resolved", value: "resolved" },
];

export default function App() {
  const [tickets, setTickets] = useState<Ticket[]>(seedTickets);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<TicketStatus | "all">("all");
  const [darkMode, setDarkMode] = useState(() => localStorage.getItem("darkMode") === "true");

  useEffect(() => {
    document.body.classList.toggle("dark", darkMode);
    localStorage.setItem("darkMode", String(darkMode));
  }, [darkMode]);

  const visibleTickets = useMemo(() => {
    return tickets.filter((ticket) => {
      const matchesStatus = statusFilter === "all" ? true : ticket.status === statusFilter;
      const q = query.trim().toLowerCase();
      const matchesQuery = q
        ? ticket.subject.toLowerCase().includes(q) ||
          ticket.customerEmail.toLowerCase().includes(q)
        : true;

      return matchesStatus && matchesQuery;
    });
  }, [query, statusFilter, tickets]);

  return (
    <main className="app-shell">
      <nav className="top-nav panel">
        <span className="nav-title">Support Dashboard</span>
        <button
          className="dark-mode-toggle"
          onClick={() => setDarkMode((prev) => !prev)}
          aria-label="Toggle dark mode"
        >
          {darkMode ? "☀️ Light" : "🌙 Dark"}
        </button>
      </nav>

      <section className="hero-bar panel">
        <div>
          <p className="eyebrow">Customer success workspace</p>
          <h1>Support operations dashboard</h1>
          <p>
            Track urgent tickets, keep enterprise accounts moving, and coordinate the next response.
          </p>
        </div>
        <div className="hero-stats">
          <div>
            <strong>18</strong>
            <span>open tickets</span>
          </div>
          <div>
            <strong>4</strong>
            <span>high priority</span>
          </div>
          <div>
            <strong>94%</strong>
            <span>sla hit rate</span>
          </div>
        </div>
      </section>

      <section className="controls panel">
        <div className="control-block">
          <label htmlFor="search">Search tickets</label>
          <input
            id="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search by subject or email"
          />
        </div>

        <div className="control-block">
          <label htmlFor="status-filter">Status</label>
          <select
            id="status-filter"
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value as TicketStatus | "all")}
          >
            {statusOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
      </section>

      <section className="content-grid">
        <TicketTable tickets={visibleTickets} />
        <NewTicketForm onCreate={(ticket) => setTickets((current) => [ticket, ...current])} />
      </section>
    </main>
  );
}
