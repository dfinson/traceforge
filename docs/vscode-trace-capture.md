# Capturing real VS Code agent traces for the golden corpus

This is a **manual, GUI-only** companion to `scripts/capture_traces/`. The headless
capture scripts cannot drive VS Code extensions, so the traces for the editor-based
agents have to be produced by a human running the **same canonical task** on the
**same vendored demo repo**, then handing the native session files back to the
harness.

Everything here was verified on this machine (paths are real). Where a path contains
`<id>`, substitute the session/task UUID you get from the run.

## Ground rules (do not skip)

- **Only the vendored demo repos.** Run every task against a throwaway copy of
  `tests/fixtures/demo_repos/demo-issue-tracker-api` (or `demo-support-dashboard`).
  Never point an agent at real-world or third-party code — these traces are committed
  in native format for CI and must contain only first-party demo content.
- **Use a paid, top-tier model** (e.g. `gpt-5` / Claude Opus class). Cheap models
  produce degenerate tool-use that isn't representative.
- **One task, every agent — so traces are comparable.** The canonical task
  (from `scripts/capture_traces/_repo_task.py`):

  > Add a `GET /tickets/{ticket_id}` endpoint to the FastAPI app that returns the
  > matching ticket or HTTP 404. Add a `get_ticket` method to `TicketService` that
  > delegates to `TicketRepository.get_ticket`, then wire the route in
  > `app/main.py`. Read the files first, make minimal edits, run the test suite.

- **Scrub secrets before handing anything over.** Some extensions serialize the API
  key/headers into the transcript. Grep each file for `sk-`, `Bearer `, `api_key`,
  `authorization` and delete those values. (`_harness.write_trace` also redacts, but
  do not rely on it — GitHub push protection will block the PR otherwise.)

## Step 0 — stage a scratch copy of the demo repo

```powershell
$dst = "$env:TEMP\tm-demo-issue-tracker-api"
Remove-Item $dst -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item -Recurse "<repo>\tests\fixtures\demo_repos\demo-issue-tracker-api" $dst
code $dst
```

Open that folder (not the tracemill repo) in VS Code so the agent's file/edit tools
operate only on the disposable copy.

---

## Channel 1 — VS Code Copilot Chat, **Agent mode** (primary platform)

This is the one that matters most. VS Code Copilot Chat in **agent mode** drives the
**bundled Copilot CLI** (`globalStorage/github.copilot-chat/copilotCli/copilot.ps1`),
which persists to the **same on-disk formats as the standalone CLI** — so the existing
`copilot.yaml` mapping already covers it. No new mapping needed; we just need a real
VS Code-originated session.

**Run it:**
1. Open the scratch repo (Step 0).
2. Open Copilot Chat, switch the dropdown to **Agent**.
3. Pick a top-tier model in the model picker.
4. Paste the canonical task. Let it read, edit, and run tests to completion.

**Where it lands (verified):**
- VS Code keeps its own session index at
  `%APPDATA%\Code\User\globalStorage\github.copilot-chat\session-store.db`
  — table `sessions` rows for the editor have **`host_type = 'vscode'`** (this is how
  you tell VS Code sessions apart from terminal-CLI ones), with `turns` holding
  `user_message` / `assistant_response`.
- The **rich** event stream is written to the shared CLI home:
  `%USERPROFILE%\.copilot\session-state\<id>\events.jsonl`
  — the same `{type, data, id, timestamp, parentId}` lines `copilot.yaml` maps.

**Hand-off:** find the freshest `vscode` session id, then copy its `events.jsonl`.

```powershell
# newest VS Code-originated session id
$db = "$env:APPDATA\Code\User\globalStorage\github.copilot-chat\session-store.db"
python -c "import sqlite3;c=sqlite3.connect(r'$db');print([r[0] for r in c.execute(\"select id from sessions where host_type='vscode' order by updated_at desc limit 3\")])"

# its rich trace
Get-Content "$env:USERPROFILE\.copilot\session-state\<id>\events.jsonl" | Select-Object -First 1
```

Give me the `<id>` (or the `events.jsonl` itself). I run:

```powershell
python scripts\capture_traces\capture_copilot.py <id>   # harvests events.jsonl -> fixture
```

> If `events.jsonl` is absent for the VS Code session (older builds only wrote the
> SQLite turns), fall back to the thin `copilot_markdown` SQLite path: export the
> `turns` rows for that `session_id` and we map via `copilot_markdown.yaml`.

---

## Channel 2 — Cline / Roo Cline

Both are installed (`microsoftai.ms-roo-cline` is in active use here). They share one
native format, which tracemill's **`cline`** preprocessor already targets.

**Run it:**
1. Open the scratch repo.
2. Open the Cline (or Roo Code) panel, set a top-tier model + your API key.
3. Paste the canonical task; approve its tool actions through to a green test run.

**Where it lands (verified):**
- Cline: `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\tasks\<id>\`
- Roo:   `%APPDATA%\Code\User\globalStorage\microsoftai.ms-roo-cline\tasks\<id>\`

Each `<id>` dir holds:
- `api_conversation_history.json`  ← the native Anthropic-style turn list (**this is
  the trace tracemill ingests**)
- `ui_messages.json`               ← UI render log (not needed)

**Hand-off:** the newest task dir is the one you just created —

```powershell
$tasks = "$env:APPDATA\Code\User\globalStorage\microsoftai.ms-roo-cline\tasks"
Get-ChildItem $tasks | Sort-Object LastWriteTime -Desc | Select-Object -First 1 FullName
```

Send me `api_conversation_history.json`. Scrub it for secrets first.

---

## Channel 3 — Continue.dev  (needs install)

Not installed on this machine (`~/.continue` is absent). tracemill's **`continue_dev`**
preprocessor expects the camelCase session JSON.

**Run it:**
1. Install the **Continue** extension; sign in / set a top-tier model.
2. Open the scratch repo; in the Continue panel choose **Agent** mode.
3. Paste the canonical task; run to a green test suite.

**Where it lands:**
- Sessions: `%USERPROFILE%\.continue\sessions\<id>.json`
  (index in `%USERPROFILE%\.continue\sessions\sessions.json`)
- Raw dev data: `%USERPROFILE%\.continue\dev_data\`

**Hand-off:** send me the newest `~/.continue/sessions/<id>.json`.

---

## Channel 4 — Amazon Q for VS Code  (needs install + Builder ID)

Not installed here (no AWS Toolkit / Amazon Q extension). tracemill's **`amazonq`**
preprocessor targets its chat transcript.

**Run it:**
1. Install **Amazon Q** (AWS Toolkit), sign in with Builder ID / IAM Identity Center.
2. Open the scratch repo; in Amazon Q chat enable **agentic** coding.
3. Paste the canonical task; run to green.

**Where it lands:**
- Workspace chat state: `%APPDATA%\Code\User\globalStorage\amazonwebservices.amazon-q-vscode\`
- Some builds also write under `%USERPROFILE%\.aws\amazonq\`

**Hand-off:** zip the `amazonwebservices.amazon-q-vscode` globalStorage dir (minus any
cache) and send it; I'll locate the transcript and trim to the session.

---

## What I do with the files

For each handed-back native file I:
1. Secret-scan it again, then drop it verbatim into
   `tests/fixtures/raw_traces/<framework>/<scenario>.jsonl` via `write_trace(...)`
   (records `source_repo`, `framework_version`, `model`, `notes` in `meta.yaml`).
2. Run `uv run pytest tests/e2e/test_raw_traces.py -q` — the golden harness replays the
   trace through the real mapping and fails on any `raw` fallthrough (drift guard).
3. If a new event type falls through, I add the mapping (as done for copilot
   `session.model_change`) and re-run.

## Quick reference — native trace locations

| Channel | Native file (what I ingest) | Mapping |
|---|---|---|
| Copilot Chat **agent** | `~/.copilot/session-state/<id>/events.jsonl` (host_type=`vscode` in the VS Code `session-store.db`) | `copilot.yaml` |
| Copilot Chat (thin fallback) | `github.copilot-chat/session-store.db` → `turns` | `copilot_markdown.yaml` |
| Cline / Roo Cline | `globalStorage/<ext>/tasks/<id>/api_conversation_history.json` | `cline` |
| Continue.dev | `~/.continue/sessions/<id>.json` | `continue_dev` |
| Amazon Q | `globalStorage/amazonwebservices.amazon-q-vscode/...` | `amazonq` |
