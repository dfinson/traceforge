"""VS Code Copilot Chat preprocessor — replay the ChatModel journal.

VS Code persists each Copilot Chat session as a line-delimited journal at
``workspaceStorage/<hash>/chatSessions/<sessionId>.jsonl`` (ChatModel version 3).
Each physical line is one journal record:

* ``{"kind": 0, "v": {...}}``            — full snapshot (session start)
* ``{"kind": 1, "k": [path...], "v": x}`` — *set* value ``x`` at ``path``
* ``{"kind": 2, "k": [path...], "v": [...]}`` — *append* (list-extend) at ``path``

Paths mix dict keys and integer array indices, e.g.
``["requests", 2, "response"]``. The interesting state lives under ``requests[]``:
each request carries the user ``message``, a streamed ``response`` part list
(``thinking`` / ``toolInvocationSerialized`` / markdown text / file refs), and a
terminal ``result`` with timings.

Because the adapter feeds records one physical line at a time, this preprocessor
keeps a small amount of module-level state (a mirror of the ``requests`` index ->
metadata map) so streamed response parts can be attributed to their originating
request. State is reset whenever a snapshot (``kind == 0``) is seen, which is the
first record of every session file, so state never bleeds across sessions.
"""

from __future__ import annotations

from typing import Any

from tracemill.preprocessors.registry import register_preprocessor

# Per-session reconstruction state (reset on every snapshot record).
_REQ_IDS: dict[int, str | None] = {}
_REQ_MODELS: dict[int, Any] = {}
_REQ_TS: dict[int, Any] = {}
_REQ_COUNT = [0]  # boxed int so helpers can mutate it


def _reset() -> None:
    _REQ_IDS.clear()
    _REQ_MODELS.clear()
    _REQ_TS.clear()
    _REQ_COUNT[0] = 0


def _agent_id(req: dict[str, Any]) -> Any:
    agent = req.get("agent")
    if isinstance(agent, dict):
        return agent.get("id") or agent.get("name")
    return agent


def _emit_part(part: Any, idx: int) -> dict[str, Any] | None:
    """Turn one streamed ``response`` part into a flat, typed dict."""
    if not isinstance(part, dict):
        return None
    flat = dict(part)
    # Markdown content parts have no ``kind`` discriminator; everything else
    # (thinking / toolInvocationSerialized / inlineReference / ...) does.
    flat["event_type"] = part.get("kind") or "assistant_text"
    flat["request_id"] = _REQ_IDS.get(idx)
    flat["model"] = _REQ_MODELS.get(idx)
    flat["timestamp"] = _REQ_TS.get(idx)
    return flat


def _emit_request(req: dict[str, Any], idx: int) -> list[dict[str, Any]]:
    """Register a request at ``idx`` and emit its user message + any inline parts."""
    _REQ_IDS[idx] = req.get("requestId")
    _REQ_MODELS[idx] = req.get("modelId")
    _REQ_TS[idx] = req.get("timestamp")

    message = req.get("message")
    text = message.get("text") if isinstance(message, dict) else message

    out: list[dict[str, Any]] = [
        {
            "event_type": "user_message",
            "request_id": req.get("requestId"),
            "model": req.get("modelId"),
            "agent": _agent_id(req),
            "text": text,
            "timestamp": req.get("timestamp"),
        }
    ]
    # A compacted journal may carry the full response/result inline on the
    # request itself; a streamed one appends them later. Emit whatever is here.
    for part in req.get("response") or []:
        emitted = _emit_part(part, idx)
        if emitted is not None:
            out.append(emitted)
    result = req.get("result")
    if isinstance(result, dict):
        out.append(_emit_result(result, idx))
    return out


def _emit_result(result: dict[str, Any], idx: int) -> dict[str, Any]:
    timings = result.get("timings") if isinstance(result.get("timings"), dict) else {}
    return {
        "event_type": "request_result",
        "request_id": _REQ_IDS.get(idx),
        "model": _REQ_MODELS.get(idx),
        "first_progress_ms": timings.get("firstProgress"),
        "total_elapsed_ms": timings.get("totalElapsed"),
        "timestamp": _REQ_TS.get(idx),
    }


@register_preprocessor("copilot_vscode")
def preprocess_copilot_vscode(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand one VS Code ChatModel journal record into typed flat events."""
    # Passthrough for already-typed rows (generic conformance probes feed a line
    # keyed by the post-preprocessor type_field, with no journal ``kind``).
    if "kind" not in obj and obj.get("event_type"):
        return [obj]

    kind = obj.get("kind")

    # ── Snapshot: session start (+ any requests already present) ──────────────
    if kind == 0:
        _reset()
        snap = obj.get("v") if isinstance(obj.get("v"), dict) else {}
        out: list[dict[str, Any]] = [
            {
                "event_type": "session_started",
                "session_id": snap.get("sessionId"),
                "version": snap.get("version"),
                "responder": snap.get("responderUsername"),
                "initial_location": snap.get("initialLocation"),
                "timestamp": snap.get("creationDate"),
            }
        ]
        for req in snap.get("requests") or []:
            if isinstance(req, dict):
                out.extend(_emit_request(req, _REQ_COUNT[0]))
                _REQ_COUNT[0] += 1
        return out

    path = obj.get("k")
    value = obj.get("v")
    if not isinstance(path, list) or not path:
        return []

    # ── Append (list-extend) records ──────────────────────────────────────────
    if kind == 2:
        # New requests appended to the top-level requests array.
        if path == ["requests"]:
            out = []
            for req in value or []:
                if isinstance(req, dict):
                    out.extend(_emit_request(req, _REQ_COUNT[0]))
                    _REQ_COUNT[0] += 1
            return out
        # Streamed response parts appended to requests[i].response.
        if len(path) == 3 and path[0] == "requests" and path[2] == "response":
            idx = path[1]
            out = []
            for part in value or []:
                emitted = _emit_part(part, idx)
                if emitted is not None:
                    out.append(emitted)
            return out
        return []

    # ── Set records: only the terminal per-request result is interesting ──────
    if kind == 1:
        if len(path) == 3 and path[0] == "requests" and path[2] == "result":
            if isinstance(value, dict):
                return [_emit_result(value, path[1])]
        return []

    return []
