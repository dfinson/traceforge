---
id: classification
title: Classification & Risk
sidebar_label: Classification & Risk
description: The multi-dimensional classification engine, shell AST analysis, MCP profiles, and 0–100 risk scoring with MITRE ATT&CK mappings.
---

# Classification & Risk

TraceForge classifies every tool invocation along **seven independent dimensions**, then
derives a 0–100 risk score. The engine is YAML-driven, rules, weights, and profiles are all
externalized to data files, not code.

## Dimensions

| Dimension | Question | Root values |
| --- | --- | --- |
| `Mechanism` | What resource domain? | filesystem, process, network, database, delegation, communication, unknown |
| `Effect` | What state change? | read_only, mutating, destructive |
| `Scope` | What's operated on? | artifact, state, data, configuration, knowledge, identity, message |
| `Role` | What archetype of tool? | validator, retriever, transformer, generator, modifier, executor, communicator, orchestrator, observer, persistence |
| `Action` | What verb? | validate, retrieve, transform, generate, execute, deliver, configure, analyze, persist, modify, remove |
| `Capability` | What permissions? | filesystem_read/write, network_inbound/outbound, subprocess, uses_credentials, elevated_privilege, human_interaction |
| `Structure` | Composition pattern? | sequential, parallel, conditional, interactive |

Coding-domain extensions add dot-path subtypes (e.g. `process.shell`, `artifact.source_code`,
`validator.linter`, `validate.lint`) plus `ShellDialect` (bash, powershell, cmd, zsh, fish,
posix_sh) and `ShellStructure` (piped, redirected).

```python
@dataclass(frozen=True)
class Classification:
    mechanism: str
    effect: str | None = None
    scope: frozenset[str] = frozenset()
    role: frozenset[str] = frozenset()
    action: frozenset[str] = frozenset()
    capability: frozenset[str] = frozenset()
    structure: frozenset[str] = frozenset()
    shell_dialect: str | None = None
    binaries: tuple[str, ...] = ()
    phase_map: tuple[PhaseSegment, ...] = ()
```

## Three classification paths

- **Shell commands**: deep AST analysis. Bash and PowerShell use tree-sitter grammars; cmd.exe
  uses lightweight tokenization. Shared infrastructure unwraps transparent wrappers (`env`,
  `sudo`, `nohup`, …), classifies binaries via rule tables, analyzes subcommands and flags,
  detects activity (verification, delivery, setup, investigation, implementation), and groups
  per-command phases into a `phase_map`.
- **Native tools**: static lookup via declarative classification tables.
- **MCP tools**: profile-based classification keyed on the `mcp__<server>__<tool>` namespace.

### Classifying directly

Most classification runs inside the [Enricher](enrichment.md), but you can call the classifiers
yourself with a default engine:

```python
from traceforge import classify_shell, classify_tool
from traceforge.classify import get_default_engine

engine = get_default_engine()

shell = classify_shell("rm -rf build && git push", engine=engine)
# shell.mechanism, shell.effect, and shell.capability describe the command

tool = classify_tool("mcp__github__create_issue", engine=engine)
# profile-based Classification for the MCP tool
```

## Risk scoring

A 0–100 score with a confidence level and MITRE ATT&CK technique mappings.

```python
@dataclass(frozen=True, slots=True)
class RiskAssessment:
    score: int         # 0-100
    level: str         # safe / caution / danger / critical
    confidence: str    # high / medium / low
    factors: tuple[str, ...]
    mitre: tuple[str, ...]
    version: str
```

Five scoring layers combine:

1. **Structural**: effect × scope (from the Classification).
2. **Flag modifiers**: per-binary flag rules (from `risk.yaml`).
3. **Injection patterns**: regex-matched evasion / injection patterns (capped).
4. **Pipeline taint**: source→sink flow escalation through pipe operators.
5. **Context**: project-relative path targeting adjustments.

## Data files

Classification behavior is governed entirely by YAML in `classify/data/`:

| File | Content |
| --- | --- |
| `canonical_tools.yaml` | Tool-name aliases (many→one normalization). |
| `verb_inference.yaml` | Verb prefix → (effect, action) mappings. |
| `binary_info.yaml` | Static metadata about known binaries. |
| `shell_defaults.yaml` | Activity → dimension default mappings. |
| `shell_rules.yaml` | Declarative binary + subcmd + flag → classification rules. |
| `effect_overrides.yaml` | Per-binary flag/subcmd effect overrides. |
| `mcp_profiles.yaml` | MCP server classification profiles. |
| `tool_classifications.yaml` | Full classifications for known native tools. |
| `risk.yaml` | Risk weights, flag modifiers, injection patterns, taint rules. |
| `recommendation_rules.yaml` | Governance rule set → `RecommendedAction` (consumed by the [Assessor](sdk.md)). |

## Bring your own classifications

The tables above are TraceForge's **built-in generic defaults**. Because the engine keys off
trace-native structure, it ships only a general vocabulary and never hard-codes a specific
consumer's tool catalog. To add your own native tools or private MCP servers, overlay them
through the config chain — don't edit these bundled files or fork the package.

Three overlay surfaces cover the common cases:

- `tool_classifications` — full classifications for your native tools.
- `mcp_profiles` — profiles for your MCP servers, with nested `tool_overrides` for per-tool refinement.
- `tool_display` — human-facing labels for the `tool_display` field stamped at enrichment (see the [event model](../architecture/event-model.md)).

Your entries **overlay** the defaults per key; nothing is replaced wholesale. Give an
`mcp_profiles` entry the same `id` as a built-in to supersede it, or set `disabled: true` to
drop one. Supply the overlay via (highest priority first) the `TRACEFORGE_CONFIG` env var, a
project `.traceforge/config.yaml`, or a `traceforge.profiles` entry point in your own package —
each overlaying the bundled defaults. See
[Custom tool & MCP classifications](../configuration.md#custom-tool--mcp-classifications) for the full
schema, YAML examples, and an entry-point walkthrough.

## Workflow dimensions

Derived presentation concerns are kept separate from semantic classification:

```python
class Phase(StrEnum):
    PLANNING, IMPLEMENTATION, VERIFICATION, EXPLORATION, REVIEW

class Visibility(StrEnum):
    VISIBLE, SYSTEM, COLLAPSED
```

The live phase model (see [Live Structuring](live-structuring.md)) folds the legacy `review`
class into `verification`.
