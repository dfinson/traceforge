# Weekly CLI Surface Audit — Copilot Agent Workflow Specification

## Purpose

Automated weekly Copilot workflow that verifies traceforge's CLI classification YAML files remain accurate and complete against the latest stable releases of AWS CLI, Azure CLI, and Google Cloud CLI. Detects new subcommands, removed verbs, and misclassified effects before they cause silent classification drift.

## Scope

All CLI-related classification data:
- `src/traceforge/classify/data/shell_rules.yaml` — service-group routing rules
- `src/traceforge/classify/data/effect_overrides.yaml` — verb-to-effect mappings
- `src/traceforge/classify/data/binary_info.yaml` — binary metadata
- `src/traceforge/classify/data/risk.yaml` — flag modifiers

## Breakable Surfaces

| CLI | Source of Truth | Breakable Surface |
|-----|-----------------|-------------------|
| AWS CLI | `aws/aws-cli` repo, `aws <svc> help` | New services, renamed verbs, deprecated subcommands |
| Azure CLI | `Azure/azure-cli` repo, `az <group> --help` | New extension groups, renamed commands, removed verbs |
| GCP gcloud | `google-cloud-sdk` changelogs, `gcloud <group> --help` | New component groups, verb renames, storage command aliases |
| gsutil | Deprecated in favor of `gcloud storage` | Removal timeline, command parity gaps |

## Audit Process

### 1. Version Detection
For each CLI, determine the latest stable release:
- **AWS**: Check `aws/aws-cli` GitHub releases or PyPI `awscli` package
- **Azure**: Check `Azure/azure-cli` GitHub releases or PyPI `azure-cli` package
- **GCP**: Check `cloud.google.com/sdk/docs/release-notes` or the components list

### 2. Verb Coverage Audit (per CLI)
For each CLI, fetch the current verb taxonomy from official documentation:

1. **Extract all verb prefixes** from the official CLI reference
2. **Compare against `effect_overrides.yaml`** verb_prefix_effects entries
3. **Identify gaps**:
   - Verbs in source not in our mappings (coverage gap)
   - Verbs in our mappings not in source (stale entries)
   - Verbs with wrong effect classification (misclassification)

### 3. Service Group Audit
For `shell_rules.yaml` service-group subcmds:
1. **Extract all top-level service names** from each CLI
2. **Compare against subcmds lists** in shell rules
3. **Identify**:
   - New services not routed (will fall through to fallback rule)
   - Deprecated/removed services still listed

### 4. Risk Flag Audit
For `risk.yaml` flag_modifiers:
1. **Verify flags still exist** in current CLI versions
2. **Identify new dangerous flags** (e.g., `--force`, `--no-confirm` variants)

## Verdict Categories

| Verdict | Condition | Action |
|---------|-----------|--------|
| 🔴 BREAKING | Mapped verb removed/renamed, effect wrong for active verb | Fix immediately |
| ⚠️ GAP | New service or verb not covered (falls to fallback) | Add mapping |
| ✅ PASS | All mappings verified correct | No action |

## Execution Model

```
┌─────────────────────────────────────────────────────────┐
│  Copilot Workflow (weekly, autopilot)                    │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  1. Run test suite (baseline pass required)             │
│                                                         │
│  2. Launch parallel research agents:                    │
│     ├── AWS verb audit (fetch latest reference)         │
│     ├── Azure verb audit (fetch latest reference)       │
│     └── GCP verb audit (fetch latest reference)         │
│                                                         │
│  3. Diff findings against current YAML files            │
│                                                         │
│  4. If 🔴 findings:                                     │
│     ├── Apply fixes to YAML files                       │
│     ├── Add/update test cases                           │
│     ├── Run full test suite                             │
│     └── Commit + create issue                           │
│                                                         │
│  5. If ⚠️ findings:                                     │
│     ├── Add new mappings                                │
│     ├── Run tests                                       │
│     └── Commit + create issue                           │
│                                                         │
│  6. Report summary                                      │
└─────────────────────────────────────────────────────────┘
```

## Copilot Workflow Configuration

**Name**: CLI Surface Audit
**Schedule**: Weekly (Monday 06:00 UTC)
**Mode**: Autopilot
**Prompt**:

```
Audit the CLI classification YAML files against latest stable CLI releases.

## Steps

1. Run `python -m pytest tests/unit/test_cloud_cli_effects.py -q` to verify baseline.

2. For each CLI (aws, az, gcloud), research the latest stable release and fetch the
   current verb/subcommand taxonomy from official documentation.

3. Compare against the verb_prefix_effects in `src/traceforge/classify/data/effect_overrides.yaml`:
   - Identify verbs in the official CLI that are NOT in our mappings
   - Identify verbs in our mappings that no longer exist
   - Identify verbs with incorrect effect classification

4. Compare service-group subcmds in `src/traceforge/classify/data/shell_rules.yaml`:
   - Identify new top-level services not listed in any rule's subcmds
   - Identify deprecated services still listed

5. If issues found:
   - Fix effect_overrides.yaml and shell_rules.yaml
   - Add test cases to tests/unit/test_cloud_cli_effects.py
   - Run full test suite: `python -m pytest tests/ -q`
   - Commit with message: "chore: update CLI classification for [CLI] [version]"
   - Create a GitHub issue summarizing findings

6. If no issues: report "CLI surfaces verified current" and exit.
```

## Relationship to Existing Workflows

| Workflow | Scope | Trigger | Mechanism |
|----------|-------|---------|-----------|
| `tool-surface-audit.yml` | YAML syntax + engine smoke test | Weekly (GH Actions) | Deterministic CI |
| `weekly-compat-audit.yml` | Framework SDK compatibility | Weekly (GH Actions) | pip install + pytest |
| **This workflow** | CLI verb/service coverage accuracy | Weekly (Copilot) | Agentic research + fix |
| `weekly-audit-job.md` | Framework YAML mapping drift | Weekly (Copilot) | Agentic research + fix |

The existing GH Actions workflows catch *structural* issues (syntax, import errors).
The Copilot agentic workflows catch *semantic* drift (new upstream verbs, removed APIs).
