# 02 — Data Inventory

What we have on disk, what's labeled, what aligns, and what we can actually
use for training.

## Sources

### Legacy SWE-agent / OpenHands / SWE-smith (labeled)

Copied from `\\wsl$\Ubuntu\home\dave01\tracemill-labels\` to
`research/data/raw/legacy/`. Six files, ~60 MB total.

| File | Sessions | Turns / events | Source framework |
| --- | --- | --- | --- |
| `fulltext/labels_v2.json` | 514 | 22,116 (turn-level labels) | SWE-agent |
| `fulltext/all_fulltext.json` | 1,500 | turn-level text (SWE-agent / OpenHands / SWE-smith, 500 each) | mixed |
| `fulltext/phase_labels.json` | 1,102 | ~67k per-turn phase labels (multi-label frozensets) | mixed |
| `swe_agent_extracted_v2.json` | 1,700 | per-event tool data | SWE-agent |
| `openhands_extracted.json` | varies | per-event tool data | OpenHands |
| `swesmith_extracted.json` | varies | per-event tool data | SWE-smith |

Label distribution on the labeled 514:

- noise: 84% (18,577)
- activity-boundary: 13% (2,875)
- step-boundary: 3% (664)

### Local Copilot CLI corpus (unlabeled, large)

WSL `~/.copilot/session-state/`. Real sessions from this machine.

- 53,987 directories
- 49,690 with non-trivial `events.jsonl` (>10 KB)
- 34,866 substantial sessions (>100 KB)
- 11 GB total

Schema is event-sourced JSONL: `{type, id, timestamp, parentId, data}` with
event types `tool.execution_start/complete`, `assistant.turn_start/message/turn_end`,
`hook.start/end`, `user.message`, `session.start/shutdown`, `abort`,
`subagent.started/completed`, `skill.invoked`.

`src/tracemill/mappings/copilot.yaml` already handles this exact schema —
sessions can be enriched without parser work.

### VS Code Copilot Chat / Claude Code Windows

Both negligible. VS Code chat has only an embeddings cache on disk; Claude
Code on Windows has zero session state in standard locations.

### Hugging Face originals

Listed in `manifest.yaml` as placeholders, not fetched. Decision to date: do
not download — we have the extracted subset already and full downloads are
large.

### The post-tracemill canonicalization problem

Important caveat for both label sets: **none of these labels were generated
by looking at canonicalized tracemill output**. The original LLM annotator
saw raw turn text and tool-call shapes. It did not see
`metadata.classification`, `metadata.activity`, `metadata.phases`, or any
post-enricher field — because those didn't exist on the labeled data.

Consequence: when we train a classifier with canonical features as input
against these labels, ground truth was generated from signals the
classifier can't see, and may correlate with text-surface features the
canonical form deliberately abstracts away. The 0.533 F1_macro number is a
measurement against labels that aren't grounded in the feature space we
care about.

This is why fresh labeling on the local Copilot corpus, *after* running
those sessions through the tracemill enricher and showing the canonical
view to the labeler, is on the roadmap. See
[`05-data-sizing.md`](05-data-sizing.md) for the sizing recommendation.

## The alignment problem

This is the constraint that has shaped everything.

- 514 labeled sessions (in `labels_v2.json` and `all_fulltext.json`).
- Only 3 of those 514 also appear in `swe_agent_extracted_v2.json` (the
  per-event extraction).
- Even where IDs match, the counts disagree: e.g., a session with 25 turns in
  fulltext has 46 events in extracted. The fulltext version was condensed /
  concatenated by an upstream preprocessor.

**Consequence.** We cannot run `tracemill.Enricher` on the labeled data
end-to-end, because we have either *labels with text* or *events without
labels*, and the join is broken for 511 of 514 sessions.

**Three options considered:**

1. **Re-fetch from Hugging Face.** User said no — too large.
2. **Stay condensed.** Limits feature richness; we get text but not per-event
   tool calls.
3. **Pivot to local Copilot corpus.** Real events, full schema, 50k sessions,
   but no labels.

The current direction is a hybrid: train and validate on what's labeled
(option 2), then evaluate transfer / utilize Copilot as unlabeled data
(option 3).

## What this constrains

- The classifier input cannot rely on per-event tool fields for the labeled
  set — only turn-level fields and text.
- model2vec on payload text works because it's text-level — that's the
  natural representation we have for labels.
- Multi-framework transfer evaluation requires either labeling some Copilot
  sessions or accepting that transfer is evaluated on OpenHands /
  SWE-smith only.

## Re-running the inventory

```powershell
cd research
uv run python scripts\inventory.py
```

Reports counts, alignment overlap, and label distribution. The script is the
authoritative answer — anything in this doc may be stale if the script
disagrees.
