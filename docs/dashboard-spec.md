# TraceForge Local Dashboard — Specification

Status: built (tasks D0–D10 complete on branch `dfinson-dashboard-spec-build`). This is the
authoritative spec for the local "trace the traces" dashboard portal. It ports the user-approved
design mock verbatim and replaces its synthetic data layer with a read-only local API over
TraceForge's real SQLite storage, launched by a new `traceforge dashboard` CLI subcommand.

Product axis: **observability / cost & latency attribution**. Risk/governance is an opt-in
lens, never the landing tone. The Fleet landing is accounting-first
(Active / Runs / Spend / Tokens / Classified%); loud severity treatment lives only in Triage.

---

## Implementation status (as-built)

**Shipped and verified.** `traceforge dashboard` serves the ported SPA and renders live from a
seeded output-sink DB; `tsc -b` + `corepack pnpm build` are clean and the backend suite (48
tests across mappers, repository, server, API, CLI) is green. Degraded and no-output modes were
smoke-tested against the real server.

**API surface actually built (thin backend — see §5 fork 2):**

- `GET /api/health` → `{output_db, system_db, has_output_db, has_system_memory}`
- `GET /api/runs?limit&offset` → the **most-recent window** of runs, each fully assembled
  (identity + events + usage + segs + governance memory), ordered most-recent-first. Bounded:
  `limit` defaults to **200** and is clamped to a hard server-side max of **500**; `offset` pages.
  The bound is applied at the SQL layer (`repository.list_run_ids(limit, offset)`), so a
  high-volume store never materializes more than one page of full runs.
- `GET /api/runs/{id}` → one run (drill-in; always full detail, unbounded by the list window)

Because every view aggregates over this shared window, the Fleet/Triage/Cost/Coverage numbers
reflect the **most-recent N runs** rather than all-time — the correct semantics for a live
console. The Fleet subtitle says "The most recent runs…" so the UI doesn't over-claim; no other
copy or layout changed. Ordering is by last-activity timestamp (`MAX(timestamp) DESC`), which
keeps live runs at the top.

**Known follow-up (documented, not in this build):** within the returned window, a single
pathological long run still carries all its event bodies in the list payload. Trimming raw event
bodies from the *list* response would break the client-side aggregations (Fleet KPIs/rail, Cost
attribution, Coverage classification, Triage risk all read `run.events`), so it's deferred. The
clean path is a lightweight list projection — per-run aggregates (cost/tokens + phase/risk/
classification histograms) computed server-side (the unused `repository.list_runs()` /
`_run_summary()` scaffold is the seed) — that the fleet views consume instead of raw events,
leaving `/api/runs/{id}` as the only full-detail read. That's a real per-view refactor, out of
scope for the windowing bound.

The per-view aggregate endpoints sketched in §2 (`/api/fleet`, `/api/triage`, `/api/cost`,
`/api/coverage`) were **not** built as separate routes. Every view instead aggregates
client-side over the shared `Run[]` from the `useRuns()` React Query hook — faithful to how the
approved mock drove its synthetic `RUNS` generator, and the fastest path to visual parity. The
§2 "Endpoint" lines below therefore describe *what data each view consumes*, not distinct HTTP
routes. Promoting any of them to a real aggregate endpoint later is additive and non-breaking:
the client hook is the only seam. This decision was reported to the coordinator at the D5
milestone.

---

## 1. Architecture & delivery

### 1.1 Two real data sources (both already exist)

- **Output-sink DB** — written by `SqliteOutputSink` (`src/traceforge/sinks/sqlite_output.py`),
  default path `~/.traceforge/traceforge.db` (config `type: sqlite`). Standalone schema, no
  migrations. Tables: `enriched_events`, `segment_titles`, `context_gaps`, `spans`,
  `usage_records`, `attribution_rollups`, `attribution_anomalies`. **Backbone** — one row per
  tool event with risk/action/cost/duration hoisted to columns + full `metadata_json`
  (governance `SessionMeta`) + `payload_json`.
- **system.db** — Alembic-managed governance store (`SystemStore`,
  `migrations/versions/0001_initial.py`), path `~/.traceforge/system.db`. Tables:
  `session_state`, `session_summaries`, `processed_events`, `mcp_profiles`(+`_attributes`),
  `budget_counters`, `taint_entries`, `trust_grants`, `drift_baselines`, `content_hashes`,
  `gate_endpoints`. **Cross-session governance MEMORY** (identity, taint, trust, MCP drift,
  drift baselines).

### 1.2 Delivery — `traceforge dashboard` CLI subcommand

New `src/traceforge/cli/dashboard_cmd.py`, registered in `cli/__init__.py` alongside the other
commands. It serves the built SPA **and** a read-only JSON API from **one** stdlib
`http.server.ThreadingHTTPServer` — the same stack `ScoreServer` (`cli/score.py`) already uses,
so **zero new Python runtime deps** (consistent with the repo's lightweight design). Read-only
`sqlite3` connections mirror `cli/status.py`.

Flags:
- `--output-db PATH` (default `~/.traceforge/traceforge.db`)
- `--system-db PATH` (default `~/.traceforge/system.db`)
- `--config PATH` (read the sqlite sink path from config if present)
- `--host` / `--port` (default `127.0.0.1:7788`), `--open` / `--no-open` (launch browser)

The API is **strictly read-only** — never opens a write connection, never mutates either DB.

### 1.3 Repo home & packaging

- **Frontend source**: top-level `dashboard/` — the ported mock (own `package.json`, pnpm, Vite).
  Built with `corepack pnpm build` (npm is broken on the dev machine — pnpm only).
- **Python module**: `src/traceforge/dashboard/` — `server.py` (HTTP handler + routing),
  `repository.py` (read-only SQLite queries + mock-shape mapping), `__init__.py`. Bundled SPA in
  `src/traceforge/dashboard/static/` (the `pnpm build` output).
- **Packaging (hatchling)**: add `src/traceforge/dashboard/static/**` to
  `[tool.hatch.build.targets.wheel].artifacts` and `[...sdist].only-include`, so the built SPA
  ships in the wheel. `scripts/build_dashboard.py` (or a hatch build hook) runs the pnpm build and
  copies `dashboard/dist` → `src/traceforge/dashboard/static`.

### 1.4 Frontend data swap

Keep **all** presentational components/charts and the nav store untouched; only the data layer
changes:
- Move the `Run`/`TEvent`/`Seg`/`Evidence`/… TypeScript types out of `src/data/runs.ts` into
  `src/lib/types.ts` (delete the synthetic generator once wiring lands).
- Add `src/lib/api.ts` (typed fetch client) + `@tanstack/react-query` for
  caching/loading/error/refetch (frontend dev dep only — does not touch the Python wheel). "Live"
  runs use interval refetch.
- Each view replaces its `RUNS.flatMap(...)` / `useMemo` aggregation with a `useQuery` to that
  view's endpoint. The backend returns data already shaped to the mock's structures, so components
  (KpiCard, RiskBadge, VerdictBadge, all charts, Inspector) don't change.

---

## 2. Screen-by-screen

For each view: components kept from the mock → real data → endpoint.

> **As-built transport:** the per-view "Endpoint" lines below name the *data each view needs*;
> the shipped transport is a single shared `GET /api/runs` (bounded — most-recent window,
> `?limit` default 200 / max 500, `?offset`) that every view aggregates client-side (see
> "Implementation status" above). `/api/runs/{id}` and `/api/health` are the only other routes.

### Fleet (landing — accounting-first)
- **Keeps**: 5 `KpiCard`s (Active / Runs / Spend / Tokens / Classified%), quiet
  "N flagged for triage →" chip, `ActivityChart` (by hour, stacked by phase), `SpendArea`
  (cumulative), rail: `AttributionBars` (Spend by phase), `DistBar` (Classification mix),
  `DistBar` (Risk mix, with Triage link), Runs `Table`.
- **Data**: KPIs + rail from fleet-wide `GROUP BY` over `enriched_events` / `usage_records`; run
  rows from per-session rollup joined to `session_summaries` (identity).
- **Endpoints**: `GET /api/fleet` (KPIs + rail + charts), `GET /api/runs` (summary rows).

### RunView (drill-in)
- **Keeps**: header identity line, **Rewind** ribbon (`RiskRibbon` + `SpendSparkline`),
  **Chapters** two-tier tree, **Timeline** (enriched events list), **Inspector**
  (Recommendation, Evidence: MITRE + predicates + PII + info-flow + payload ptr, meta grid,
  Payload, context-gap banner).
- **Data**: `segment_titles` (chapters tree), `enriched_events` for the session (timeline + most
  inspector fields via `metadata_json`), `usage_records` / `spans` (tokens/duration/cost),
  `context_gaps` (gap banner), system.db `taint` / `trust` / `mcp` +
  `session_summaries.drift_max` (header drift, governance side-data).
- **Endpoint**: `GET /api/runs/{id}` → full `Run`.

### Triage (risk lens — loud severity lives ONLY here)
- **Keeps**: Critical/Danger buckets (worst-first queue), `RiskByAgent`, `TechniqueBars`, three
  governance-memory cards (Taint ledger / Trust grants / MCP drift) OR the "memory unavailable"
  degraded card.
- **Data**: `enriched_events` where risk ≥ danger across fleet; MITRE from `metadata_json`
  evidence; memory cards from system.db `taint_entries` / `trust_grants` / `mcp_profiles`.
- **Endpoint**: `GET /api/triage`.

### Cost (attribution)
- **Keeps**: 4 `KpiCard`s (Spend / Tokens / Tool calls / Retry waste), `AttributionBars` with
  phase/tool/file tabs, `CostScatter` (cost×duration per run), `SpendArea`, By-model `Table`.
- **Data**: attribution by trace-native dimension (`attribution_rollups` when attribution is
  enabled; else reconstructed via `GROUP BY` over `usage_records.attributes` / `enriched_events`);
  per-run scatter from per-session cost/duration; by-model from `usage_records.model`.
- **Endpoint**: `GET /api/cost?dim=phase|tool|file`.

### Coverage (classification completeness)
- **Keeps**: `CoverageDonut` + legend (classification spread), Override-candidates `Table`
  (low-confidence), Context-gaps list.
- **Data**: classification category spread from `metadata_json`; low-confidence candidates from
  classification confidence; gaps from `context_gaps`.
- **Endpoint**: `GET /api/coverage`.

Plus `GET /api/health` → `{output_db, system_db, has_system_memory, has_output_db}` driving
degraded modes + the data-source indicator.

---

## 3. Data contract (mock field → real source)

`RISK` levels line up exactly: `RiskAssessment.level` = safe/caution/danger/critical ↔ mock 0–3.

### `Run` (a run == a `session_id`)
| mock field | real source |
|---|---|
| `id` | `enriched_events.session_id` (distinct) |
| `repo` | `session_summaries.repo`; fallback `metadata_json.repo` (per-event `EventMetadata.repo`) |
| `agent` | `session_summaries.agent_model` (split) / `metadata_json.source_framework` |
| `model` | `usage_records.model` (dominant); part of `session_summaries.agent_model` |
| `title` | `segment_titles` WHERE `kind='session'` |
| `live` | `session_state` row exists AND `session_summaries.ended_at IS NULL` |
| `segs[]` | `segment_titles` (kind session/activity/step, `parent_id`, `version`) |
| `events[]` | `enriched_events` for `session_id` (+ usage/spans join) |
| `usage {in,out,cost}` | `SUM(usage_records.input_tokens/output_tokens/cost_usd)` per session |
| `started` | `MIN(enriched_events.timestamp)` / `session_summaries.started_at` |
| `durMs` | `MAX(ts)−MIN(ts)` / `session_state.elapsed_seconds` |
| `drift` | `session_summaries.drift_max` (**system.db only**; n/a otherwise) |
| `peak` | `MAX` of `enriched_events.risk_level` mapped to 0–3 |
| `taint[]` | system.db `taint_entries` (source→clearance, payload_pointer) |
| `trust[]` | system.db `trust_grants` (key, granted_at+ttl_seconds→TTL, reason) |
| `mcp[]` | system.db `mcp_profiles` + `metadata_json.governance.mcp_alerts` |

> **Usage token semantics (Claude Code per-message path).** For real Claude Code
> transcripts there is no Agent-SDK `result` line — token usage rides every assistant
> message, and Claude Code writes one JSONL line per content block, so a single
> `message.id` (with identical `message.usage`) is repeated ~3×. The watch usage bridge
> therefore **dedups on `message.id` first**, then writes one `usage_records` row per
> message. Headline `input_tokens` is the **aggregate context the model processed** =
> `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` (on Claude the
> uncached delta alone is misleadingly tiny — almost all input is replayed cached
> context). The lossless split is kept in `usage_records.attributes` as
> `{input_uncached, cache_read_tokens, cache_creation_tokens}` so a future weighted-cost
> calc can price cache-read (far cheaper) separately. `cost_usd` is **`None`** on this
> path — the per-message wire carries no cost and one is never synthesized (the Cost lens
> shows real tokens with honest null/`$0.00` dollars). A `<synthetic>`/absent model
> normalizes to `""` so it never wins `_dominant_model`, while its real tokens still count.

### `TEvent` (timeline + inspector)
| mock field | real source |
|---|---|
| `id` | `enriched_events.id` |
| `t` | `enriched_events.timestamp` |
| `tool.n` | `tool_name` / `tool_display` |
| `tool.canon` | `metadata_json.governance.classification.mechanism` |
| `tool.cat` | `classification.effect` (read-only/mutating/exec/network/mcp) |
| `kind` | `enriched_events.kind` |
| `summary` | derived from `payload_json` / `tool_display` |
| `risk` (0–3) | `enriched_events.risk_level` mapped |
| `score` (0–1) | `enriched_events.risk_score / 100` |
| `action` | `enriched_events.action` (`recommendation.recommended_action`) |
| `cost` | `enriched_events.cost` (best-effort `payload.cost_usd`); authoritative in `usage_records` |
| `tokens` | `usage_records` (session+time correlated) / payload |
| `dur` | `enriched_events.duration_ms` |
| `phase` | `metadata_json.phase` |
| `seg` | `metadata_json.activity_id`/`step_id` |
| `file` | `metadata_json`/`payload` file attribute |
| `turn` | `metadata_json.turn_id` |
| `retry` | attributes `retry` (may be absent → false) |
| `cls {canon,cat,conf}` | `classification.mechanism`/`effect`; `conf` ← `risk_assessment.confidence` (categorical→numeric — see gap) |
| `ev.mitre` | `evidence.mitre_techniques` |
| `ev.preds` | `evidence.matched_predicates` / `risk_factors` |
| `ev.ptr` | `evidence.pointers[].payload_pointer` |
| `ev.pii` | **derive** from `taint_entries`/PII labels (gap — best-effort, else "none") |
| `ev.ifc` | **derive** from taint clearance flow / `classification.source_labels` (gap) |
| `reco {action,why}` | `recommendation.recommended_action` + `reason_code`/`message` |
| `gap` | `context_gaps` (session-correlated) |
| `payload` | `enriched_events.payload_json` |

### Attribution / Coverage
- 6 trace-native dims (`TRACE_NATIVE_DIMENSIONS` = phase, turn, segment, tool, file, retry) match
  the mock exactly.
- `attribution_rollups` PK is `(dimension, key)` — **global / last-write, no `session_id`**, and
  **opt-in** (empty unless attribution is enabled). Global fleet attribution can read it; per-run
  attribution is **reconstructed** from `usage_records`/`enriched_events` grouped by session.
- Classification spread + override candidates ← `GROUP BY` over `metadata_json`; context gaps ←
  `context_gaps`.

### Documented gaps (flagged, not blockers)
1. **Numeric classification confidence** — real confidence is categorical (high/medium/low);
   Coverage's `<0.9`/`≥0.9` needs numeric. → map bands (high≈0.95/med≈0.8/low≈0.6) **or** re-cut
   Coverage to categorical.
2. **`ev.pii` / `ev.ifc`** — no single column; derive best-effort from taint/labels, else "none".
3. **`retry` / per-event `tokens`** — depend on producers stamping attributes/usage; degrade to
   false/omitted when absent.
4. **`attribution_rollups` opt-in + global** — reconstruct per-run; show rollups when present.

---

## 4. Degraded / partial-data modes

| condition | what shows |
|---|---|
| **Full** (output DB + system.db present) | everything |
| **Output-DB only** (SDK-embed, no system.db) | identity (repo/model/framework from `metadata_json`+`usage_records`) + all per-event governance stamps (in `metadata_json`) **remain**; **LOST**: taint ledger, trust grants, cross-session MCP/drift memory → Triage shows "Governance memory unavailable" card, RunView drift = "n/a" |
| **No output DB** | global empty state with a one-liner to configure the `sqlite` sink |

`GET /api/health` exposes `has_system_memory` / `has_output_db`; the mock's `DataSourceToggle`
becomes a **real auto-detected indicator** (with an optional manual override kept for
teaching/demo). This refines the mock, which tied the toggle to *identity*; identity is in fact
available in both modes because the output DB carries repo/model.

---

## 5. Resolved decisions (defaults; adjustable)

These were presented as product-taste forks and approved with the recommended defaults below.

1. **sysdb toggle semantics** — auto-detected "governance memory present?" (identity stays
   available in both modes). Keep a manual override for teaching/demo. *(diverges from the mock's
   "identity unknown on SDK-embed")*
2. **API shape** — **resolved to the thin backend**: `GET /api/runs` returns the most-recent
   window of runs fully assembled (bounded by `?limit`, default 200 / hard max 500, plus
   `?offset`, ordered most-recent-first) and each view aggregates client-side, plus
   `/api/runs/{id}` for the drill-in. This keeps the mock's presentational components verbatim
   and was the fastest path to parity; reported to the coordinator at D5, and the list was
   bounded as a post-review follow-up so a high-volume store can't return an unbounded payload.
   Fat per-view aggregate endpoints (and a lightweight event-body-free list projection) remain a
   non-breaking future option (the `useRuns()` hook is the only seam).
3. **Read-API stack** — stdlib `http.server` (zero deps, matches ScoreServer).
4. **Live updates** — interval poll-refetch for v1; SSE later.
5. **Classification confidence** — categorical→numeric bands.
6. **`pii`/`ifc` inspector fields** — derive best-effort.
7. **Frontend data lib** — `@tanstack/react-query`.
8. **Repo home** — `dashboard/` (source) + `src/traceforge/dashboard/static` (bundled).

---

## 6. Task DAG (PR-sized)

Two foundational tracks (**D1 frontend**, **D2 backend read-layer**) run in parallel; **D3** (HTTP
server) needs D2; **D4** (CLI + bundling) needs D1+D3; the four wiring tasks **D5–D8** each = one
view + its endpoint(s) and are mutually parallel once D1+D2+D3 land; **D9** degraded modes
consolidates across views; **D10** tests/docs closes.

```
D0 spec-commit
D1 frontend-port ─┐
D2 backend-readlayer ─┬─> D3 http-server ─> D4 cli+bundle
                      │                       │
        D1 ───────────┴───────────────┐       │
                                      v       v
                         D5 fleet  D6 runview  D7 triage  D8 cost+coverage   (parallel)
                                      └──────────┬───────────────┘
                                                 v
                                              D9 degraded-modes
                                                 v
                                              D10 tests+docs
```

- **D0 — Commit the SPEC.** This document. Deps: none.
- **D1 — Scaffold + port frontend.** Copy the mock into `dashboard/` verbatim; verify
  `corepack pnpm install/build` + `pnpm exec tsc -b` clean; extract shared types to
  `src/lib/types.ts`; add `src/lib/api.ts` + react-query provider (unused yet). No behavior change.
  Deps: none.
- **D2 — Backend read-layer.** `dashboard/repository.py`: read-only sqlite3 access to both DBs
  (mirror `status.py`), DB discovery (flags/config/defaults), and mock-shape mapping helpers
  (Run/TEvent/Seg → JSON). Unit-tested vs a seeded temp DB. Deps: none.
- **D3 — HTTP server + static + `/api/health`.** `dashboard/server.py`: ThreadingHTTPServer that
  serves bundled SPA + routes `/api/*`; wire `/api/health` (degraded detection). Deps: D2.
- **D4 — `traceforge dashboard` command + bundling.** `cli/dashboard_cmd.py` (+ register);
  hatchling `artifacts`/sdist `only-include` for the static dir; `scripts/build_dashboard.py`;
  `--open` browser launch. End-to-end launchable. Deps: D1, D3.
- **D5 — Wire Fleet.** `/api/fleet` + `/api/runs`; swap Fleet's data layer. Deps: D1, D3.
- **D6 — Wire RunView.** `/api/runs/{id}` (segments/timeline/inspector/rewind); swap RunView.
  Deps: D1, D3.
- **D7 — Wire Triage.** `/api/triage` (+ system.db governance memory); swap Triage. Deps: D1, D3.
- **D8 — Wire Cost + Coverage.** `/api/cost?dim=`, `/api/coverage`; swap both. Deps: D1, D3.
- **D9 — Degraded modes.** Per-view empty/partial states from `/api/health`; real DataSourceToggle
  indicator; no-output-DB empty state. Deps: D5–D8.
- **D10 — Tests + docs.** Backend unit + endpoint tests over a seeded DB; e2e smoke (spin server,
  hit endpoints); frontend typecheck/build in CI or a make target; finalize this doc; README
  mention. Deps: all.

### Notes / constraints (dev machine)
- **pnpm only** (`corepack pnpm <cmd>`); npm is broken. Typecheck `corepack pnpm exec tsc -b`;
  build `corepack pnpm build`.
- Python tests run from a checkout with a `.venv` (worktrees may lack one — create a venv or run
  from the main checkout).
- Ruff pinned `0.15.20` (both `check` and `format --check`).
