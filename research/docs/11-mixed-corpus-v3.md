# Mixed Corpus v3 — Selection Rationale

**Status:** Active corpus for the golden labeling run (2025-11-25).
**Supersedes:** [`10-mixed-corpus-v2.md`](10-mixed-corpus-v2.md) and the legacy
`copilot-cli` source (SQLite session-store).
**Target:** N_sweet = 800 sessions (see [`05-data-sizing.md`](05-data-sizing.md)
§"Boundary classifier", binding constraint).

## Why a third corpus revision?

v2 mixed three sources: `copilot-cli` (SQLite turn dumps), `swe-agent-nebius`,
and a planned codeplane bridge. Three problems forced a v3:

1. **`copilot-cli` SQLite was the wrong source.** The Copilot CLI
   `session-store.db` stores only *turn* metadata (one row per LLM call,
   no tool-call detail). That gives turn boundaries but loses the per-event
   `mechanism`/`effect`/`action`/`role` taxonomy the classifier learns from.
   The right source is `~/.copilot/session-state/<sid>/events.jsonl` — the
   native event stream the CLI itself writes.
2. **codeplane was a bespoke schema.** Routing through a codeplane SQLite
   would couple the training data to a project-specific persistence layer
   rather than the upstream tool's own format. Killed before any ingest ran.
3. **VS Code agent storage is effectively dead** on this machine
   (`session-store.db` has 1 session; `chatSessions/` has 3 files; Xodus
   blobs total 3.4 MB). Not viable as a corpus source.

The user-driven correction: *use the actual underlying session logs in
copilot or vscode agent. codeplane is my own project. not some well-established
format.*

## Sources

| name | format | rows | selection | yield |
| --- | --- | --- | --- | --- |
| `copilot-cli-native` | `~/.copilot/session-state/*/events.jsonl` (JSONL) | 726 sessions, 1.77 GB | floor: `n_tool_events >= 5` AND `n_unique_phase_signals >= 2` | 52 |
| `swe-agent-nebius` | HF nebius/swe-agent-trajectories (parquet) | 3,259 sessions | top-N by composite quality_score, floor `>= 0.67` | 748 |
| **total** |  |  |  | **800** |

### `copilot-cli-native` — single-machine reality check

Probed all 726 sessions to get an honest distribution:

| signal | sessions |
| --- | --- |
| sessions with **zero** tool calls | 663 (91%) |
| sessions with ≥1 tool call | 63 |
| sessions with ≥5 tool & ≥2 phase signals (the floor) | **52** |
| sessions with ≥100 tool calls | 24 |
| sessions with ≥250 tool calls (multi-hour) | 17 |
| longest session | 9,420 tool calls / 67,936 events / 41 parquet shards |

The 91% zero-tool tail is real: hook handlers, aborted prompts,
copilot-internal helpers, and command-line one-shots. The 52 sessions
clearing the floor are the real human-driven coding sessions. **Take all
of them** — they are the only single-machine distribution-shift safety net
this corpus has.

Selection is a hard floor with quality_score as a tiebreaker, not a top-N.
Floor values are calibrated in `experiments/mixed-corpus-v3.yaml`.

#### Marathon sessions (the giants)

Eight sessions exceed `max_events_per_call=220` after enrichment:

| sid prefix | events | tool calls | shards |
| --- | --- | --- | --- |
| `45149467` | 67,936 | 9,420 | 41 |
| `58869d29` | 34,306 | 6,484 | 21 |
| `f7440018` | 19,329 | 5,293 | 16 |
| `2096836f` | 17,631 | 5,171 | 12 |
| ... (4 more) |  |  |  |

These are the richest data in the corpus (sustained, real, multi-hour). The
current runner skips them. Window-segmentation into ~220-event labeling
units is the documented follow-up. **For tonight's run:** 21 native
sessions clear `<= 220` events, the remaining 31 (including the 8 giants)
are deferred.

Effective tonight-yield: **21 native + 748 swe-agent = 769 labelable
sessions** (96% of N_sweet target).

### `swe-agent-nebius` — top-quality slice

3,259 academic-bench sessions with uniformly clean tool sequences (every
session ≥5 tool & ≥2 phases — bench tasks are well-formed by construction).
We take top-748 by composite quality_score with a 0.67 floor (≈ p77).

* `min_quality_score=0.67` selected by inversion: the cutoff that yields
  exactly 748 sessions (800 - 52).
* Distribution at p77: every selected session has ≥20 tool events and
  ≥3 phase signals (verified 2025-11-25 against quality-scores.parquet).
* The lower bound is well above the swe-agent failure cases — bench failures
  cluster at the bottom decile (score < 0.45).

### Why not VS Code agent?

Probed exhaustively:

| location | finding |
| --- | --- |
| `%APPDATA%\Code\User\globalStorage\github.copilot-chat\session-store.db` | 1 session, 1 turn, 98 KB |
| `%APPDATA%\Code - Insiders\...\session-store.db` | 0 sessions, 4 KB |
| `%APPDATA%\Code\User\workspaceStorage\<hash>\chatSessions/` | 3 files, 798 KB (1 workspace) |
| `%LOCALAPPDATA%\github-copilot\py\chat-sessions\` | 42 Xodus binary blobs (.xd), 3.4 MB total |

This machine's VS Code agent footprint is effectively zero — the user
does not use Copilot Chat in VS Code for sustained coding. Not a viable
corpus source.

## Ingest pipeline

Both sources flow through the **same canonical traceforge pipeline** —
no bespoke ingest paths. Anti-pattern (calling `enricher.process()` then
`sink.on_event()` in a loop) is rejected on principle.

```python
from traceforge.adapters.mapped_json import MappedJsonAdapter
from traceforge.cli.runner import load_mapping_path
from traceforge.enricher import Enricher
from traceforge.pipeline import EventPipeline
from traceforge.sinks.parquet import ParquetSink

mapping_path = load_mapping_path("copilot")   # src/traceforge/mappings/copilot.yaml
adapter = MappedJsonAdapter.from_yaml(str(mapping_path), session_id=sid)
sink = ParquetSink(path=str(OUT_DIR / "{session_id}.parquet"))
pipeline = EventPipeline(sinks=[sink], enricher=Enricher())

for line in jsonl_file:
    for event in adapter.parse(line):
        await pipeline.push(event)
await sink.close()
```

Scripts:
* `research/scripts/ingest_copilot_sessions.py` — native JSONL ingest
* `research/scripts/ingest_swe_agent.py` — swe-agent ingest
* `research/scripts/score_session_quality.py` — per-session_id composite scoring
* `research/scripts/build_v3_manifest.py` — selects + emits manifest

## ParquetSink shard handling

ParquetSink rolls a new `.{N}.parquet` file on every `session.ended` or
`session.paused` event. One Copilot CLI session can span 40+ shards because
the user runs `copilot resume` repeatedly. The v3 manifest carries
`parquets: [shard_path, ...]` per session; the canonical view loader
concatenates and re-sorts by the per-session monotonic `seq`.

## Pre-existing audits that informed v3

* **Round-trip integrity** — 100% adapter round-trip on `58869d29` (40,805
  raw → 40,805 parsed). The 15-event delta between raw lines and emitted
  events is canonical Enricher behavior: it pairs `tool.execution_start` +
  `tool.execution_complete` into one merged event with `duration_ms`.
  Orphan starts (~15 per giant) are leaked into `_pending` at session end.
  **Not a bug. Documented.**
* **Mutation vocab** — Scorer uses canonical `EFFECTS = {"mutating",
  "destructive"}`, `ACTION_PREFIXES = ("persist.", "modify.", "delete.")`,
  plus bare-leaf compatibility (`write`, `edit`, `create`, `delete`,
  `modify`, `patch`).
* **Per-session_id aggregation** — Scorer reads the `session_id` parquet
  column rather than filename to handle shard-split sessions correctly.
* **Stale `copilot-cli` SQLite dir** — Removed
  `data/interim/labeling-corpus/copilot-cli/` so it cannot contaminate v3.

## Cost estimate

* 769 sessions × ~$0.15/session at Copilot Sonnet pricing ≈ **$115**
* Two passes per session (labeler + red-team) ≈ **$230 total**
* Within the original $120-$240 budget from `05-data-sizing.md`.

## Open follow-ups

* **Window-segmentation** for the 31 oversized native sessions. Could yield
  100-400 additional labeled windows from the richest 8 marathons.
* **Refactor `research/src/traceforge_research/ingest/swe_agent.py`** to use
  `MappedJsonAdapter` + `EventPipeline` (currently calls `enricher.process()`
  directly). Existing 3,259 enriched parquets are schema-valid so no
  re-ingest needed — code-cleanup only.
* **Snapshot policy** — `~/.copilot/session-state-snapshots/` currently
  holds three giants (`45149467`, `7da3558b`, `f7440018`). If we add
  window-segmentation, snapshot the rest of the active marathons before
  the user resumes them and they grow further.
