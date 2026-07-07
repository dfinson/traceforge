---
id: external-gating
title: External Preflight Gating
sidebar_label: External Gating
description: Delegate tool-call ALLOW/DENY decisions to an out-of-process HTTP or subprocess Policy Decision Point, configured entirely from YAML.
---

# External Preflight Gating

In-process gating registers a Python `GatePolicy` (see
[The Gate](../governance/gate.md)). **External** preflight gating moves the ALLOW/DENY
decision *out of process* to a Policy Decision Point (PDP) you run — so gating can be
configured entirely from YAML, with no Python.

Two gate types are available, both selected under `governance.preflight_gate`:

| `type` | Transport | Use when |
| --- | --- | --- |
| `http` | POST JSON to a persistent HTTP PDP (e.g. an [OPA](https://www.openpolicyagent.org/) REST server) | **Recommended.** A long-lived server avoids per-call process spawn cost. |
| `subprocess` | Spawn a command per call; JSON request on **stdin**, JSON verdict on **stdout** | Portable / air-gapped, or driving `opa eval` without a server. |

Both implement the same synchronous `PreflightGate` contract as in-process gates, so
they slot into the existing preflight chain with no framework-adapter changes.

## Configuration

`governance.preflight_gate` is a discriminated union keyed on `type`. It is **mutually
exclusive** with `governance.tool_preflight_gate` (the dotted-path, in-process form):
setting both raises a configuration error at load time.

### HTTP PDP

```yaml
governance:
  preflight_gate:
    type: http
    endpoint: http://localhost:8181/v1/data/traceforge/verdict
    timeout: 2.0            # seconds
    fail_open: false        # false = fail-closed (DENY on any error). Keep false.
    headers:                # optional — merged over Content-Type: application/json
      Authorization: "Bearer ${PDP_TOKEN}"
    max_input_bytes: 65536  # per-string cap on tool input before it is sent
```

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `endpoint` | string | — (required) | Absolute URL of the decision endpoint. |
| `timeout` | float | `2.0` | Per-request timeout, seconds (`> 0`). |
| `fail_open` | bool | `false` | `false` = DENY on error/timeout/non-2xx. See [Fail-closed](#fail-closed-by-default). |
| `headers` | map | `{}` | Extra request headers (e.g. auth tokens). |
| `max_input_bytes` | int | `65536` | Per-string redaction cap (`> 0`). |

The request is sent with `Content-Type: application/json`; any configured `headers` are
merged on top. A non-2xx response, timeout, connection error, or unparseable body is an
error and is resolved by `fail_open`.

### Subprocess decider

```yaml
governance:
  preflight_gate:
    type: subprocess
    command: "opa eval -I -f raw data.traceforge.verdict"
    timeout: 10.0
    fail_open: false
    max_input_bytes: 65536
```

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `command` | string | — (required) | Command line for the decider. |
| `timeout` | float | `10.0` | Per-call timeout, seconds (`> 0`). |
| `fail_open` | bool | `false` | `false` = DENY on error/timeout/non-zero exit/bad output. |
| `max_input_bytes` | int | `65536` | Per-string redaction cap (`> 0`). |

The JSON request is written to the process's **stdin**; the JSON verdict is read from
its **stdout**. A non-zero exit, timeout, or unparseable stdout is an error resolved by
`fail_open`.

`command` is split with `shlex` (POSIX rules on Unix, so quoted arguments survive). On
Windows, quoting is best-effort because native command quoting differs from POSIX — pass
a simple, unquoted command line there where possible.

## Wire contract

### Request (traceforge → decider)

A JSON object describing the assessed tool call. Enums are stringified; the full
`EventTrace` escape hatch is **never** serialized. Example:

```json
{
  "tool": "shell",
  "input": { "command": "rm -rf /tmp/build" },
  "target": "/tmp/build",
  "mechanism": "process.shell",
  "effect": "destructive",
  "capabilities": ["subprocess", "filesystem_write"],
  "scope": ["system.os"],
  "role": ["executor.script_runner"],
  "action": ["remove.delete"],
  "risk_score": 78,
  "risk_band": "danger",
  "suggested_action": "deny",
  "reason": "destructive shell command",
  "session_id": "sess-123",
  "tool_call_id": "call-456",
  "context": {
    "session_id": "sess-123",
    "tool_call_count": 12,
    "denied_count": 1,
    "agent_id": null,
    "user_id": null
  }
}
```

### Response (decider → traceforge)

```json
{ "decision": "deny", "reason": "destructive command blocked" }
```

- `decision` is matched **case-insensitively**. `"deny"` → DENY (with `reason`
  propagated to the model); **anything else → ALLOW**.
- Extra fields (e.g. `score`, `level`) are ignored, so traceforge's own score-server
  response shape works as-is.
- An OPA-style envelope `{ "result": { "decision": "deny", "reason": "..." } }` is
  unwrapped automatically.

## Fail-closed by default

`fail_open` defaults to **`false`** on both gate types. When the decider errors, times
out, returns a non-2xx status / non-zero exit, or emits unparseable output, the call is
**DENIED**. This is deliberate: a gate that fails *open* silently disables enforcement
at exactly the moment something is wrong, which is a security anti-pattern.

Set `fail_open: true` only when availability outranks safety for your deployment and you
have consciously accepted that a broken decider means unfiltered tool calls.

## Input redaction cap

Each string value in `input` is capped to `max_input_bytes` UTF-8 bytes (with a
truncation marker) before being sent. This bounds payload size and limits how much raw
tool input leaves the process.

:::warning Trust the decider
`input` can contain secrets (arguments, file contents, tokens). It crosses the wire to
your PDP, so run the decider on a **trusted** endpoint/host and secure the transport
(TLS, network policy, auth headers). The `max_input_bytes` cap reduces but does not
eliminate exposure.
:::

## Worked example: OPA

Run [OPA](https://www.openpolicyagent.org/) as an HTTP PDP with a rule whose document is
the verdict:

```rego
package traceforge

import rego.v1

# Default allow; deny destructive shell calls.
verdict := {"decision": "deny", "reason": "destructive shell blocked"} if {
    input.mechanism == "process.shell"
    input.effect == "destructive"
} else := {"decision": "allow"}
```

```bash
opa run --server ./traceforge.rego
```

```yaml
governance:
  preflight_gate:
    type: http
    endpoint: http://localhost:8181/v1/data/traceforge/verdict
    fail_open: false
```

OPA wraps the document as `{ "result": { "decision": ... } }`; traceforge unwraps the
`result` envelope, so no glue code is needed. For an air-gapped setup, swap the HTTP gate
for a `subprocess` gate driving `opa eval` against the same policy.
