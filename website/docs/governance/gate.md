---
id: gate
title: The Gate (Enforcement)
sidebar_label: Gate & Enforcement
description: Turn recommendations into enforced verdicts with an opt-in GatePolicy, framework adapters, and shell-hook relays.
---

# The Gate (Enforcement)

By default, TraceForge only **recommends**. To let it **decide**, register an opt-in
`GatePolicy`. The `Shield` then turns a recommendation into an enforced `Verdict` at a
framework's native pre/post-execution hook. Any error inside the shield's chains is
**fail-closed**: DENY on preflight, SUPPRESS on postflight.

## In-process gating (SDK)

Compose a `GatePolicy` (preflight/postflight callbacks returning a `Verdict`) onto the
pipeline's shield, then bind it to a framework with one call:

```python
from traceforge.sdk import Pipeline, GatePolicy, Verdict, ToolCallRequest, GateContext

def preflight(request: ToolCallRequest, ctx: GateContext) -> Verdict:
    if request.risk_score and request.risk_score > 60:
        return Verdict.deny(f"score {request.risk_score} exceeds threshold")
    return Verdict.allow()

policy = GatePolicy().preflight(preflight)
pipeline = Pipeline.create(policy=policy)   # facade; shield enabled

pipeline.gate_crewai()                 # CrewAI hooks
tool = pipeline.gate_langchain(tool)   # wrap a LangChain tool
pipeline.gate_maf()                    # Microsoft Agent Framework middleware
```

The shield enforces the returned `Verdict` using each framework's native blocking mechanism.
The optional postflight callback receives the tool output for audit. The `gate_*` helpers also
exist directly on `GovernancePipeline` for gating-only use.

### Available framework adapters

`gate_crewai()`, `gate_langchain(tool)`, `gate_langgraph(tools)`,
`gate_semantic_kernel(kernel)`, `gate_maf()`, `gate_smolagents(agent_cls=None)`,
`gate_pydantic_ai(agent)`, and `gate_openai_agents(agent)`.

## Shell-hook gating (CLI agents)

For CLI agents (Copilot, Claude Code, Cline, OpenHands), the consumer's hook script pipes the
tool-call event to `traceforge gate`, which relays it to the running pipeline's IPC server and
prints a verdict in the framework's format:

```bash
#!/bin/bash
# Claude Code PreToolUse hook (consumer's script)
echo "$TOOL_EVENT_JSON" | traceforge gate --stdin --format claude-code
# the JSON/exit-code verdict is consumed by the agent's native hook contract
```

`traceforge init claude-code` writes this `PreToolUse` hook into `.claude/settings.json` for
you. The gating pipeline must be running (`traceforge watch`) so the IPC server is listening.

## Read-only scoring (interpret it yourself)

Consumers that prefer to interpret recommendations themselves can score and branch without
registering a policy at all:

```python
from traceforge.governance.pipeline import GovernancePipeline

gov = GovernancePipeline.create()

async def can_use_tool(tool_name, input_data, session_id):
    trace = gov.score_tool_call({
        "tool_name": tool_name,
        "tool_input": input_data,
        "session_id": session_id,
    })
    return trace.suggested_action not in ("deny", "escalate")
```

`score_tool_call()` is read-only: it scores against accumulated state but does **not** advance
the counter, budget, taint, or drift. State changes only when the monitor observes an event from
its source, so blocked calls never corrupt budget or taint.

:::warning Known limitation — an `escalate` verdict collapses to deny

Enforcement is **binary today**: a gate `Verdict` carries only `Decision.ALLOW` or `Decision.DENY`
— there is no `ESCALATE` decision. A risk **recommendation** of `escalate` (surfaced above as a
`suggested_action`, and as `RecommendedAction.ESCALATE` on a `SessionMeta`) therefore has **no
distinct enforcement path**. An in-process `GatePolicy` that wants to escalate has to return
`Verdict.deny(...)`, and the cross-process relay (`traceforge gate --stdin`) maps an `escalate`
verdict to **deny** as well. In practice an "escalate" outcome currently **blocks the tool exactly
like a deny** — there is no human-in-the-loop approval/hold step yet. Read-only scoring still
exposes `escalate` as a recommendation (see above), so a consumer that interprets recommendations
itself can distinguish it; the **gate** cannot.
:::

## The two servers

`traceforge watch` starts two IPC surfaces:

| Server | Endpoint | Returns |
| --- | --- | --- |
| **Score API** | `POST /score`, `GET /health` (default `localhost:7331`) | Read-only assessment (monitor only). |
| **Gate IPC** | Unix/named socket relayed by `traceforge gate` | Enforced verdict from a shield with a `GatePolicy`. |

`traceforge score` serves read-only assessments; `traceforge gate` returns an enforced verdict.
See the [CLI Reference](../getting-started/cli.md) for full command details and the
[SDK reference](../reference/sdk.md) for the underlying object model.
