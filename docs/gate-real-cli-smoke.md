# Smoke test: a real vendor CLI fires the `traceforge gate` PreToolUse hook

> **⚠️ NON-CI — this runbook requires a real vendor CLI (Claude Code) and a human.**
> It is deliberately **not** part of the automated test suite. CI already covers the
> *synthetic* halves of this path (`tests/e2e/test_gate_init_e2e.py` proves the hook is
> injected into `.claude/settings.json`; `tests/e2e/test_gate_stdin_e2e.py` drives the
> real `traceforge gate --stdin` relay with live allow/deny verdicts). The one honest
> blind spot they can't cover is a **real Claude Code process actually firing the
> installed hook on a live tool call** — that is what you verify by hand here.
>
> A CI-friendly *fake-vendor-CLI* harness closes as much of this gap as possible
> without a vendor binary: `tests/e2e/test_gate_real_cli_smoke_harness.py`. See
> [Appendix B](#appendix-b--the-automated-fake-vendor-harness).

## What this proves

By the end you will have watched a **real Claude Code CLI**:

1. **Allow baseline** — with no gate policy, run a tool call and watch it proceed
   normally (the gate fires, sees allow-all, and steps aside).
2. **Deny** — declare a deny policy, restart the pipeline, run the *same* tool call,
   and watch Claude Code **block it** and surface the traceforge deny reason.

The mechanical core — the exact command `init` injects, and the allow/deny verdict
bytes it returns — is reproducible with a one-line `traceforge gate --stdin` sanity
check (given below with its verified output), so you can confirm the wiring even
before you open the vendor CLI.

## How the pieces fit together

```
Claude Code (real CLI)
  │  every tool call → PreToolUse hook
  ▼
<abs>/traceforge gate --stdin --agent claude-code        # injected into .claude/settings.json
  │  reads {session_id, tool_name, tool_input} from stdin
  ▼
gate client  →  looks up session_id in the registry, falls back to "_default"
  │            (~/.traceforge/system.db :: gate_endpoints)
  ▼
Gate IPC server  (running inside `traceforge watch`)
  │  scores + runs the preflight policy chain
  ▼
Verdict  →  translated into Claude Code's hook dialect on stdout
            allow → {}      deny → {"hookSpecificOutput": {... "permissionDecision":"deny" ...}}
```

Source of truth for each hop: hook injection `src/traceforge/cli/init_cmd.py`
(`_init_claude_code`, ~L167–199); relay + fail-closed dialect
`src/traceforge/gate/client.py` (`_gate_from_stdin_impl` L93–161, `_output_deny`/`_output_allow`
L164–264); IPC server + registration `src/traceforge/gate/server.py` and
`src/traceforge/cli/watch.py` (`gate_server.register_session("_default")`, ~L156);
policy loading `src/traceforge/governance/shield.py` (`build_policy_from_config`, L37).

---

## Prerequisites

- **traceforge installed** and on `PATH` (`traceforge --version` works). A `pip install
  traceforge-toolkit` or an editable dev checkout both work.
- **A real Claude Code CLI** installed and authenticated. (The same shape works for
  Copilot CLI — swap `claude-code` for `copilot-cli` in step 1 and `--agent
  copilot-cli` throughout; its hook lands in `.github/hooks/traceforge.json`.)
- **A throwaway project directory** you don't mind an agent poking at. Everything below
  assumes you `cd` into it first:

  ```bash
  mkdir /tmp/tf-smoke && cd /tmp/tf-smoke
  ```

> Paths in the captured output below are from the machine this runbook was verified on
> (2026-07); substitute your own. See [Appendix A](#appendix-a--verified-on-this-machine).

---

## Step 1 — Install the hook

```bash
traceforge init claude-code --project .
```

Verified output (the middle line is your absolute `traceforge` path):

```text
✓ Wrote PreToolUse hook to /tmp/tf-smoke/.claude/settings.json
  Command: /abs/path/to/traceforge gate --stdin --agent claude-code
  Note: configure a gate policy (governance.gate_policy) and run `traceforge watch` — until then the gate allows every call.
```

This writes (or idempotently merges into) `.claude/settings.json`. The resulting shape —
a `.*` matcher so **every** tool call is gated, plus the injected relay command:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": ".*",
        "hooks": [
          {
            "type": "command",
            "command": "/abs/path/to/traceforge gate --stdin --agent claude-code"
          }
        ]
      }
    ]
  }
}
```

The `command` is an absolute path so Claude Code can exec it regardless of its own
working directory (`_find_traceforge_command`, `init_cmd.py` L59–67). Re-running `init`
is a no-op once the traceforge hook is present.

> **Note:** `init` only lays down the *wiring*. The hook relays every call to the gate,
> which **allows everything until you declare a policy** (step 2). That is the whole
> point of the loud warning you'll see in step 3.

---

## Step 2 — Declare a deny policy

A preflight gate is a callable `(request, ctx) -> Verdict` referenced by a dotted import
path under `governance.gate_policy.preflight` (`shield.py` L37–108; the request exposes
`request.tool`, `request.input`, etc. — see `src/traceforge/sdk/gate_types.py`
`ToolCallRequest`). Create a tiny policy module in the project:

```python
# smoke_policy.py
from traceforge.sdk.verdict import Verdict


def deny_all(request, ctx):
    # Block every tool call so the smoke is unambiguous — the FIRST tool Claude
    # Code tries is denied, with this reason surfaced back to the model.
    return Verdict.deny(f"traceforge smoke: blocked {request.tool}")
```

…and a config that points at it:

```yaml
# traceforge-smoke.yaml
governance:
  gate_policy:
    preflight:
      - smoke_policy.deny_all
```

Because `smoke_policy.py` is imported by dotted path, its directory must be on
`PYTHONPATH` when you start the daemon. From the project directory:

```bash
# bash / zsh
export PYTHONPATH="$PWD"
```

```powershell
# PowerShell
$env:PYTHONPATH = (Resolve-Path .).Path
```

---

## Step 3 — Start the gate / pipeline session

`traceforge watch` builds the policy, starts the Gate IPC server, and registers a
`_default` session that any CLI hook falls back to. Start it in **its own terminal**
(keep `PYTHONPATH` set from step 2) and leave it running:

```bash
traceforge watch --config traceforge-smoke.yaml --frameworks claude
```

> `--frameworks claude` keeps detection deterministic (watch exits if it detects
> nothing; it detects `claude` once `~/.claude/projects` exists, which it will after
> you've run Claude Code at least once). Add `--no-score` if port 7331 is busy.

**Enforcing** output looks like this (note: **no** allow-all warning):

```text
Detected 1 framework(s): claude
Gate IPC server listening on tcp://127.0.0.1:50510
Watching 1 pipeline(s). Press Ctrl+C to stop.
```

For contrast, start it **without** `--config` (or with a config that declares no
`gate_policy`) and you get the allow-all baseline, which prints a loud banner:

```text
Gate IPC server listening on tcp://127.0.0.1:65188

  ============================================================
  WARNING: gating enforcement is INACTIVE (allow-all).
  No gate policy is configured, so EVERY tool call is ALLOWED.
  ...
  ============================================================
```

That banner is your proof the two modes are distinct (`_warn_gating_inactive`,
`watch.py` L38–65).

### Sanity-check the wiring before touching the CLI

The relay verdict is deterministic, so you can confirm the whole chain with one pipe —
**run this while `watch` is up**, from the same shell environment (same `HOME`) so the
registry lookup resolves. `session_id` is intentionally a value the daemon never saw;
it falls back to `_default` (`client.py` L124–126), exactly like a real Claude Code
session id:

```bash
echo '{"hook_event_name":"PreToolUse","session_id":"smoke","tool_name":"Bash","tool_input":{"command":"rm -rf /"}}' \
  | traceforge gate --stdin --agent claude-code
```

```powershell
'{"hook_event_name":"PreToolUse","session_id":"smoke","tool_name":"Bash","tool_input":{"command":"rm -rf /"}}' |
  traceforge gate --stdin --agent claude-code
```

**Verified verdicts** (this is the evidence to capture):

| Daemon policy | stdout | exit |
| --- | --- | --- |
| Deny (`smoke_policy.deny_all`) | `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "traceforge smoke: blocked Bash"}}` | 0 |
| Allow-all (no policy) | `{}` | 0 |

A fail-closed deny still exits **0** — the stdout JSON *is* the contract Claude Code
reads (`client.py` L65–90, `_output_deny` L201–220). If the daemon is not running you'll
still get a deny (`"... not registered with any pipeline"`), because the gate fails
closed.

---

## Step 4 — Fire a real tool call in Claude Code

In a **separate terminal**, from the same project directory (so Claude Code loads
`.claude/settings.json`):

```bash
cd /tmp/tf-smoke
claude          # start the real Claude Code CLI
```

Ask it to run a shell command, e.g.:

> Run the shell command `echo hello` for me.

### Expected evidence

**Allow baseline (daemon started with no policy):** Claude Code runs the command
normally. The `watch` terminal shows the allow-all warning from step 3; the gate fired
(`{}` on stdout) and deferred to Claude Code's own permission flow — so from the user's
seat the tool "just works", which is the correct allow behavior.

**Deny (daemon started with `traceforge-smoke.yaml`):** Claude Code is **blocked before
the tool executes** and surfaces the deny reason — `traceforge smoke: blocked Bash`
(the exact `permissionDecisionReason` from the verdict). The command never runs.

Capture the contrast — a screenshot or copy-paste of Claude Code refusing the tool
with the traceforge reason, alongside the deterministic `gate --stdin` verdict from the
sanity check — and the smoke is complete.

> If Claude Code appears to ignore the hook: confirm `.claude/settings.json` exists in
> the directory you launched `claude` from, that `traceforge watch` is still running,
> and that the sanity-check pipe in step 3 returns the deny JSON. Those three isolate
> which hop failed.

---

## Cleanup

- `Ctrl+C` the `traceforge watch` terminal.
- Delete the throwaway project (`rm -rf /tmp/tf-smoke`), which removes the injected
  `.claude/settings.json`, `smoke_policy.py`, and the config.
- The gate registry row self-heals once the daemon exits (`registry.lookup_endpoint`
  drops rows whose owning PID is gone), so nothing persists in `~/.traceforge`.

---

## Appendix A — verified on this machine

The command/verdict captures above were produced on 2026-07 (Windows, Python 3.12) by
driving a **real `traceforge watch`** daemon and execing the exact command string from
`.claude/settings.json` with a Claude-Code-shaped PreToolUse event — i.e. everything in
this runbook except the literal keypress inside the Claude Code UI. The only
platform-specific detail is the IPC transport: a `tcp://127.0.0.1:<port>` loopback
socket on Windows vs. a unix-domain socket under `~/.traceforge/gates/` on POSIX
(`server.py` `_default_sock_path` / `start`).

## Appendix B — the automated fake-vendor harness

`tests/e2e/test_gate_real_cli_smoke_harness.py` (marked `e2e` + `slow`) automates the
mechanical core with **no vendor binary**: it runs `traceforge init claude-code`, reads
the injected command **verbatim** out of `.claude/settings.json`, and execs it from a
fake "vendor CLI" against a real in-process Gate IPC server registered as `_default`.
Swapping the pipeline policy flips the result between allow (`{}`, tool runs) and deny
(tool blocked with the policy reason) — the same contrast this runbook captures by hand.
Run it with:

```bash
uv run python -m pytest tests/e2e/test_gate_real_cli_smoke_harness.py -v
```

What it deliberately does **not** cover — and why this manual runbook still exists — is
a genuine vendor process reading `settings.json` and firing the hook through its own
tool-call machinery. That last mile needs a real CLI and a human.
