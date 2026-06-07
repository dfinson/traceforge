# tracemill

*Agent event observation pipeline with pluggable storage backends.*

Mills raw agent traces into structured output.

---

## §1 — What It Is

A standalone Python library that observes AI agent sessions and routes structured events to pluggable storage backends. It is the observation-to-storage pipeline — the plumbing layer between "agent did something" and "that knowledge lives somewhere useful."

The library doesn't decide what to do with agent events. It parses them, enriches them, and delivers them to sinks that consumers provide. Known consumers:

- **CodePlane** routes events to SQLite + OTEL for its control plane UI.
- **memrelay** routes events to Graphiti for persistent agent memory.
- A hypothetical third project might route to PostgreSQL, Elasticsearch, Langfuse, or a custom analytics pipeline.

**tracemill does not:**
- Manage processes, spawn adapters, or handle lifecycle
- Poll filesystems or tail files
- Query storage (sinks write only — consumers query their own backends)
- Contain domain logic (no jobs, approvals, memory retrieval, MCP)
- Do networking (no HTTP, sockets, SSE)
