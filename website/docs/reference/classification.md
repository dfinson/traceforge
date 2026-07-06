---
id: classification
title: Classification & Risk
sidebar_label: Classification & Risk
description: The multi-dimensional classification engine, shell AST analysis, MCP profiles, and 0–100 risk scoring with MITRE ATT&CK mappings.
---

# Classification & Risk

TraceForge classifies every tool invocation along **seven independent dimensions**, then
derives a 0–100 risk score. The engine is YAML-driven — rules, weights, and profiles are all
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

- **Shell commands** — deep AST analysis. Bash and PowerShell use tree-sitter grammars; cmd.exe
  uses lightweight tokenization. Shared infrastructure unwraps transparent wrappers (`env`,
  `sudo`, `nohup`, …), classifies binaries via rule tables, analyzes subcommands and flags,
  detects activity (verification, delivery, setup, investigation, implementation), and groups
  per-command phases into a `phase_map`.
- **Native tools** — static lookup via declarative classification tables.
- **MCP tools** — profile-based classification keyed on the `mcp__<server>__<tool>` namespace.

```python
@dataclass(frozen=True)
class McpServerProfile:
    namespace_aliases: tuple[str, ...]   # e.g. ("github", "gh")
    mechanism: str
    role: frozenset[str]
    default_effect: str | None
    scope: frozenset[str]
    action: frozenset[str]
    capability: frozenset[str]
    tool_overrides: dict[str, McpToolOverride]
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

1. **Structural** — effect × scope (from the Classification).
2. **Flag modifiers** — per-binary flag rules (from `risk.yaml`).
3. **Injection patterns** — regex-matched evasion / injection patterns (capped).
4. **Pipeline taint** — source→sink flow escalation through pipe operators.
5. **Context** — project-relative path targeting adjustments.

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
